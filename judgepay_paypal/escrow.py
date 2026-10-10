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
import time
from datetime import datetime, timedelta, timezone

from .store import TaskStore, utcnow
from .paypal_client import PayPalClient, PayPalError
from .judge import AIJudge
from .policy import (
    APPROVE_THRESHOLD, REVIEW_MIN, evaluate_policy, normalize_verdict,
)

# กันงานขยะ: output ยาวเกินนี้ไม่รับ
MAX_OUTPUT_LEN = 20000
# อายุ confirmation token (วินาที)
CONFIRM_TOKEN_TTL = 1800


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
        order = self.paypal.create_order(
            amount=amount,
            currency=currency,
            description=f"JudgePay escrow: {description[:80]}",
        )
        deadline_at = (datetime.now(timezone.utc)
                       + timedelta(hours=deadline_hours)).isoformat()
        criteria_json = json.dumps(acceptance_criteria or [], ensure_ascii=False)
        task = self.store.create_task(description, amount, currency, deadline_at,
                                      requester_email, owner_id, criteria_json)
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
        """ปล่อยเงิน: capture ก่อน แล้ว payout. ผลคลุมเครือ -> NEEDS_RECONCILIATION."""
        auth_id = task["authorization_id"]
        recipient = task["worker_email"]  # มาจาก DB ที่ล็อกไว้เท่านั้น
        try:
            cap = self.paypal.capture_authorization(auth_id)
        except Exception as e:
            self.store.update_task(task_id, status="NEEDS_RECONCILIATION",
                                   last_paypal_op=f"capture:{auth_id}")
            raise EscrowError(f"capture failed or ambiguous -> NEEDS_RECONCILIATION: {e}")
        try:
            task = self.store.update_task(task_id, capture_id=cap["capture_id"])
        except Exception as e:
            # capture อาจสำเร็จแล้วแต่ DB เขียนไม่ติด — ห้ามยิง capture ซ้ำ
            self.store.update_task(task_id, capture_id=cap["capture_id"],
                                   status="NEEDS_RECONCILIATION",
                                   last_paypal_op=f"capture:{auth_id}")
            raise EscrowError(
                f"capture may have succeeded but DB write failed -> "
                f"NEEDS_RECONCILIATION (do NOT retry capture): {e}"
            )
        try:
            payout = self.paypal.payout_to_worker(
                recipient, task["amount"], task["currency"],
                note=f"JudgePay task-{task_id} human-approved (score {task['judge_score']})",
            )
        except Exception as e:
            self.store.update_task(task_id, status="NEEDS_RECONCILIATION",
                                   last_paypal_op=f"payout:{cap['capture_id']}")
            raise EscrowError(f"payout failed or ambiguous -> NEEDS_RECONCILIATION: {e}")
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
        self.store.update_task(task_id, status="COMPLETED")
        return {
            "success": True, "task_id": task_id,
            "verdict": task["judge_verdict"], "score": task["judge_score"],
            "capture_id": cap["capture_id"], "payout": payout,
            "status": "COMPLETED",
        }

    def _settle_reject(self, task_id, verdict):
        # งานไม่ผ่าน: void — เงินไม่ขยับ คืน hold ให้ requester
        task = self._require(task_id)
        try:
            void = self.paypal.void_authorization(task["authorization_id"])
        except Exception as e:
            self.store.update_task(task_id, status="NEEDS_RECONCILIATION",
                                   last_paypal_op=f"void:{task['authorization_id']}")
            raise EscrowError(f"void failed or ambiguous -> NEEDS_RECONCILIATION: {e}")
        self.store.update_task(task_id, status="REFUNDED")
        return {
            "success": True, "task_id": task_id,
            "verdict": "reject", "score": verdict["score"],
            "reasoning": verdict["reasoning"],
            "void_status": void["status"], "status": "REFUNDED",
        }

    # ---------- 5. หมดเวลา ----------

    def expire_task(self, task_id):
        task = self._require(task_id)
        if task["status"] in ("COMPLETED", "REFUNDED", "EXPIRED"):
            raise EscrowError(f"task {task_id} already {task['status']}")
        if task["status"] == "OPEN" and task["authorization_id"]:
            try:
                self.paypal.void_authorization(task["authorization_id"])
            except PayPalError as e:
                raise EscrowError(f"void failed: {e}")
        self.store.update_task(task_id, status="EXPIRED")
        return {"success": True, "task_id": task_id, "status": "EXPIRED"}

    # ---------- 6. reconcile: เทียบกับ PayPal จริงแล้วซ่อม DB ----------

    def reconcile(self, task_id):
        """อ่านความจริงจาก PayPal แล้วปรับ DB ให้ตรง.

        กฎ:
        - capture อยู่ใน DB + payout batch SUCCESS -> COMPLETED
        - authorization ถูก VOIDED และไม่มี capture -> REFUNDED
        - นอกนั้นคง NEEDS_RECONCILIATION ไว้ให้คนตัดสินใจ (ไม่เดา)
        """
        task = self._require(task_id)
        actions = []
        auth = batch = None
        if task.get("authorization_id"):
            try:
                auth = self.paypal.get_authorization(task["authorization_id"])
                actions.append(f"authorization {task['authorization_id']} -> {auth.get('status')}")
            except Exception as e:
                actions.append(f"get_authorization failed: {e}")
        if task.get("payout_batch_id"):
            try:
                batch = self.paypal.get_payout_batch(task["payout_batch_id"])
                actions.append(f"payout batch {task['payout_batch_id']} -> {batch.get('status')}")
            except Exception as e:
                actions.append(f"get_payout_batch failed: {e}")
        new_status = task["status"]
        if task.get("capture_id") and batch and batch.get("status") == "SUCCESS":
            new_status = "COMPLETED"
        elif auth and auth.get("status") == "VOIDED" and not task.get("capture_id"):
            new_status = "REFUNDED"
        if new_status != task["status"]:
            self.store.update_task(task_id, status=new_status)
            actions.append(f"status {task['status']} -> {new_status}")
        else:
            actions.append(f"status unchanged: {task['status']} (needs human decision)")
        return {"success": True, "task_id": task_id,
                "status": new_status, "actions": actions}

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
