#!/usr/bin/env python3
"""JudgePay PayPal Edition CLI.

คำสั่ง (mirror ต้นฉบับ + confirm/judge-settle สำหรับโฟลว์ PayPal):
  create       สร้าง task + PayPal order (ได้ approval_url)
  confirm      หลัง requester อนุมัติ -> authorize ล็อกเงิน
  get          ดูรายละเอียด task
  submit       worker ส่งงาน
  evaluate     ตัดสินด้วยมือ (--approve/--reject)
  judge-settle ให้ AI ตัดสิน + settle อัตโนมัติ
  expire       หมดเวลา -> void
ทุกคำสั่งพิมพ์ JSON เหมือนต้นฉบับ
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from judgepay_paypal.escrow import EscrowService, EscrowError  # noqa: E402


def _svc():
    return EscrowService()


def main():
    parser = argparse.ArgumentParser(description="JudgePay PayPal Edition CLI")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("create", help="Create a task (PayPal order)")
    p.add_argument("--description", "-d", required=True)
    p.add_argument("--amount", "-a", required=True, type=float)
    p.add_argument("--currency", default="USD")
    p.add_argument("--deadline", type=int, default=24, help="deadline in hours")
    p.add_argument("--requester-email", default="")

    p = sub.add_parser("confirm", help="Authorize after requester approval")
    p.add_argument("task_id", type=int)
    p.add_argument("--order-id", default=None)

    p = sub.add_parser("get", help="Get task details")
    p.add_argument("task_id", type=int)

    p = sub.add_parser("submit", help="Submit work")
    p.add_argument("task_id", type=int)
    p.add_argument("--output", "-o", required=True)
    p.add_argument("--worker-email", default="")

    p = sub.add_parser("evaluate", help="Manual evaluate (no AI)")
    p.add_argument("task_id", type=int)
    p.add_argument("--approve", action="store_true")
    p.add_argument("--reject", action="store_true")

    p = sub.add_parser("judge-settle", help="AI judge + settle")
    p.add_argument("task_id", type=int)

    p = sub.add_parser("expire", help="Expire a task")
    p.add_argument("task_id", type=int)

    p = sub.add_parser("list", help="List tasks")
    p.add_argument("--status", default=None)

    args = parser.parse_args()
    svc = _svc()

    try:
        if args.command == "create":
            result = svc.create_task(args.description, args.amount, args.currency,
                                     args.deadline, args.requester_email)
        elif args.command == "confirm":
            result = svc.confirm_authorization(args.task_id, args.order_id)
        elif args.command == "get":
            result = svc.get_task(args.task_id)
        elif args.command == "submit":
            result = svc.submit_work(args.task_id, args.output, args.worker_email)
        elif args.command == "evaluate":
            # manual override: ใช้ judge แบบบังคับผล
            if args.approve:
                verdict = {"score": 100, "verdict": "approve",
                           "reasoning": "manual approve via CLI"}
            elif args.reject:
                verdict = {"score": 0, "verdict": "reject",
                           "reasoning": "manual reject via CLI"}
            else:
                result = {"error": "Specify --approve or --reject"}
                print(json.dumps(result, indent=2))
                return
            task = svc.get_task(args.task_id)
            if not task.get("success") or task["status"] != "SUBMITTED":
                result = {"error": f"task {args.task_id} not in SUBMITTED state"}
            elif verdict["verdict"] == "approve":
                result = svc._settle_approve(args.task_id, svc._require(args.task_id), verdict)
            else:
                result = svc._settle_reject(args.task_id, svc._require(args.task_id), verdict)
        elif args.command == "judge-settle":
            result = svc.judge_and_settle(args.task_id)
        elif args.command == "expire":
            result = svc.expire_task(args.task_id)
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
