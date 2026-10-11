#!/usr/bin/env python3
"""JudgePay PayPal Edition CLI (Phase A / sandbox).

คำสั่ง:
  create       สร้าง task + PayPal order (ได้ approval_url)
  confirm      หลัง requester อนุมัติ -> authorize ล็อกเงิน
  get          ดูรายละเอียด task
  submit       worker ส่งงาน
  evaluate     คนตัดสินเอง (--approve/--reject) — ไม่จ่ายเงินทันที
  judge        AI ตัดสินอย่างเดียว (ไม่แตะเงิน)
  settle       มนุษย์ยืนยัน settlement (ออก confirmation token ให้เอง)
  reconcile    เทียบ DB กับ PayPal จริงแล้วซ่อมสถานะ
  payout-status ดึง item-level payout truth จาก PayPal (B2)
  dispute-open   เปิดข้อพิพาท — ระงับ settlement (B4)
  dispute-evidence แนบหลักฐาน dispute (B4)
  dispute-resolve มนุษย์ตัดสิน dispute (B4)
  refund         คืนเงินจาก capture — ต้อง --confirm (B4)
  payout-cancel-item ยกเลิก payout item ที่ UNCLAIMED (B4)
  ledger         ดูบัญชีเหตุการณ์ของ task (B4)
  expire       หมดเวลา -> void
  list         รายการ task
ทุกคำสั่งพิมพ์ JSON เหมือนต้นฉบับ
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from judgepay_paypal.escrow import EscrowService, EscrowError  # noqa: E402
from judgepay_paypal.auth import owner_id_for  # noqa: E402


def _svc():
    return EscrowService()


def _owner_id():
    token = os.getenv("JUDGEPAY_AUTH_TOKEN", "")
    if not token:
        raise EscrowError("JUDGEPAY_AUTH_TOKEN not set — CLI requires the operator token")
    return owner_id_for(token)


def main():
    parser = argparse.ArgumentParser(description="JudgePay PayPal Edition CLI")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("create", help="Create a task (PayPal order)")
    p.add_argument("--description", "-d", required=True)
    p.add_argument("--amount", "-a", required=True, type=float)
    p.add_argument("--currency", default="USD")
    p.add_argument("--deadline", type=int, default=24, help="deadline in hours")
    p.add_argument("--requester-email", default="")
    p.add_argument("--criteria", default="",
                   help="acceptance criteria, one per line; '!' prefix = required")

    p = sub.add_parser("confirm", help="Authorize after requester approval")
    p.add_argument("task_id", type=int)
    p.add_argument("--order-id", default=None)

    p = sub.add_parser("get", help="Get task details")
    p.add_argument("task_id", type=int)

    p = sub.add_parser("submit", help="Submit work")
    p.add_argument("task_id", type=int)
    p.add_argument("--output", "-o", required=True)
    p.add_argument("--worker-email", default="")

    p = sub.add_parser("evaluate", help="Manual evaluate (no AI, no instant settle)")
    p.add_argument("task_id", type=int)
    p.add_argument("--approve", action="store_true")
    p.add_argument("--reject", action="store_true")

    p = sub.add_parser("judge", help="AI judge only (no money moves)")
    p.add_argument("task_id", type=int)

    p = sub.add_parser("settle", help="Human-confirmed settlement")
    p.add_argument("task_id", type=int)
    p.add_argument("--approve-review", action="store_true",
                   help="required for NEEDS_REVIEW tasks")

    p = sub.add_parser("reconcile", help="Reconcile task state with PayPal truth")
    p.add_argument("task_id", type=int, nargs="?",
                   help="task id (ไม่ต้องใส่ถ้าใช้ --all)")
    p.add_argument("--all", action="store_true",
                   help="reconcile ทุก operation ที่ค้างทั่ว DB (ใช้ตอน start/cron)")

    p = sub.add_parser("payout-status",
                       help="Refresh item-level payout truth from PayPal (B2)")
    p.add_argument("task_id", type=int)

    p = sub.add_parser("dispute-open", help="Open a dispute (suspends settlement)")
    p.add_argument("task_id", type=int)
    p.add_argument("--by", default="operator", help="who opens the dispute")
    p.add_argument("--reason", required=True)
    p.add_argument("--evidence", default="", help="initial evidence note")

    p = sub.add_parser("dispute-evidence", help="Attach evidence to open dispute")
    p.add_argument("task_id", type=int)
    p.add_argument("--by", default="operator")
    p.add_argument("--note", required=True)

    p = sub.add_parser("dispute-resolve",
                       help="Human resolves dispute (upheld|rejected)")
    p.add_argument("task_id", type=int)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--decision", required=True, choices=["upheld", "rejected"])
    p.add_argument("--reason", default="")

    p = sub.add_parser("refund", help="Refund from capture (requires --confirm)")
    p.add_argument("task_id", type=int)
    p.add_argument("--amount", type=float, default=None,
                   help="partial amount; omit for full remaining")
    p.add_argument("--reason", default="")
    p.add_argument("--confirm", action="store_true",
                   help="explicit human confirmation (required)")
    p.add_argument("--dispute-resolution", action="store_true",
                   help="this refund resolves an upheld dispute")

    p = sub.add_parser("payout-cancel-item",
                       help="Cancel an UNCLAIMED payout item")
    p.add_argument("task_id", type=int)
    p.add_argument("item_id")

    p = sub.add_parser("ledger", help="Show task ledger (audit trail)")
    p.add_argument("task_id", type=int)

    p = sub.add_parser("expire", help="Expire a task")
    p.add_argument("task_id", type=int)

    p = sub.add_parser("list", help="List tasks")
    p.add_argument("--status", default=None)

    args = parser.parse_args()
    svc = _svc()

    def parse_criteria(text):
        out = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            req = line.startswith("!")
            out.append({"text": line[1:].strip() if req else line, "required": req})
        return out

    try:
        if args.command == "create":
            result = svc.create_task(args.description, args.amount, args.currency,
                                     args.deadline, args.requester_email,
                                     owner_id=_owner_id(),
                                     acceptance_criteria=parse_criteria(args.criteria))
        elif args.command == "confirm":
            result = svc.confirm_authorization(args.task_id, args.order_id)
        elif args.command == "get":
            result = svc.get_task(args.task_id)
        elif args.command == "submit":
            result = svc.submit_work(args.task_id, args.output, args.worker_email)
        elif args.command == "evaluate":
            if args.approve == args.reject:
                result = {"error": "Specify exactly one of --approve / --reject"}
            else:
                result = svc.manual_evaluate(args.task_id, approve=args.approve)
        elif args.command == "judge":
            result = svc.judge_only(args.task_id)
        elif args.command == "settle":
            token = svc.make_confirmation_token(args.task_id)
            result = svc.settle_approved(args.task_id, token,
                                         human_approve=args.approve_review)
        elif args.command == "reconcile":
            if args.all:
                result = svc.reconcile_all()
            elif args.task_id is None:
                result = {"error": "reconcile ต้องระบุ task_id หรือ --all"}
            else:
                result = svc.reconcile(args.task_id)
        elif args.command == "expire":
            result = svc.expire_task(args.task_id)
        elif args.command == "payout-status":
            result = svc.refresh_payout(args.task_id)
        elif args.command == "dispute-open":
            result = svc.open_dispute(args.task_id, args.by, args.reason,
                                      evidence_note=args.evidence,
                                      owner_id=_owner_id())
        elif args.command == "dispute-evidence":
            result = svc.add_dispute_evidence(args.task_id, args.by, args.note,
                                              owner_id=_owner_id())
        elif args.command == "dispute-resolve":
            result = svc.resolve_dispute(args.task_id, args.reviewer,
                                         args.decision, reason=args.reason,
                                         owner_id=_owner_id())
        elif args.command == "refund":
            result = svc.refund_capture(args.task_id, amount=args.amount,
                                        reason=args.reason,
                                        human_confirm=args.confirm,
                                        dispute_resolution=args.dispute_resolution,
                                        owner_id=_owner_id())
        elif args.command == "payout-cancel-item":
            result = svc.cancel_payout_item(args.task_id, args.item_id,
                                            owner_id=_owner_id())
        elif args.command == "ledger":
            t = svc.get_task(args.task_id)
            result = {"success": True, "task_id": args.task_id,
                      "task_status": t["status"],
                      "disputes": svc.store.list_disputes(args.task_id),
                      "ledger": svc.store.ledger_list(args.task_id)}
        elif args.command == "list":
            result = svc.list_tasks(args.status)
        else:
            parser.print_help()
            return
    except EscrowError as e:
        result = {"error": str(e)}

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
