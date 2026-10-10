"""EscrowService — state machine หลักของ JudgePay PayPal Edition.

โฟลว์:
  create_task      -> สร้าง PayPal order (intent=AUTHORIZE), สถานะ AWAITING_APPROVAL
  confirm_authorization -> buyer อนุมัติแล้ว -> authorize ล็อกเงิน, สถานะ OPEN
  submit_work      -> worker ส่งงาน, สถานะ SUBMITTED
  judge_and_settle -> AI ตัดสิน:
                        approve -> capture + payout ให้ worker, สถานะ COMPLETED
                        reject  -> void คืน hold, สถานะ REFUNDED
  expire_task      -> หมดเวลา -> void, สถานะ EXPIRED
"""

from datetime import datetime, timedelta, timezone

from .store import TaskStore, utcnow
from .paypal_client import PayPalClient, PayPalError
from .judge import AIJudge


class EscrowError(Exception):
    pass


class EscrowService:
    def __init__(self, paypal=None, store=None, judge=None):
        self.paypal = paypal or PayPalClient()
        self.store = store or TaskStore()
        self.judge = judge or AIJudge()

    # ---------- 1. สร้าง task ----------

    def create_task(self, description, amount, currency="USD", deadline_hours=24,
                    requester_email=""):
        if amount <= 0:
            raise EscrowError("amount must be > 0")
        order = self.paypal.create_order(
            amount=amount,
            currency=currency,
            description=f"JudgePay escrow: {description[:80]}",
        )
        deadline_at = (datetime.now(timezone.utc)
                       + timedelta(hours=deadline_hours)).isoformat()
        task = self.store.create_task(description, amount, currency, deadline_at,
                                      requester_email)
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

    # ---------- 3. worker ส่งงาน ----------

    def submit_work(self, task_id, output, worker_email=""):
        task = self._require(task_id)
        if task["status"] != "OPEN":
            raise EscrowError(f"task {task_id} is {task['status']}, cannot submit")
        if self._is_past_deadline(task):
            raise EscrowError(f"task {task_id} is past deadline; run expire instead")
        task = self.store.update_task(
            task_id, worker_output=output, worker_email=worker_email, status="SUBMITTED"
        )
        return {
            "success": True,
            "task_id": task_id,
            "output_length": len(output or ""),
            "status": "SUBMITTED",
            "next": "run: judge-settle",
        }

    # ---------- 4. AI ตัดสิน + settle ----------

    def judge_and_settle(self, task_id):
        task = self._require(task_id)
        if task["status"] != "SUBMITTED":
            raise EscrowError(f"task {task_id} is {task['status']}, nothing to judge")

        verdict = self.judge.evaluate(task["description"], task["worker_output"] or "")
        self.store.update_task(
            task_id,
            judge_score=verdict["score"],
            judge_verdict=verdict["verdict"],
            judge_reasoning=verdict["reasoning"],
        )

        if verdict["verdict"] == "approve":
            return self._settle_approve(task_id, task, verdict)
        return self._settle_reject(task_id, task, verdict)

    def _settle_approve(self, task_id, task, verdict):
        # ปล่อยเงิน: capture ก่อน แล้ว payout ให้ worker
        try:
            cap = self.paypal.capture_authorization(task["authorization_id"])
        except PayPalError as e:
            raise EscrowError(f"capture failed: {e}")
        payout = None
        if task["worker_email"]:
            try:
                payout = self.paypal.payout_to_worker(
                    task["worker_email"], task["amount"], task["currency"],
                    note=f"JudgePay task-{task_id} approved by AI judge "
                         f"(score {verdict['score']})",
                )
            except PayPalError as e:
                # capture สำเร็จแล้ว แต่ payout ล้ม — เก็บสถานะไว้ให้ตามทีหลัง
                self.store.update_task(task_id, capture_id=cap["capture_id"],
                                       status="COMPLETED",
                                       judge_reasoning=verdict["reasoning"]
                                       + f" [payout failed: {e}]")
                return {
                    "success": True, "task_id": task_id,
                    "verdict": "approve", "score": verdict["score"],
                    "reasoning": verdict["reasoning"],
                    "capture_id": cap["capture_id"],
                    "payout": {"error": str(e)},
                    "status": "COMPLETED",
                }
        fields = {"capture_id": cap["capture_id"], "status": "COMPLETED"}
        if payout:
            fields["payout_batch_id"] = payout["batch_id"]
        self.store.update_task(task_id, **fields)
        return {
            "success": True,
            "task_id": task_id,
            "verdict": "approve",
            "score": verdict["score"],
            "reasoning": verdict["reasoning"],
            "capture_id": cap["capture_id"],
            "payout": payout,
            "status": "COMPLETED",
        }

    def _settle_reject(self, task_id, task, verdict):
        # งานไม่ผ่าน: void — เงินไม่ขยับ คืน hold ให้ requester
        try:
            void = self.paypal.void_authorization(task["authorization_id"])
        except PayPalError as e:
            raise EscrowError(f"void failed: {e}")
        self.store.update_task(task_id, status="REFUNDED")
        return {
            "success": True,
            "task_id": task_id,
            "verdict": "reject",
            "score": verdict["score"],
            "reasoning": verdict["reasoning"],
            "void_status": void["status"],
            "status": "REFUNDED",
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
    def _is_past_deadline(task):
        if not task.get("deadline_at"):
            return False
        try:
            dl = datetime.fromisoformat(task["deadline_at"])
            return datetime.now(timezone.utc) > dl
        except ValueError:
            return False
