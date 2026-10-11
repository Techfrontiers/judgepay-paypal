"""EscrowService — state machine หลักของ JudgePay PayPal Edition.

โฟลว์ (Phase A / sandbox):
  create_task          -> สร้าง PayPal order (intent=AUTHORIZE), AWAITING_APPROVAL
  confirm_authorization -> buyer อนุมัติแล้ว -> authorize ล็อกเงิน, OPEN
  submit_work          -> worker ส่งงาน (ล็อก worker_email ครั้งแรก), SUBMITTED
  judge_only           -> นโยบาย fail-closed + AI ให้คะแนนเท่านั้น ไม่แตะเงิน
                           JUDGED (>=80, รอคนยืนยัน) | NEEDS_REVIEW (60-79)
                           | REFUNDED (<60 หรือ hard-fail -> void)
  settle_approved      -> คนยืนยันด้วย HMAC token -> atomic claim -> capture
                           + payout -> COMPLETED (payout ต้อง SUCCESS)
                           ผลคลุมเครือ -> NEEDS_RECONCILIATION (ห้าม retry มั่ว)
  expire_task          -> หมดเวลา -> void, EXPIRED
  reconcile            -> เทียบ DB กับ PayPal จริง แล้วซ่อมสถานะ

กฎเหล็ก:
- ไม่เชื่อค่าจาก client: amount/currency/recipient/score/verdict อ่านจาก DB/ PayPal
- AI approve ไม่ใช่คำสั่งจ่ายเงิน — ต้องมี human confirmation token ก่อนเสมอ
- ธุรกรรมที่ผลไม่ชัดเจน ห้าม retry อัตโนมัติ
"""

import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from .store import TaskStore, utcnow
from .paypal_client import PayPalClient, PayPalError
from .judge import AIJudge
from .policy import (
    APPROVE_THRESHOLD, REVIEW_MIN, evaluate_policy, normalize_verdict,
)
from .payout_status import normalize_payout_item, aggregate_task_status

# กันงานขยะ: output ยาวเกินนี้ไม่รับ
MAX_OUTPUT_LEN = 20000
# อายุ confirmation token (วินาที)
CONFIRM_TOKEN_TTL = 1800


def _op_key(task_id, op_type):
    """Idempotency key ที่เสถียร — derive จาก task + op_type เท่านั้น.

    ห้ามสุ่มใหม่ตอน retry: PayPal ใช้ key นี้กันทำซ้ำ (capture: PayPal-Request-Id,
    payout: sender_batch_id จำ 30 วัน).
    """
    return f"judgepay-{task_id}-{op_type}"


def _request_hash(op_type, amount, currency, recipient=""):
    """ลายนิ้วมือของธุรกรรม — จับได้ถ้า key เดิมถูกเอาไปใช้กับธุรกรรมอื่น."""
    raw = f"{op_type}|{float(amount):.2f}|{(currency or '').upper()}|{recipient or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_definite_failure(exc):
    """แยก 'ล้มเหลวแน่ๆ' ออกจาก 'ผลคลุมเครือ'.

    definite (retry ด้วย key เดิมได้): HTTP 400/404/422 ที่ไม่ใช่ duplicate
    ambiguous (ห้ามสรุปว่าล้มเหลว): timeout, connection error, 5xx,
        409/DUPLICATE (key นี้อาจสำเร็จไปแล้ว — ต้องไปถาม PayPal ก่อน)
    default: ambiguous — ระวังไว้ก่อนเสมอ. timeout ห้าม mark FAILED เด็ดขาด.
    """
    msg = str(exc)
    if "DUPLICATE" in msg.upper():
        return False
    m = re.search(r"-> (\d{3})", msg)
    if m:
        code = int(m.group(1))
        if code in (400, 404, 422):
            return True
        return False
    return False


def _extract_ref(op_type, result):
    """ดึง paypal ref จากผลลัพธ์ — เก็บลง op row ไว้ adopt ทีหลัง."""
    if op_type == "capture":
        return result.get("capture_id", "")
    if op_type == "payout":
        return result.get("batch_id", "")
    return ""  # void ไม่มี ref ใหม่


def _ref_result(op_type, op):
    """สร้างผลลัพธ์จาก op ที่เคย SUCCEEDED — คืนของเดิม ไม่ยิงใหม่."""
    ref = op.get("paypal_ref") or ""
    if op_type == "capture":
        return {"capture_id": ref, "status": "COMPLETED"}
    if op_type == "payout":
        return {"batch_id": ref, "status": "UNKNOWN"}
    return {"authorization_id": "", "status": "VOIDED"}


class EscrowError(Exception):
    pass


def _hmac_key():
    key = os.getenv("JUDGEPAY_HMAC_SECRET") or os.getenv("FLASK_SECRET_KEY", "")
    if not key:
        raise EscrowError("HMAC secret not configured (set JUDGEPAY_HMAC_SECRET)")
    return key.encode("utf-8")


class EscrowService:
    def __init__(self, paypal=None, store=None, judge=None):
        self.paypal = paypal or PayPalClient()
        self.store = store or TaskStore()
        self.judge = judge or AIJudge()

    # ---------- 1. สร้าง task ----------

    def create_task(self, description, amount, currency="USD", deadline_hours=24,
                    requester_email="", owner_id="", acceptance_criteria=None):
        if amount <= 0:
            raise EscrowError("amount must be > 0")
        if not description or not description.strip():
            raise EscrowError("description is required")
        deadline_at = (datetime.now(timezone.utc)
                       + timedelta(hours=deadline_hours)).isoformat()
        criteria_json = json.dumps(acceptance_criteria or [], ensure_ascii=False)
        # B1: สร้าง task row ก่อนเพื่อให้ได้ task_id -> เอาไปเป็น reference_id
        # ที่เสถียรของ PayPal order (traceability; order ไม่ขยับเงินเลย retry
        # ปลอดภัยอยู่แล้ว)
        task = self.store.create_task(description, amount, currency, deadline_at,
                                      requester_email, owner_id, criteria_json)
        try:
            order = self.paypal.create_order(
                amount=amount,
                currency=currency,
                description=f"JudgePay escrow: {description[:80]}",
                reference_id=f"judgepay-{task['id']}-order",
            )
        except Exception as e:
            # order สร้างไม่สำเร็จ — task ค้าง AWAITING_APPROVAL แบบไม่มี order_id
            # (ซื่อสัตย์กว่าลบทิ้ง: ให้เห็นว่าพังตรงไหน)
            raise EscrowError(f"PayPal order creation failed: {e}")
        task = self.store.update_task(task["id"], order_id=order["order_id"])
        return {
            "success": True,
            "task_id": task["id"],
            "order_id": order["order_id"],
            "approval_url": order["approval_url"],
            "status": task["status"],
            "mock": self.paypal.mock,
            "next": "requester approves at approval_url, then run: confirm",
        }

    # ---------- 2. ยืนยัน authorization (หลัง buyer อนุมัติ) ----------

    def confirm_authorization(self, task_id, order_id=None):
        task = self._require(task_id)
        if task["status"] != "AWAITING_APPROVAL":
            raise EscrowError(f"task {task_id} is {task['status']}, cannot confirm")
        oid = order_id or task["order_id"]
        try:
            auth = self.paypal.authorize_order(oid)
        except PayPalError as e:
            raise EscrowError(str(e))
        task = self.store.update_task(
            task_id, authorization_id=auth["authorization_id"], status="OPEN"
        )
        return {
            "success": True,
            "task_id": task_id,
            "authorization_id": auth["authorization_id"],
            "paypal_status": auth["status"],
            "status": "OPEN",
        }

    # ---------- 3. worker ส่งงาน (A3: recipient binding + A7: input limit) ----------

    def submit_work(self, task_id, output, worker_email=""):
        task = self._require(task_id)
        if task["status"] != "OPEN":
            raise EscrowError(f"task {task_id} is {task['status']}, cannot submit")
        if self._is_past_deadline(task):
            raise EscrowError(f"task {task_id} is past deadline; run expire instead")
        if len(output or "") > MAX_OUTPUT_LEN:
            raise EscrowError(
                f"output too long ({len(output)} chars > {MAX_OUTPUT_LEN}); "
                "split it or shorten it"
            )
        fields = {"worker_output": output, "status": "SUBMITTED"}
        email = (worker_email or "").strip()
        if email:
            if task["worker_email_locked"]:
                # ล็อกแล้ว — ห้ามเปลี่ยนผู้รับเงินเด็ดขาด
                if email.lower() != (task["worker_email"] or "").lower():
                    raise EscrowError(
                        "worker_email already locked to "
                        f"{task['worker_email']}; cannot redirect payout"
                    )
            else:
                fields["worker_email"] = email
                fields["worker_email_locked"] = 1
        task = self.store.update_task(task_id, **fields)
        return {
            "success": True,
            "task_id": task_id,
            "output_length": len(output or ""),
            "status": "SUBMITTED",
            "next": "run: judge (AI only, no money moves)",
        }

    # ---------- 4a. AI ตัดสินอย่างเดียว — ไม่แตะเงิน (A5) ----------

    def judge_only(self, task_id):
        task = self._require(task_id)
        if task["status"] != "SUBMITTED":
            raise EscrowError(f"task {task_id} is {task['status']}, nothing to judge")

        criteria = self._load_criteria(task)
        fails = evaluate_policy(task["description"], criteria, task["worker_output"])
        if fails:
            verdict = {
                "score": 0,
                "verdict": "reject",
                "reasoning": "policy hard-fail: " + ", ".join(fails)
                             + " — rejected without AI call",
            }
        else:
            raw = self.judge.evaluate(task["description"], task["worker_output"] or "")
            score = max(0, min(100, int(raw.get("score", 0))))
            v, r = normalize_verdict(score, raw.get("verdict", "reject"),
                                     str(raw.get("reasoning", "")))
            verdict = {"score": score, "verdict": v, "reasoning": r}

        self.store.update_task(
            task_id,
            judge_score=verdict["score"],
            judge_verdict=verdict["verdict"],
            judge_reasoning=verdict["reasoning"],
        )

        if verdict["score"] >= APPROVE_THRESHOLD:
            self.store.update_task(task_id, status="JUDGED")
            nxt = "human reviews at the review page, then settles with confirmation token"
            status = "JUDGED"
        elif verdict["score"] >= REVIEW_MIN:
            self.store.update_task(task_id, status="NEEDS_REVIEW")
            nxt = "human decision required on the review page (no auto-settle)"
            status = "NEEDS_REVIEW"
        else:
            return self._settle_reject(task_id, verdict)

        return {
            "success": True, "task_id": task_id,
            "verdict": verdict["verdict"], "score": verdict["score"],
            "reasoning": verdict["reasoning"], "status": status, "next": nxt,
        }

    def manual_evaluate(self, task_id, approve):
        """คนตัดสินเอง (CLI): เก็บ verdict แล้วไปตาม band เดียวกัน — ไม่จ่ายเงินทันที."""
        task = self._require(task_id)
        if task["status"] != "SUBMITTED":
            raise EscrowError(f"task {task_id} is {task['status']}, nothing to judge")
        verdict = (
            {"score": 100, "verdict": "approve", "reasoning": "manual approve via CLI"}
            if approve else
            {"score": 0, "verdict": "reject", "reasoning": "manual reject via CLI"}
        )
        self.store.update_task(
            task_id, judge_score=verdict["score"], judge_verdict=verdict["verdict"],
            judge_reasoning=verdict["reasoning"],
        )
        if approve:
            self.store.update_task(task_id, status="JUDGED")
            return {"success": True, "task_id": task_id, "status": "JUDGED",
                    "next": "run: settle (requires confirmation)"}
        return self._settle_reject(task_id, verdict)

    # ---------- 4b. human confirmation token (A2) ----------

    @staticmethod
    def _confirmation_message(task):
        amt = f"{float(task['amount']):.2f}"
        return "|".join([
            str(task["id"]), amt, (task["currency"] or "").upper(),
            task["worker_email"] or "", task["judge_verdict"] or "",
            str(task["judge_score"]),
        ])

    def make_confirmation_token(self, task_id):
        """ออก token บนหน้า review — ผูกกับสถานะปัจจุบันทั้งก้อน + วันหมดอายุ."""
        task = self._require(task_id)
        if task["status"] not in ("JUDGED", "NEEDS_REVIEW"):
            raise EscrowError(f"task {task_id} is {task['status']}, cannot confirm")
        ts = str(int(time.time()))
        msg = self._confirmation_message(task) + "|" + ts
        sig = hmac.new(_hmac_key(), msg.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{ts}:{sig}"

    def verify_confirmation_token(self, task_id, token):
        """ตรวจ token: ถูกฟอร์แมตไหม, หมดอายุไหม, ค่าตรงกับ DB ปัจจุบันไหม."""
        task = self._require(task_id)
        try:
            ts, sig = (token or "").split(":", 1)
            age = time.time() - int(ts)
        except (ValueError, TypeError):
            return False
        if age < 0 or age > CONFIRM_TOKEN_TTL:
            return False
        msg = self._confirmation_message(task) + "|" + ts
        expected = hmac.new(_hmac_key(), msg.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    # ---------- 4c. settle หลังมนุษย์ยืนยัน (A2+A3+A4) ----------

    def settle_approved(self, task_id, confirmation_token, human_approve=False):
        task = self._require(task_id)
        if task["status"] == "NEEDS_REVIEW" and not human_approve:
            raise EscrowError("task needs explicit human review decision first")
        if task["status"] not in ("JUDGED", "NEEDS_REVIEW"):
            raise EscrowError(f"task {task_id} is {task['status']}, cannot settle")
        if not self.verify_confirmation_token(task_id, confirmation_token):
            raise EscrowError(
                "confirmation token invalid, expired, or task changed since review"
            )
        # A3: ตรวจ invariant แบบ read-only ก่อนเงินขยับ (fail-closed)
        self._assert_payment_invariants(task)
        # A4: atomic claim — ชนะคนเดียวเท่านั้น
        if not self.store.claim_for_settlement(task_id, ("JUDGED", "NEEDS_REVIEW")):
            raise EscrowError("settlement already in progress or completed by another process")
        return self._settle_approve(task_id, self._require(task_id))

    def _assert_payment_invariants(self, task):
        """ตรวจก่อน capture: ยอด/สกุลเงินต้องตรงกับ authorization จริง,
        ผู้รับเงินต้องเป็น locked email เท่านั้น. ไม่ตรง -> ไม่จ่าย."""
        if not task.get("authorization_id"):
            raise EscrowError("no authorization on task — cannot settle")
        try:
            auth = self.paypal.get_authorization(task["authorization_id"])
        except PayPalError as e:
            raise EscrowError(f"cannot verify authorization before capture: {e}")
        if (f"{float(auth.get('amount') or 0):.2f}" != f"{float(task['amount']):.2f}"
                or (auth.get("currency") or "").upper() != (task["currency"] or "").upper()):
            raise EscrowError(
                f"authorization amount/currency {auth.get('amount')} {auth.get('currency')} "
                f"!= task {float(task['amount']):.2f} {task['currency']} — refusing to capture"
            )
        if not task.get("worker_email"):
            raise EscrowError("no locked payout recipient — cannot payout")
        # recipient ที่ใช้ต้องมาจาก DB ที่ล็อกไว้เท่านั้น ห้ามรับจาก client

    def _settle_approve(self, task_id, task):
        """ปล่อยเงิน: capture ก่อน แล้ว payout — ผ่าน _execute_op (idempotent).

        ผลคลุมเครือ -> NEEDS_RECONCILIATION + op ถูกบันทึกไว้ reconcile ทีหลัง.
        ห้ามยิงซ้ำด้วย key ใหม่เด็ดขาด.
        """
        auth_id = task["authorization_id"]
        recipient = task["worker_email"]  # มาจาก DB ที่ล็อกไว้เท่านั้น
        try:
            cap = self._execute_op(task, "capture")
        except EscrowError:
            self.store.update_task(task_id, status="NEEDS_RECONCILIATION",
                                   last_paypal_op=f"capture:{auth_id}")
            raise
        try:
            task = self.store.update_task(task_id, capture_id=cap["capture_id"])
        except Exception as e:
            # capture สำเร็จแล้ว (op SUCCEEDED + paypal_ref ครบ) แต่เขียน task ไม่ติด —
            # reconcile จะ adopt จาก op ได้ ห้ามยิง capture ซ้ำ
            self.store.update_task(task_id, capture_id=cap["capture_id"],
                                   status="NEEDS_RECONCILIATION",
                                   last_paypal_op=f"capture:{auth_id}")
            raise EscrowError(
                f"capture may have succeeded but DB write failed -> "
                f"NEEDS_RECONCILIATION (do NOT retry capture): {e}"
            )
        try:
            payout = self._execute_op(task, "payout")
        except EscrowError:
            self.store.update_task(task_id, status="NEEDS_RECONCILIATION",
                                   last_paypal_op=f"payout:{cap['capture_id']}")
            raise
        self.store.update_task(task_id, payout_batch_id=payout.get("batch_id"))
        if payout.get("status") != "SUCCESS":
            # payout ยังไม่สำเร็จ (PENDING/UNCLAIMED/DENIED) — ห้าม mark COMPLETED
            self.store.update_task(task_id, status="NEEDS_RECONCILIATION",
                                   last_paypal_op=f"payout:{payout.get('batch_id')}")
            return {
                "success": True, "task_id": task_id,
                "status": "NEEDS_RECONCILIATION",
                "capture_id": cap["capture_id"], "payout": payout,
                "note": f"payout batch status is {payout.get('status')}, "
                        "not SUCCESS — not marked COMPLETED",
            }
        # B2: batch ได้รับการยืนยันแล้ว — อย่าเพิ่งประกาศ COMPLETED
        # ถาม item-level truth (เงินถึงมือผู้รับจริงไหม) แล้วใช้คำตอบนั้น
        rp = self.refresh_payout(task_id)
        return {
            "success": True, "task_id": task_id,
            "verdict": task["judge_verdict"], "score": task["judge_score"],
            "capture_id": cap["capture_id"], "payout": payout,
            "payout_items": rp["items"],
            "status": rp["task_status"],
        }

    def _settle_reject(self, task_id, verdict):
        # งานไม่ผ่าน: void — เงินไม่ขยับ คืน hold ให้ requester (idempotent)
        task = self._require(task_id)
        try:
            void = self._execute_op(task, "void")
        except EscrowError:
            self.store.update_task(task_id, status="NEEDS_RECONCILIATION",
                                   last_paypal_op=f"void:{task['authorization_id']}")
            raise
        self.store.update_task(task_id, status="REFUNDED")
        return {
            "success": True, "task_id": task_id,
            "verdict": "reject", "score": verdict["score"],
            "reasoning": verdict["reasoning"],
            "void_status": void["status"], "status": "REFUNDED",
        }

    # ---------- B2: item-level payout lifecycle ----------

    def refresh_payout(self, task_id):
        """ดึง item-level truth จาก PayPal แล้วปรับ task status — B2.

        - upsert payout_items ทุก item (จับคู่ด้วย payout_item_id)
        - task status มาจาก aggregate ของ item states เท่านั้น —
          batch SUCCESS อย่างเดียวไม่พอ (เคสจริง: batch SUCCESS แต่ item UNCLAIMED)
        - PayPal ล่ม / response ผิดรูป / ไม่มี items: เก็บ state เดิมไว้;
          ถ้ายังไม่เคยมีข้อมูลดี -> task NEEDS_RECONCILIATION
        - ไม่ raise ยกเว้น task ไม่เจอ (คืน dict มี note เสมอ)
        """
        task = self._require(task_id)
        batch_id = task.get("payout_batch_id")
        actions = []
        items_out = []
        if not batch_id:
            return {"success": False, "task_id": task_id,
                    "task_status": task["status"], "items": [],
                    "note": "no payout_batch_id — nothing to refresh",
                    "actions": actions}
        try:
            b = self.paypal.get_payout_batch(batch_id)
        except (PayPalError, requests.Timeout, requests.ConnectionError) as e:
            actions.append(f"get_payout_batch failed, kept old state: {e}")
            return self._refresh_fallback(task_id, task, batch_id, actions,
                                          "paypal unreachable")
        raw_items = b.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            # response ผิดรูป / ไม่มี items — อย่าเดา เก็บของเดิมไว้
            actions.append("batch response has no items list — kept old state")
            return self._refresh_fallback(task_id, task, batch_id, actions,
                                          "malformed batch response")
        batch_status = b.get("status")
        ours = []
        for raw in raw_items:
            norm = normalize_payout_item(raw)
            norm["batch_id"] = batch_id
            norm["batch_status"] = batch_status or ""
            self.store.upsert_payout_item(task_id, **norm)
            ours.append(norm["item_status"])
            actions.append(
                f"item {norm['payout_item_id'] or '?'}: "
                f"{norm['paypal_status']} -> {norm['item_status']}")
            items_out.append({
                "payout_item_id": norm["payout_item_id"],
                "receiver": norm["receiver"],
                "amount": norm["amount"], "currency": norm["currency"],
                "item_status": norm["item_status"],
                "paypal_status": norm["paypal_status"],
                "error": f"{norm['error_name']}: {norm['error_message']}".strip(": "),
            })
        new_status = aggregate_task_status(ours,
                                           has_capture=bool(task.get("capture_id")))
        actions.append(f"aggregate {ours} -> {new_status}")
        if new_status != task["status"]:
            self.store.update_task(task_id, status=new_status,
                                   last_paypal_op=f"payout:{batch_id}")
            actions.append(f"status {task['status']} -> {new_status}")
        return {"success": True, "task_id": task_id, "task_status": new_status,
                "batch_status": batch_status, "items": items_out,
                "actions": actions}

    def _refresh_fallback(self, task_id, task, batch_id, actions, reason):
        """refresh ล้มเหลว (PayPal ล่ม/timeout/response ผิดรูป/ไม่มี items).

        fail-closed: เก็บ item rows เดิมไว้ (ไม่ลบ) แต่ task ต้องเป็น
        NEEDS_RECONCILIATION เสมอ — ของไม่ชัดห้ามคงสถานะดีไว้
        (refresh รอบหน้าที่สำเร็จจะปรับกลับเองถ้าเงินถึงมือจริง).
        """
        if task["status"] != "NEEDS_RECONCILIATION":
            self.store.update_task(task_id, status="NEEDS_RECONCILIATION",
                                   last_paypal_op=f"payout:{batch_id}")
            actions.append(f"{reason} -> NEEDS_RECONCILIATION (item rows kept)")
        else:
            actions.append(f"{reason} -- already NEEDS_RECONCILIATION")
        existing = self.store.list_payout_items(task_id)
        items_out = [{
            "payout_item_id": r["payout_item_id"], "receiver": r["receiver"],
            "amount": r["amount"], "currency": r["currency"],
            "item_status": r["item_status"], "paypal_status": r["paypal_status"],
            "error": f"{r['error_name'] or ''}: {r['error_message'] or ''}".strip(": "),
        } for r in existing]
        return {"success": False, "task_id": task_id,
                "task_status": "NEEDS_RECONCILIATION",
                "items": items_out, "note": reason, "actions": actions}

    # ---------- B1: idempotent operation executor ----------

    def _execute_op(self, task, op_type, reconcile_mode=False):
        """รัน PayPal operation แบบ idempotent — หัวใจของ B1.

        1. record_intent ลง DB ก่อนยิงเสมอ (crash-safe; key ซ้ำ -> ใช้ row เดิม)
        2. ตรวจ request_hash — key เดิมแต่ธุรกรรมเปลี่ยน -> ปฏิเสธเสียงดัง
        3. SUCCEEDED -> คืนผลเดิม ไม่ยิง PayPal ซ้ำ
           SENT / NEEDS_RECONCILIATION -> ยิงซ้ำได้เฉพาะ reconcile_mode
               (replay ด้วย key เดิม = PayPal คืนผลเดิม ไม่ทำซ้ำ)
           FAILED (ล้มเหลวชัดเจน) -> retry ได้ด้วย key เดิม
        4. mark SENT ก่อนยิง; จบแล้ว mark SUCCEEDED / FAILED /
           NEEDS_RECONCILIATION
           - timeout / connection / 5xx / duplicate-indicator -> NEEDS_RECONCILIATION
             (ห้าม mark FAILED เด็ดขาด — timeout ไม่ใช่หลักฐานว่าล้มเหลว)
           - 400/404/422 ชัดเจน (ไม่ใช่ duplicate) -> FAILED
        """
        task_id = task["id"]
        key = _op_key(task_id, op_type)
        req_hash = _request_hash(op_type, task["amount"], task["currency"],
                                 task.get("worker_email") or "")
        op = self.store.record_intent(
            task_id, op_type, key, req_hash,
            amount=task["amount"], currency=task["currency"],
            recipient=task.get("worker_email") or "",
        )
        if op["request_hash"] != req_hash:
            raise EscrowError(
                f"idempotency key {key} ถูกใช้กับธุรกรรมอื่นแล้ว "
                f"(amount/currency/recipient ไม่ตรง intent เดิม) — ปฏิเสธ"
            )
        st = op["status"]
        if st == "SUCCEEDED":
            # เคยสำเร็จแล้ว — คืนผลเดิม ไม่ยิง PayPal ซ้ำ
            if op_type == "payout" and op.get("paypal_ref"):
                # B2: batch status อาจเปลี่ยน (PENDING -> SUCCESS) และ item
                # states ต้องทันสมัย — route ผ่าน refresh_payout (อ่านอย่างเดียว
                # ไม่สร้างธุรกรรมใหม่)
                try:
                    rp = self.refresh_payout(task_id)
                    return {"replayed": True, "batch_id": op["paypal_ref"],
                            "status": rp.get("batch_status"),
                            "task_status": rp.get("task_status"),
                            "payout_items": rp.get("items", [])}
                except (PayPalError, EscrowError):
                    pass
            return {"replayed": True, **_ref_result(op_type, op)}
        if st == "NEEDS_RECONCILIATION" and not reconcile_mode:
            raise EscrowError(
                f"operation {op_type} อยู่ในสถานะ NEEDS_RECONCILIATION — "
                f"ต้อง reconcile ก่อน retry"
            )
        # SENT: อาจมีอีก thread/process กำลังยิงอยู่ หรือ crash ค้างไว้ —
        # replay ด้วย key เดิมปลอดภัยเสมอ (PayPal คืนผลเดิม ไม่ทำซ้ำ)
        if st != "SENT":
            # INTENDED / FAILED / NEEDS_RECONCILIATION(reconcile) -> SENT
            # (SENT อยู่แล้วไม่ต้อง mark ซ้ำ — replay ปลอดภัย)
            self.store.transition_operation(key, "SENT")
        send = self._sender_for(task, op)
        try:
            result = send(key, op)
        except (requests.Timeout, requests.ConnectionError) as e:
            self.store.transition_operation(key, "NEEDS_RECONCILIATION")
            raise EscrowError(
                f"{op_type}: timeout/connection ผลไม่ชัด "
                f"-> NEEDS_RECONCILIATION: {e}")
        except PayPalError as e:
            if _is_definite_failure(e):
                self.store.transition_operation(key, "FAILED")
                raise EscrowError(f"{op_type}: ล้มเหลวชัดเจน -> FAILED: {e}")
            self.store.transition_operation(key, "NEEDS_RECONCILIATION")
            raise EscrowError(f"{op_type}: ผลคลุมเครือ -> NEEDS_RECONCILIATION: {e}")
        except Exception as e:
            # error อื่น (รวม bug) — ระวังไว้ก่อน ไม่ mark FAILED
            self.store.transition_operation(key, "NEEDS_RECONCILIATION")
            raise EscrowError(
                f"{op_type}: error ไม่คาดคิด -> NEEDS_RECONCILIATION: {e}")
        ref = _extract_ref(op_type, result)
        self.store.transition_operation(key, "SUCCEEDED", paypal_ref=ref)
        return result

    def _sender_for(self, task, op):
        """สร้าง send closure ของแต่ละ op_type — ใช้ค่าตอน intent (กัน task เปลี่ยน)."""
        op_type = op["op_type"]
        if op_type == "capture":
            auth_id = task["authorization_id"]
            return lambda key, _op: self.paypal.capture_authorization(
                auth_id, idempotency_key=key)
        if op_type == "payout":
            recipient = op["recipient"] or task.get("worker_email")
            amount = op["amount"] if op["amount"] is not None else task["amount"]
            currency = op["currency"] or task["currency"]
            note = (f"JudgePay task-{task['id']} human-approved "
                    f"(score {task.get('judge_score')})")
            return lambda key, _op: self.paypal.payout_to_worker(
                recipient, amount, currency, note=note,
                sender_batch_id=key, sender_item_id=f"{key}-item1")
        if op_type == "void":
            auth_id = task["authorization_id"]
            return lambda key, _op: self.paypal.void_authorization(
                auth_id, idempotency_key=key)
        raise EscrowError(f"unknown op_type: {op_type}")

    def _reconcile_op(self, task, op):
        """replay op ที่ค้างด้วย key เดิม — ปลอดภัย ไม่ทำซ้ำ."""
        return self._execute_op(task, op["op_type"], reconcile_mode=True)

    # ---------- 5. หมดเวลา ----------

    def expire_task(self, task_id):
        task = self._require(task_id)
        if task["status"] in ("COMPLETED", "REFUNDED", "EXPIRED"):
            raise EscrowError(f"task {task_id} already {task['status']}")
        if task["status"] == "OPEN" and task["authorization_id"]:
            try:
                self._execute_op(task, "void")
            except EscrowError as e:
                raise EscrowError(f"void failed: {e}")
        self.store.update_task(task_id, status="EXPIRED")
        return {"success": True, "task_id": task_id, "status": "EXPIRED"}

    # ---------- 6. reconcile: เทียบกับ PayPal จริงแล้วซ่อม DB ----------

    def reconcile(self, task_id):
        """อ่านความจริงจาก PayPal แล้วปรับ DB ให้ตรง.

        B1: ไล่ operations ที่ค้างก่อน (INTENDED/SENT/NEEDS_RECONCILIATION) —
        replay ด้วย key เดิม (ปลอดภัย ไม่ทำซ้ำ) แล้วค่อยปรับ task status.
        กฎ task-level เดิมยังคง:
        - capture อยู่ใน DB + payout batch SUCCESS -> COMPLETED
        - authorization ถูก VOIDED และไม่มี capture -> REFUNDED
        - นอกนั้นคง NEEDS_RECONCILIATION ไว้ให้คนตัดสินใจ (ไม่เดา)
        """
        task = self._require(task_id)
        actions = []
        # 1. B1: ซ่อม operations ที่ค้างก่อน — ใช้ key เดิม replay
        for op in self.store.list_operations(task_id):
            if op["status"] in ("INTENDED", "SENT", "NEEDS_RECONCILIATION"):
                try:
                    self._reconcile_op(task, op)
                    new = self.store.get_operation(op["idempotency_key"])
                    actions.append(
                        f"op {op['op_type']}: {op['status']} -> {new['status']}")
                except EscrowError as e:
                    actions.append(f"op {op['op_type']} unresolved: {e}")
                task = self._require(task_id)  # refresh หลัง op อาจเปลี่ยน task
        # 2. backfill refs จาก ops (crash ระหว่าง op สำเร็จกับ update task)
        ops = {o["op_type"]: o for o in self.store.list_operations(task_id)}
        updates = {}
        if not task.get("capture_id") and (ops.get("capture") or {}).get("paypal_ref"):
            updates["capture_id"] = ops["capture"]["paypal_ref"]
        if not task.get("payout_batch_id") and (ops.get("payout") or {}).get("paypal_ref"):
            updates["payout_batch_id"] = ops["payout"]["paypal_ref"]
        if updates:
            task = self.store.update_task(task_id, **updates)
            actions.append(f"backfilled task refs from ops: {sorted(updates)}")
        # 3. task-level truth จาก PayPal
        # B2: payout ใช้ item-level truth (refresh_payout) — batch SUCCESS
        # อย่างเดียวไม่พอ (เคสจริง: batch SUCCESS แต่ item UNCLAIMED)
        auth = None
        if task.get("authorization_id"):
            try:
                auth = self.paypal.get_authorization(task["authorization_id"])
                actions.append(f"authorization {task['authorization_id']} -> {auth.get('status')}")
            except Exception as e:
                actions.append(f"get_authorization failed: {e}")
        new_status = task["status"]
        if task.get("payout_batch_id"):
            try:
                b = self.paypal.get_payout_batch(task["payout_batch_id"])
                actions.append(f"payout batch {task['payout_batch_id']} -> {b.get('status')}")
                if b.get("status") == "SUCCESS":
                    rp = self.refresh_payout(task_id)
                    task = self._require(task_id)
                    new_status = rp["task_status"]
                    actions.append(f"refresh_payout -> {new_status}")
                    for a in rp["actions"]:
                        actions.append(f"  item: {a}")
                else:
                    actions.append("batch not SUCCESS — item truth not queried, "
                                   "kept state (fail-closed)")
            except Exception as e:
                actions.append(f"get_payout_batch failed: {e}")
        elif auth and auth.get("status") == "VOIDED" and not task.get("capture_id"):
            new_status = "REFUNDED"
        if new_status != task["status"]:
            self.store.update_task(task_id, status=new_status)
            actions.append(f"status {task['status']} -> {new_status}")
        else:
            actions.append(f"status unchanged: {task['status']} (needs human decision)")
        return {"success": True, "task_id": task_id,
                "status": new_status, "actions": actions}

    def reconcile_all(self):
        """ไล่ทุก operation ที่ค้างทั่ว DB — ใช้ตอน start / cron / หลัง restart."""
        results = []
        for op in self.store.list_open_operations():
            task = self.store.get_task(op["task_id"])
            if not task:
                results.append({"task_id": op["task_id"], "op": op["op_type"],
                                "status": "skipped", "note": "task not found"})
                continue
            try:
                self._reconcile_op(task, op)
                new = self.store.get_operation(op["idempotency_key"])
                results.append({"task_id": op["task_id"], "op": op["op_type"],
                                "status": new["status"]})
            except EscrowError as e:
                results.append({"task_id": op["task_id"], "op": op["op_type"],
                                "status": "unresolved", "error": str(e)})
        return {"success": True, "results": results}

    # ---------- helpers ----------

    def get_task(self, task_id):
        task = self._require(task_id)
        return {"success": True, **task}

    def list_tasks(self, status=None):
        return {"success": True, "tasks": self.store.list_tasks(status)}

    def _require(self, task_id):
        task = self.store.get_task(task_id)
        if not task:
            raise EscrowError(f"task {task_id} not found")
        return task

    @staticmethod
    def _load_criteria(task):
        try:
            data = json.loads(task.get("acceptance_criteria") or "[]")
            return data if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []

    @staticmethod
    def _is_past_deadline(task):
        if not task.get("deadline_at"):
            return False
        try:
            dl = datetime.fromisoformat(task["deadline_at"])
            return datetime.now(timezone.utc) > dl
        except ValueError:
            return False
