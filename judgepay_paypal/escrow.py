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
from .audit import audit as _audit_append, AuditError  # B5: audit trail
from .logutil import get_logger, log_event  # B5: structured logging
import logging as _logging
from .policy import (
    APPROVE_THRESHOLD, REVIEW_MIN, evaluate_policy, normalize_verdict,
    hash_criteria, criteria_evidence,
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
    if op_type == "refund":
        return result.get("refund_id", "")
    return ""  # void ไม่มี ref ใหม่


def _ref_result(op_type, op):
    """สร้างผลลัพธ์จาก op ที่เคย SUCCEEDED — คืนของเดิม ไม่ยิงใหม่."""
    ref = op.get("paypal_ref") or ""
    if op_type == "capture":
        return {"capture_id": ref, "status": "COMPLETED"}
    if op_type == "payout":
        return {"batch_id": ref, "status": "UNKNOWN"}
    if op_type == "refund":
        # สถานะ refund จริงต้องถาม get_refund — คืน UNKNOWN ให้ caller ไป sync
        return {"refund_id": ref, "status": "UNKNOWN"}
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
        self._log = get_logger("judgepay.escrow")

    # ---------- B5: audit helper ----------

    @staticmethod
    def _short_owner(owner_id):
        """owner_id ย่อสำหรับ log — ไม่จำเป็นต้องเต็ม (เป็น hash อยู่แล้ว)."""
        return f"owner:{(owner_id or '')[:16]}" if owner_id else "operator"

    def _audit(self, action, task_id=None, outcome="ok", correlation_id="",
               details=None, actor=None, fail_closed=False):
        """เขียน audit แบบ fail-closed ตามสั่ง.

        fail_closed=True -> เขียนไม่ได้ raise EscrowError ปฏิเสธ operation
        (ใช้ก่อน money movement). False -> ลง stderr แล้วไปต่อ.
        """
        try:
            return _audit_append(
                self.store, actor or "operator", action,
                target_type="task" if task_id else "",
                target_id=task_id or "", outcome=outcome,
                correlation_id=correlation_id or "",
                details=details or {})
        except AuditError as e:
            if fail_closed:
                raise EscrowError(
                    f"audit write failed — refusing operation: {e}")
            log_event(self._log, _logging.WARNING, "audit_write_failed",
                      str(e), task_id=task_id,
                      correlation_id=correlation_id)
            return None

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
        # B3: freeze เกณฑ์ตั้งแต่สร้าง — judge จะเทียบ hash นี้อีกที
        # (เกณฑ์โดนแก้ย้อนหลัง -> NEEDS_REVIEW)
        self.store.update_task(task["id"],
                               criteria_hash=hash_criteria(acceptance_criteria or []))
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
        # B5: audit — สร้าง task สำเร็จ (ไม่มี secret ใน details)
        self._audit("TASK_CREATED", task_id=task["id"], correlation_id=order["order_id"],
                    details={"amount": amount, "currency": currency,
                             "order_id": order["order_id"],
                             "mock": self.paypal.mock},
                    actor=self._short_owner(owner_id))
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
            self._audit("AUTHORIZE_FAILED", task_id=task_id, outcome="failed",
                        correlation_id=oid, details={"error": type(e).__name__})
            raise EscrowError(str(e))
        task = self.store.update_task(
            task_id, authorization_id=auth["authorization_id"], status="OPEN"
        )
        self._audit("AUTHORIZED", task_id=task_id, correlation_id=oid,
                    details={"authorization_id": auth["authorization_id"],
                             "paypal_status": auth["status"]})
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
        self._audit("WORK_SUBMITTED", task_id=task_id,
                    details={"output_length": len(output or ""),
                             "worker_email_locked": bool(task.get("worker_email_locked"))})
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
        # B3: เกณฑ์ต้อง freeze ตั้งแต่สร้าง — โดนแก้ย้อนหลัง -> ส่งคนตรวจ
        # (ไม่เดา ไม่ตัดสินด้วยเกณฑ์ที่อาจถูกสับเปลี่ยน)
        current_hash = hash_criteria(criteria)
        frozen_hash = task.get("criteria_hash")
        if frozen_hash and frozen_hash != current_hash:
            reason = ("acceptance criteria changed after creation "
                      f"(frozen {frozen_hash[:12]}… != current {current_hash[:12]}…) "
                      "— refusing to judge, human review required")
            self.store.update_task(
                task_id, status="NEEDS_REVIEW",
                judge_score=0, judge_verdict="reject", judge_reasoning=reason,
                judge_version=self.judge.version_info(),
                criteria_evidence=json.dumps(
                    criteria_evidence(criteria, task["worker_output"] or ""),
                    ensure_ascii=False),
            )
            self._audit("JUDGE_VERDICT", task_id=task_id, outcome="refused-criteria-tampered", details={"score": 0, "verdict": "reject"})
            return {"success": True, "task_id": task_id, "verdict": "reject",
                    "score": 0, "reasoning": reason, "status": "NEEDS_REVIEW",
                    "next": "human decision required (criteria tampered)"}
        if not frozen_hash:
            self.store.update_task(task_id, criteria_hash=current_hash)

        fails = evaluate_policy(task["description"], criteria, task["worker_output"])
        if fails:
            verdict = {
                "score": 0,
                "verdict": "reject",
                "reasoning": "policy hard-fail: " + ", ".join(fails)
                             + " — rejected without AI call",
            }
        else:
            crit_text = "; ".join(
                ("[required] " if c.get("required") else "") + c.get("text", "")
                for c in criteria) or "(none)"
            raw = self.judge.evaluate(task["description"],
                                      task["worker_output"] or "",
                                      criteria_text=crit_text)
            # B3: LLM พัง -> fail-closed เป็น NEEDS_REVIEW (ห้าม approve/void จาก error)
            if raw.get("judge_error"):
                reason = str(raw.get("reasoning", "judge error"))
                self._store_judgement(task_id, task, criteria, 0, "reject",
                                      reason)
                self.store.update_task(task_id, status="NEEDS_REVIEW")
                self._audit("JUDGE_VERDICT", task_id=task_id,
                            outcome="judge-error-fail-closed",
                            details={"score": 0, "verdict": "reject"})
                return {"success": True, "task_id": task_id, "verdict": "reject",
                        "score": 0, "reasoning": reason, "status": "NEEDS_REVIEW",
                        "next": "human decision required (judge error, fail-closed)"}
            score = max(0, min(100, int(raw.get("score", 0))))
            v, r = normalize_verdict(score, raw.get("verdict", "reject"),
                                     str(raw.get("reasoning", "")))
            verdict = {"score": score, "verdict": v, "reasoning": r}

        self._store_judgement(task_id, task, criteria,
                              verdict["score"], verdict["verdict"],
                              verdict["reasoning"])

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

        # B5: audit คำตัดสิน (score/verdict อย่างเดียว — ไม่เก็บ output ทั้งก้อน)
        self._audit("JUDGE_VERDICT", task_id=task_id,
                    details={"score": verdict["score"],
                             "verdict": verdict["verdict"],
                             "status": status,
                             "judge_version": self.judge.version_info()})
        return {
            "success": True, "task_id": task_id,
            "verdict": verdict["verdict"], "score": verdict["score"],
            "reasoning": verdict["reasoning"], "status": status, "next": nxt,
        }

    def _store_judgement(self, task_id, task, criteria, score, verdict, reasoning):
        """บันทึกผลตัดสินพร้อม audit trail (B3) — เกณฑ์/หลักฐาน/เวอร์ชัน/input hash."""
        ev = criteria_evidence(criteria, task["worker_output"] or "")
        inp = hashlib.sha256(
            "|".join([task["description"] or "",
                       json.dumps(criteria, sort_keys=True, ensure_ascii=False),
                       task["worker_output"] or ""]).encode("utf-8")
        ).hexdigest()
        self.store.update_task(
            task_id,
            judge_score=score,
            judge_verdict=verdict,
            judge_reasoning=reasoning,
            judge_version=self.judge.version_info(),
            criteria_evidence=json.dumps(ev, ensure_ascii=False),
            input_hash=inp,
        )

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
            judge_version=f"manual:{self.judge.version_info()}",
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
        # B4: มี dispute ค้าง -> ระงับ settlement จนกว่า resolve (fail-closed)
        if task["status"] == "DISPUTED":
            raise EscrowError(
                "dispute open — settlement suspended until the dispute is resolved")
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
        # B5: audit ความตั้งใจ settle (fail-closed) — เขียนไม่ได้ -> ปฏิเสธ
        # ก่อนแตะ claim/เงิน (task คงสถานะเดิม ไม่ค้าง SETTLING)
        self._audit("SETTLE_INTENT", task_id=task_id,
                    correlation_id=f"judgepay-{task_id}-capture",
                    details={"amount": task["amount"], "currency": task["currency"],
                             "recipient": task.get("worker_email"),
                             "human_approve": bool(human_approve)},
                    actor=self._short_owner(task.get("owner_id")),
                    fail_closed=True)
        # A4: atomic claim — ชนะคนเดียวเท่านั้น
        if not self.store.claim_for_settlement(task_id, ("JUDGED", "NEEDS_REVIEW")):
            raise EscrowError("settlement already in progress or completed by another process")
        result = self._settle_approve(task_id, self._require(task_id))
        self._audit("SETTLE_RESULT", task_id=task_id,
                    correlation_id=f"judgepay-{task_id}-capture",
                    details={"status": result.get("status")},
                    actor=self._short_owner(task.get("owner_id")))
        return result

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
        self._audit("JUDGE_VERDICT", task_id=task_id,
                    details={"score": verdict["score"], "verdict": "reject",
                             "status": "REFUNDED",
                             "judge_version": self.judge.version_info()})
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

    # ---------- B4: dispute / refund lifecycle ----------

    def _check_owner(self, task, owner_id):
        """B4: dispute/refund เปลี่ยนสถานะเงิน — ต้องเป็นเจ้าของ task เท่านั้น.

        task ที่มี owner_id ผูกไว้: caller ต้องแสดง owner_id ตรงกัน.
        task เก่าที่ไม่มี owner: อนุญาต (พฤติกรรมเดิมของ escrow methods อื่น).
        """
        want = task.get("owner_id") or ""
        if want and want != (owner_id or ""):
            raise EscrowError("forbidden: not your task")

    def open_dispute(self, task_id, opened_by, reason, evidence_note="",
                     owner_id=""):
        """เปิดข้อพิพาท — task -> DISPUTED, settlement ถูกระงับทันที.

        ใครเปิดก็ได้ (requester/worker/operator) แต่ต้องเป็นเจ้าของ task.
        AI judge ไม่เกี่ยว — dispute ตัดสินโดยมนุษย์เท่านั้น.
        """
        task = self._require(task_id)
        self._check_owner(task, owner_id)
        reason = (reason or "").strip()
        if not reason:
            raise EscrowError("dispute reason is required")
        if self.store.get_open_dispute(task_id):
            raise EscrowError(
                f"task {task_id} already has an open dispute — "
                "resolve it before opening another")
        d = self.store.create_dispute(task_id, (opened_by or "operator").strip(),
                                      reason)
        self.store.update_task(task_id, status="DISPUTED",
                               last_paypal_op=f"dispute:{d['id']}")
        self.store.ledger_append(
            task_id, "DISPUTE_OPENED", ref=str(d["id"]),
            note=f"by {d['opened_by']}: {reason[:200]}")
        if (evidence_note or "").strip():
            self.add_dispute_evidence(task_id, d["opened_by"],
                                      evidence_note.strip(), owner_id=owner_id)
            d = self.store.get_dispute(task_id)
        self._audit("DISPUTE_OPENED", task_id=task_id,
                    correlation_id=f"dispute-{d['id']}",
                    details={"dispute_id": d["id"], "reason": reason[:200]},
                    actor=self._short_owner(owner_id or task.get("owner_id")))
        return {"success": True, "task_id": task_id,
                "dispute_id": d["id"], "dispute_status": d["status"],
                "task_status": "DISPUTED",
                "next": "settlement suspended until resolve_dispute"}

    def add_dispute_evidence(self, task_id, by, note, owner_id=""):
        """แนบหลักฐาน — ได้เฉพาะตอน dispute ยังเปิดอยู่; ตรวจสิทธิ์เจ้าของ."""
        task = self._require(task_id)
        self._check_owner(task, owner_id)
        d = self.store.get_open_dispute(task_id)
        if not d:
            raise EscrowError(f"task {task_id} has no open dispute")
        if not (note or "").strip():
            raise EscrowError("evidence note is required")
        try:
            ev = json.loads(d["evidence"] or "[]")
            if not isinstance(ev, list):
                ev = []
        except (ValueError, TypeError):
            ev = []
        ev.append({"by": (by or "").strip() or "unknown",
                   "note": note.strip()[:2000], "at": utcnow()})
        fields = {"evidence": json.dumps(ev, ensure_ascii=False)}
        if d["status"] == "OPEN":
            fields["status"] = "UNDER_REVIEW"
        self.store.update_dispute(d["id"], **fields)
        self._audit("DISPUTE_EVIDENCE", task_id=task_id,
                    correlation_id=f"dispute-{d['id']}",
                    details={"dispute_id": d["id"],
                             "evidence_count": len(ev)},
                    actor=self._short_owner(owner_id or task.get("owner_id")))
        return {"success": True, "task_id": task_id, "dispute_id": d["id"],
                "dispute_status": fields.get("status", d["status"]),
                "evidence_count": len(ev)}

    def resolve_dispute(self, task_id, reviewer, decision, reason="",
                        owner_id=""):
        """มนุษย์ตัดสิน dispute — AI เป็นผู้ตัดสินไม่ได้ (ไม่มี judge call ในนี้).

        upheld = ข้อพิพาทมีมูล (เช่น ต้องคืนเงิน) -> task กลับ NEEDS_REVIEW
        rejected = ข้อพิพาทไม่มีมูล -> task กลับ NEEDS_REVIEW
        ทั้งสองทางต้องให้คนตัดสินใจขั้นต่อไปเอง — ไม่ auto-settle ไม่ auto-refund.
        """
        task = self._require(task_id)
        self._check_owner(task, owner_id)
        if decision not in ("upheld", "rejected"):
            raise EscrowError("decision must be 'upheld' or 'rejected'")
        reviewer = (reviewer or "").strip()
        if not reviewer:
            raise EscrowError("reviewer is required")
        d = self.store.get_open_dispute(task_id)
        if not d:
            raise EscrowError(f"task {task_id} has no open dispute to resolve")
        new_status = ("RESOLVED_UPHELD" if decision == "upheld"
                      else "RESOLVED_REJECTED")
        self.store.update_dispute(
            d["id"], status=new_status, reviewer=reviewer,
            decision=decision, decided_at=utcnow())
        # ออกจาก DISPUTED -> NEEDS_REVIEW ให้คนตัดสินใจขั้นต่อไป (ไม่เดา)
        self.store.update_task(task_id, status="NEEDS_REVIEW",
                               last_paypal_op=f"dispute:{d['id']}:{decision}")
        self.store.ledger_append(
            task_id, "DISPUTE_CLOSED", ref=str(d["id"]),
            note=f"{decision} by {reviewer}: {(reason or '').strip()[:200]}")
        self._audit("DISPUTE_RESOLVED", task_id=task_id,
                    correlation_id=f"dispute-{d['id']}",
                    details={"dispute_id": d["id"], "decision": decision,
                             "reviewer": reviewer},
                    actor=self._short_owner(owner_id or task.get("owner_id")))
        return {"success": True, "task_id": task_id, "dispute_id": d["id"],
                "decision": decision, "dispute_status": new_status,
                "task_status": "NEEDS_REVIEW",
                "next": "human decides next step (settle / refund / close)"}

    def refund_capture(self, task_id, amount=None, reason="", human_confirm=False,
                       dispute_resolution=False, owner_id=""):
        """คืนเงินจาก capture — ต้อง human_confirm ชัดแจ้งทุกครั้ง (B4).

        กฎ:
        - ตรวจ capture จริงจาก PayPal ก่อนยิง (status ต้อง COMPLETED/PARTIALLY_REFUNDED)
        - ยอดคืน <= ยอดคงเหลือคืนได้ (capture - REFUND_SUCCEEDED ใน ledger)
        - dispute ยังเปิดอยู่ -> ต้อง resolve_dispute ก่อนเสมอ (refund ไม่ใช่
          ทางลัดข้าม dispute); dispute_resolution=True ใช้บันทึกว่า refund นี้
          เป็นการระงับข้อพิพาทที่ resolve แบบ upheld แล้วเท่านั้น
        - ใช้ _execute_op (idempotent, key ต่อครั้ง: judgepay-{task}-refund-{seq})
        - timeout/ผลคลุมเครือ -> NEEDS_RECONCILIATION (ห้ามสรุปว่าล้มเหลว)
        - คืนเต็ม -> task REFUNDED; คืนบางส่วน -> task คงสถานะเดิม
        - นี่คืนจาก capture เท่านั้น — ดึงเงินจากผู้รับ payout คืนไม่ได้
        """
        task = self._require(task_id)
        self._check_owner(task, owner_id)
        if not human_confirm:
            raise EscrowError(
                "refund requires explicit human confirmation "
                "(human_confirm=True) — refusing")
        capture_id = task.get("capture_id")
        if not capture_id:
            raise EscrowError(
                f"task {task_id} has no capture — nothing to refund")
        open_d = self.store.get_open_dispute(task_id)
        if open_d:
            raise EscrowError(
                f"dispute #{open_d['id']} still {open_d['status']} — "
                "resolve it before refund")
        resolution_note = ""
        if dispute_resolution:
            rd = self.store.get_dispute(task_id)
            if not rd or rd["status"] != "RESOLVED_UPHELD":
                raise EscrowError(
                    "dispute_resolution=True requires a dispute resolved "
                    "as 'upheld'")
            resolution_note = f" (dispute #{rd['id']} resolution)"
        # กันยิง refund ซ้อน: ถ้ามี refund op ที่ยังไม่จบ (ค้าง/ผลคลุมเครือ)
        # ห้ามเปิดอันใหม่ด้วย key ใหม่ — ต้อง reconcile อันเก่าให้จบก่อน
        # (ไม่งั้นอาจคืนซ้ำ: timeout ครั้งแรกอาจสำเร็จที่ PayPal แล้ว)
        # เช็กก่อนถาม capture — สถานะ op ของเราสำคัญกว่าสถานะ PayPal ตอนนี้
        for o in self.store.list_operations(task_id):
            if o["op_type"] == "refund" and o["status"] in (
                    "INTENDED", "SENT", "NEEDS_RECONCILIATION"):
                raise EscrowError(
                    f"refund {o['idempotency_key']} is {o['status']} — "
                    "run reconcile before requesting another refund")
        # ตรวจ capture จริงก่อนยิง — ไม่เชื่อ DB อย่างเดียว
        try:
            cap = self.paypal.get_capture(capture_id)
        except (PayPalError, requests.Timeout, requests.ConnectionError) as e:
            raise EscrowError(f"cannot verify capture before refund: {e}")
        cap_status = (cap.get("status") or "").upper()
        cap_amt = float((cap.get("amount") or {}).get("value") or 0)
        cap_cur = ((cap.get("amount") or {}).get("currency")
                   or task.get("currency") or "USD").upper()
        if (task.get("currency") or "").upper() != cap_cur:
            raise EscrowError(
                f"currency mismatch: capture {cap_cur} != task "
                f"{task.get('currency')} — refusing")
        refunded = self.store.ledger_sum(task_id, ["REFUND_SUCCEEDED"])
        refundable = round(cap_amt - refunded, 2)
        if refundable <= 0:
            # คืนครบแล้ว (ledger ตรงกับ PayPal) — ไม่ต้องดูสถานะ capture
            raise EscrowError("nothing left to refund on this capture")
        if cap_status not in ("COMPLETED", "PARTIALLY_REFUNDED"):
            # ledger บอกว่าเหลือคืนได้ แต่ PayPal บอกสถานะไม่เอื้อ —
            # ไม่ตรงกัน -> อย่าคืน ให้ reconcile ก่อน (fail-closed)
            raise EscrowError(
                f"capture status is {cap_status} but ledger shows "
                f"{refundable:.2f} {cap_cur} refundable — not refundable; "
                "NEEDS_RECONCILIATION (run reconcile)")
        if amount is None:
            amount = refundable
        amount = float(amount)
        if amount <= 0 or amount > refundable + 1e-9:
            raise EscrowError(
                f"refund amount {amount:.2f} invalid — refundable is "
                f"{refundable:.2f} {cap_cur}")
        # stable key ต่อครั้งที่ขอ refund — retry ด้วย key เดิมปลอดภัย
        seq = sum(1 for o in self.store.list_operations(task_id)
                  if o["op_type"] == "refund") + 1
        key = f"judgepay-{task_id}-refund-{seq}"
        # B5: audit ความตั้งใจ refund (fail-closed) — เขียนไม่ได้ -> ปฏิเสธก่อนเงินขยับ
        self._audit("REFUND_INTENT", task_id=task_id, correlation_id=key,
                    details={"amount": amount, "currency": cap_cur,
                             "refundable": refundable,
                             "reason": (reason or "")[:200],
                             "dispute_resolution": bool(dispute_resolution)},
                    actor=self._short_owner(owner_id or task.get("owner_id")),
                    fail_closed=True)
        try:
            result = self._execute_op(
                task, "refund",
                op_overrides={"idempotency_key": key, "amount": amount,
                              "currency": cap_cur, "recipient": ""})
        except EscrowError as e:
            # provider ปฏิเสธชัดเจน (op FAILED) -> บันทึกไว้ อย่าให้หาย
            op = self.store.get_operation(key)
            if op and op["status"] == "FAILED" and not self.store.ledger_has(
                    task_id, "REFUND_FAILED", key):
                self.store.ledger_append(
                    task_id, "REFUND_FAILED", amount=amount,
                    currency=cap_cur, ref=key,
                    note=f"PayPal refused: {str(e)[:200]}{resolution_note}")
            raise
        applied = self._apply_refund_result(
            task_id, result, cap_amt=cap_amt, cap_cur=cap_cur,
            extra_note=resolution_note)
        self._audit("REFUND_RESULT", task_id=task_id, correlation_id=key,
                    outcome=applied.get("status", "unknown").lower(),
                    details={"refund_id": applied.get("refund_id") or "",
                             "amount": applied.get("amount"),
                             "currency": applied.get("currency")},
                    actor=self._short_owner(owner_id or task.get("owner_id")))
        return applied

    def _apply_refund_result(self, task_id, result, cap_amt=None, cap_cur="",
                             extra_note="", actions=None):
        """ปรับ ledger/task ตามผล refund — idempotent (เรียกซ้ำไม่เขียนซ้ำ).

        COMPLETED -> REFUND_SUCCEEDED (+ task REFUNDED ถ้าคืนครบ)
        PENDING    -> REFUND_INTENT + task NEEDS_RECONCILIATION (รอ sync)
        FAILED/CANCELLED/DENIED -> REFUND_FAILED
        UNKNOWN    -> ถาม get_refund ก่อน; ยังไม่รู้ -> NEEDS_RECONCILIATION
        """
        task = self._require(task_id)
        refund_id = result.get("refund_id") or ""
        status = (result.get("status") or "UNKNOWN").upper()
        amt = result.get("amount") or {}
        try:
            ramt = float(amt.get("value") or 0)
        except (TypeError, ValueError):
            ramt = 0.0
        rcur = (amt.get("currency") or cap_cur or task.get("currency") or "")
        if status == "UNKNOWN" and refund_id:
            # replay ของเดิม — ถามสถานะจริงจาก PayPal
            try:
                r = self.paypal.get_refund(refund_id)
                status = (r.get("status") or "UNKNOWN").upper()
                ramt = float((r.get("amount") or {}).get("value") or ramt)
                rcur = (r.get("amount") or {}).get("currency") or rcur
                if actions is not None:
                    actions.append(f"get_refund {refund_id} -> {status}")
            except (PayPalError, requests.Timeout,
                    requests.ConnectionError) as e:
                if actions is not None:
                    actions.append(f"get_refund {refund_id} failed: {e}")
        note = (extra_note or "").strip()
        if status == "COMPLETED":
            if refund_id and not self.store.ledger_has(
                    task_id, "REFUND_SUCCEEDED", refund_id):
                self.store.ledger_append(
                    task_id, "REFUND_SUCCEEDED", amount=ramt, currency=rcur,
                    ref=refund_id, note=f"PayPal refund completed{note}")
            total = cap_amt if cap_amt else float(task.get("amount") or 0)
            refunded = self.store.ledger_sum(task_id, ["REFUND_SUCCEEDED"])
            if refunded + 1e-9 >= total and total > 0:
                self.store.update_task(task_id, status="REFUNDED",
                                       last_paypal_op=f"refund:{refund_id}")
                task_status = "REFUNDED"
            else:
                task_status = self._require(task_id)["status"]
            return {"success": True, "task_id": task_id, "refund_id": refund_id,
                    "status": "COMPLETED", "amount": ramt, "currency": rcur,
                    "refunded_total": round(refunded, 2),
                    "task_status": task_status,
                    "note": "money returned to payer's PayPal balance" + note}
        if status == "PENDING":
            if refund_id and not self.store.ledger_has(
                    task_id, "REFUND_INTENT", refund_id):
                self.store.ledger_append(
                    task_id, "REFUND_INTENT", amount=ramt, currency=rcur,
                    ref=refund_id,
                    note=f"PayPal accepted refund, pending completion{note}")
            self.store.update_task(task_id, status="NEEDS_RECONCILIATION",
                                   last_paypal_op=f"refund:{refund_id}")
            return {"success": True, "task_id": task_id, "refund_id": refund_id,
                    "status": "PENDING", "amount": ramt, "currency": rcur,
                    "task_status": "NEEDS_RECONCILIATION",
                    "note": "refund pending at PayPal — reconcile later" + note}
        if status in ("FAILED", "CANCELLED", "DENIED"):
            if refund_id and not self.store.ledger_has(
                    task_id, "REFUND_FAILED", refund_id):
                self.store.ledger_append(
                    task_id, "REFUND_FAILED", amount=ramt, currency=rcur,
                    ref=refund_id,
                    note=f"PayPal refund {status.lower()}{note}")
            return {"success": True, "task_id": task_id, "refund_id": refund_id,
                    "status": status, "amount": ramt, "currency": rcur,
                    "task_status": self._require(task_id)["status"],
                    "note": f"refund {status.lower()} — no money moved" + note}
        # UNKNOWN / shape แปลก — อย่าเดา
        self.store.update_task(task_id, status="NEEDS_RECONCILIATION",
                               last_paypal_op=f"refund:{refund_id or 'unknown'}")
        return {"success": False, "task_id": task_id, "refund_id": refund_id,
                "status": "UNKNOWN", "task_status": "NEEDS_RECONCILIATION",
                "note": "refund result unclear — reconcile before retry" + note}

    def cancel_payout_item(self, task_id, payout_item_id, owner_id=""):
        """ยกเลิก payout item ที่ผู้รับยังไม่รับ (UNCLAIMED) — B4.

        เงินกลับเข้า PayPal balance ของผู้ส่ง ไม่ใช่กลับเข้า capture.
        item ที่จ่ายถึงมือแล้ว (SUCCESS/PAID) หรือสถานะอื่น -> ปฏิเสธเสียงดัง
        (เรียกเงินคืนจากผู้รับไม่ได้).
        """
        task = self._require(task_id)
        self._check_owner(task, owner_id)
        if not payout_item_id:
            raise EscrowError("payout_item_id is required")
        # ถาม item truth ล่าสุดก่อน — ไม่เชื่อสถานะเก่าใน DB
        rp = self.refresh_payout(task_id)
        item = next((i for i in rp.get("items", [])
                     if i.get("payout_item_id") == payout_item_id), None)
        if not item:
            raise EscrowError(
                f"payout item {payout_item_id} not found for task {task_id}")
        if item["item_status"] != "UNCLAIMED":
            raise EscrowError(
                f"cannot cancel payout item in status {item['item_status']} "
                f"(PayPal: {item['paypal_status']}) — only UNCLAIMED items "
                "can be cancelled; money already paid cannot be recalled")
        try:
            res = self.paypal.cancel_payout_item(payout_item_id)
        except (PayPalError, requests.Timeout, requests.ConnectionError) as e:
            raise EscrowError(f"cancel payout item failed: {e}")
        raw_status = (res.get("transaction_status") or "CANCELED").upper()
        # normalize ผ่าน map เดียวกับ B2 (CANCELED->CANCELED; ถ้า PayPal คืน
        # RETURNED ก็รองรับ — เงินกลับผู้ส่งเหมือนกัน)
        from .payout_status import normalize_item_status
        our_status = normalize_item_status(raw_status)
        self.store.upsert_payout_item(
            task_id, payout_item_id=payout_item_id,
            transaction_id=item.get("transaction_id", ""),
            sender_item_id="", receiver=item.get("receiver", ""),
            amount=item.get("amount", 0), currency=item.get("currency", ""),
            item_status=our_status, paypal_status=raw_status,
            batch_id=task.get("payout_batch_id") or "",
            batch_status=rp.get("batch_status") or "")
        self.store.ledger_append(
            task_id, "PAYOUT_ITEM_CANCELLED", amount=item.get("amount", 0),
            currency=item.get("currency", ""), ref=payout_item_id,
            note="unclaimed payout cancelled — money returns to sender's "
                 "PayPal balance, NOT to the capture")
        return {"success": True, "task_id": task_id,
                "payout_item_id": payout_item_id,
                "item_status": our_status, "paypal_status": raw_status,
                "note": "money returns to sender's PayPal balance, "
                        "not to the capture"}

    # ---------- B1: idempotent operation executor ----------

    def _execute_op(self, task, op_type, reconcile_mode=False, op_overrides=None):
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

        op_overrides (B4): {"idempotency_key", "amount", "currency", "recipient"} —
        ใช้เมื่อ op หนึ่ง task มีได้หลายครั้ง (เช่น refund หลายรอบ) แต่ละครั้ง
        ต้องมี key ของตัวเอง. ไม่ส่ง = derive แบบเดิม (B1 ไม่เปลี่ยน).
        """
        task_id = task["id"]
        ov = op_overrides or {}
        key = ov.get("idempotency_key") or _op_key(task_id, op_type)
        amount = ov.get("amount", task["amount"])
        currency = ov.get("currency", task["currency"])
        recipient = ov.get("recipient", task.get("worker_email") or "")
        req_hash = _request_hash(op_type, amount if amount is not None else 0,
                                 currency, recipient)
        op = self.store.record_intent(
            task_id, op_type, key, req_hash,
            amount=amount, currency=currency, recipient=recipient,
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
        # B5: audit ความตั้งใจยิง provider (fail-closed) — เขียนไม่ได้ -> ปฏิเสธก่อนเงินขยับ
        self._audit("OP_INTENT", task_id=task_id, correlation_id=key,
                    details={"op_type": op_type, "amount": amount,
                             "currency": currency, "reconcile_mode": reconcile_mode},
                    actor=self._short_owner(task.get("owner_id")),
                    fail_closed=True)
        send = self._sender_for(task, op)
        try:
            result = send(key, op)
        except (requests.Timeout, requests.ConnectionError) as e:
            self.store.transition_operation(key, "NEEDS_RECONCILIATION")
            self._audit("OP_RESULT", task_id=task_id, correlation_id=key,
                        outcome="needs_reconciliation",
                        details={"op_type": op_type, "reason": "timeout"})
            raise EscrowError(
                f"{op_type}: timeout/connection ผลไม่ชัด "
                f"-> NEEDS_RECONCILIATION: {e}")
        except PayPalError as e:
            if _is_definite_failure(e):
                self.store.transition_operation(key, "FAILED")
                self._audit("OP_RESULT", task_id=task_id, correlation_id=key,
                            outcome="failed",
                            details={"op_type": op_type})
                raise EscrowError(f"{op_type}: ล้มเหลวชัดเจน -> FAILED: {e}")
            self.store.transition_operation(key, "NEEDS_RECONCILIATION")
            self._audit("OP_RESULT", task_id=task_id, correlation_id=key,
                        outcome="needs_reconciliation",
                        details={"op_type": op_type, "reason": "ambiguous"})
            raise EscrowError(f"{op_type}: ผลคลุมเครือ -> NEEDS_RECONCILIATION: {e}")
        except Exception as e:
            # error อื่น (รวม bug) — ระวังไว้ก่อน ไม่ mark FAILED
            self.store.transition_operation(key, "NEEDS_RECONCILIATION")
            self._audit("OP_RESULT", task_id=task_id, correlation_id=key,
                        outcome="needs_reconciliation",
                        details={"op_type": op_type, "reason": "unexpected"})
            raise EscrowError(
                f"{op_type}: error ไม่คาดคิด -> NEEDS_RECONCILIATION: {e}")
        ref = _extract_ref(op_type, result)
        self.store.transition_operation(key, "SUCCEEDED", paypal_ref=ref)
        self._audit("OP_RESULT", task_id=task_id, correlation_id=key,
                    details={"op_type": op_type, "paypal_ref": ref or ""})
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
        if op_type == "refund":
            # B4: ใช้ค่าตอน intent (เก็บใน op row) — กัน task เปลี่ยนระหว่างทาง
            capture_id = task.get("capture_id")
            amount = op["amount"]
            currency = op["currency"] or task["currency"]
            return lambda key, _op: self.paypal.refund_capture(
                capture_id, amount=amount,
                currency=currency, idempotency_key=key,
                note=f"JudgePay task-{task['id']} refund")
        raise EscrowError(f"unknown op_type: {op_type}")

    def _reconcile_op(self, task, op):
        """replay op ที่ค้างด้วย key เดิม — ปลอดภัย ไม่ทำซ้ำ."""
        if op["op_type"] == "refund":
            # B4: refund มีหลาย key ต่อ task — ใช้ key/amount ของ op ตัวนั้นเอง
            return self._execute_op(
                task, "refund", reconcile_mode=True,
                op_overrides={"idempotency_key": op["idempotency_key"],
                              "amount": op["amount"],
                              "currency": op["currency"],
                              "recipient": op["recipient"] or ""})
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
                    res = self._reconcile_op(task, op)
                    new = self.store.get_operation(op["idempotency_key"])
                    actions.append(
                        f"op {op['op_type']}: {op['status']} -> {new['status']}")
                    # B4: refund ที่ replay สำเร็จ -> sync สถานะจริงจาก PayPal
                    if (op["op_type"] == "refund"
                            and new["status"] == "SUCCEEDED"
                            and new.get("paypal_ref")):
                        self._apply_refund_result(
                            task_id, {**res,
                                      "refund_id": new["paypal_ref"]},
                            actions=actions)
                        actions.append(
                            f"refund {new['paypal_ref']} synced")
                except EscrowError as e:
                    actions.append(f"op {op['op_type']} unresolved: {e}")
                task = self._require(task_id)  # refresh หลัง op อาจเปลี่ยน task
        # 1b. B4: sync refund ที่ op SUCCEEDED แล้วแต่ ledger ยังไม่บันทึก
        # (เช่น crash ระหว่าง op สำเร็จกับเขียน ledger, หรือ refund ค้าง PENDING)
        for op in self.store.list_operations(task_id):
            if (op["op_type"] == "refund" and op["status"] == "SUCCEEDED"
                    and op.get("paypal_ref")
                    and not self.store.ledger_has(
                        task_id, "REFUND_SUCCEEDED", op["paypal_ref"])):
                try:
                    r = self.paypal.get_refund(op["paypal_ref"])
                    st = (r.get("status") or "").upper()
                    actions.append(
                        f"refund {op['paypal_ref']} at PayPal -> {st}")
                    if st == "COMPLETED":
                        self._apply_refund_result(
                            task_id,
                            {"refund_id": op["paypal_ref"], "status": "COMPLETED",
                             "amount": r.get("amount", {})},
                            actions=actions)
                    elif st in ("FAILED", "CANCELLED", "DENIED"):
                        self._apply_refund_result(
                            task_id,
                            {"refund_id": op["paypal_ref"], "status": st,
                             "amount": r.get("amount", {})},
                            actions=actions)
                    else:
                        actions.append(
                            f"refund {op['paypal_ref']} still {st} — "
                            "kept NEEDS_RECONCILIATION, no new refund sent")
                        self.store.update_task(
                            task_id, status="NEEDS_RECONCILIATION",
                            last_paypal_op=f"refund:{op['paypal_ref']}")
                except (PayPalError, requests.Timeout,
                        requests.ConnectionError) as e:
                    actions.append(f"get_refund {op['paypal_ref']} failed: {e}")
        # 1c. B4: dispute รายงานอย่างเดียว — ห้าม auto-close/auto-resolve
        d = self.store.get_open_dispute(task_id)
        if d:
            actions.append(
                f"dispute #{d['id']} still {d['status']} (by {d['opened_by']}) — "
                "no auto-close; needs human resolve_dispute")
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
        # B5: audit สรุป reconcile — actions ถูก truncate โดย scrub อยู่แล้ว
        self._audit("RECONCILE", task_id=task_id,
                    details={"final_status": new_status,
                             "actions": actions[-20:]})
        return {"success": True, "task_id": task_id,
                "status": new_status, "actions": actions}

    def apply_webhook_refund(self, task_id, resource):
        """ยืนยัน refund จาก webhook — sync ledger แบบ idempotent (B5).

        เรียก get_refund แล้วยัดผ่าน _apply_refund_result (idempotent เหมือน B4).
        ห้ามสั่ง refund ใหม่จาก webhook เด็ดขาด — ทำได้แค่ confirm เท่านั้น.
        คืน list ของผลที่ sync สำเร็จ (ว่าง = ไม่มีอะไรต้อง sync).
        """
        task = self._require(task_id)
        synced = []
        for op in self.store.list_operations(task_id):
            if op["op_type"] != "refund" or not op.get("paypal_ref"):
                continue
            rid = op["paypal_ref"]
            if self.store.ledger_has(task_id, "REFUND_SUCCEEDED", rid):
                continue
            try:
                r = self.paypal.get_refund(rid)
            except (PayPalError, Exception) as e:
                log_event(self._log, _logging.WARNING, "webhook_refund_lookup_failed",
                          f"get_refund {rid}: {type(e).__name__}", task_id=task_id)
                continue
            if (r.get("status") or "").upper() != "COMPLETED":
                continue
            res = self._apply_refund_result(
                task_id, {"refund_id": rid, "status": "COMPLETED",
                          "amount": r.get("amount", {})})
            synced.append(res)
            self._audit("WEBHOOK_REFUND_SYNCED", task_id=task_id,
                        correlation_id=rid,
                        details={"refund_id": rid,
                                 "amount": res.get("amount")})
        return synced

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
