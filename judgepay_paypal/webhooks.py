"""Webhook utilities — pure functions (no Flask wiring).

สถานะ (B5 scope ที่ปรับลด): receiver ยังไม่ถูก wire เข้า demo flow —
Future Work. demo path ของ hackathon นี้เป็น synchronous ทั้งหมด
(authorize -> judge -> capture+payout/void) + reconcile polling.
โมดูลนี้แยก verify -> dedup -> route -> record เป็น pure function
ที่ unit-test ได้ — แสดงความเข้าใจ webhook security ต่อกรรมการ;
ไม่ได้ทำให้ระบบปลอดภัยพอสำหรับเงินจริง.

ลำดับ pipeline (process_webhook):
  1. parse body (ต้องเป็น JSON)            -> พัง: 400 WEBHOOK_MALFORMED
  2. สกัด transmission headers            -> ขาด: 400 WEBHOOK_HEADERS_MISSING
  3. event_id ขาด                          -> 400 (dedup ไม่ได้ = ไม่ประมวล)
  4. verify_webhook_signature:
       True      -> ต่อ
       False     -> 400 WEBHOOK_SIG_FAILED, ไม่แตะ state ใดๆ
       exception -> 500 WEBHOOK_VERIFY_ERROR (transient — ให้ PayPal retry)
  5. dedup: INSERT OR IGNORE *หลัง verify เท่านั้น*
     (กัน attacker squat event_id ดัก event จริง)
     ซ้ำ & PROCESSED            -> 200 duplicate-ignored
     ซ้ำ & ยังไม่เสร็จ           -> claim สิทธิ์แบบ atomic (กัน concurrent)
  6. route event -> เฉพาะ transition ที่อนุญาต; resource ผูก task ไม่ได้
     -> ไม่เปลี่ยน state ใดๆ (audit ไว้ + reconcile ทีหลัง)
  7. mark PROCESSED/FAILED พร้อมสรุปผล

กฎเหล็ก: webhook ไม่สั่ง money movement ใหม่ (ไม่ capture/payout/refund/void)
— ทำได้แค่ confirm/record เท่านั้น.
"""

import hashlib
import json
import os
import re

from judgepay_paypal.audit import audit, AuditError
from judgepay_paypal.logutil import get_logger, log_event
from judgepay_paypal import payout_status as payout_status_mod

_log = get_logger("judgepay.webhook")

TRANSMISSION_HEADERS = (
    "PAYPAL-TRANSMISSION-ID",
    "PAYPAL-TRANSMISSION-TIME",
    "PAYPAL-TRANSMISSION-SIG",
    "PAYPAL-CERT-URL",
    "PAYPAL-AUTH-ALGO",
)

# task status ที่ถือว่า "จบแล้ว" — webhook REVERSED/VOIDED จะไม่เขียนทับ
# (แค่บันทึก note; เงินถูก account ไว้แล้วในสถานะนั้น)
_TERMINAL_STATUSES = ("REFUNDED", "COMPLETED", "EXPIRED")


class WebhookError(Exception):
    """pipeline ตัดสินใจปฏิเสธ event — มี http_status ติดมาด้วย."""

    def __init__(self, http_status, audit_action, result, msg=""):
        super().__init__(msg or result)
        self.http_status = http_status
        self.audit_action = audit_action
        self.result = result


def _audit_quiet(store, action, event_id="", event_type="",
                 outcome="ok", details=None):
    """audit แบบไม่พัง pipeline — เขียนไม่ได้ -> ลง stderr แล้วไปต่อ."""
    try:
        audit(store, actor="paypal-webhook", action=action,
              target_type="webhook_event", target_id=event_id,
              outcome=outcome, correlation_id=event_id,
              details={"event_type": event_type, **(details or {})})
    except AuditError as e:
        log_event(_log, 40, "webhook_audit_failed", str(e),
                  correlation_id=event_id)


def extract_transmission_headers(headers):
    """ดึง 5 transmission headers แบบ case-insensitive — ขาด -> WebhookError."""
    lower = {str(k).lower(): v for k, v in dict(headers or {}).items()}
    out = {}
    missing = []
    for name in TRANSMISSION_HEADERS:
        v = lower.get(name.lower(), "")
        if not v:
            missing.append(name)
        out[name] = v
    if missing:
        raise WebhookError(400, "WEBHOOK_HEADERS_MISSING", "headers-missing",
                           f"missing: {', '.join(missing)}")
    return out


def _task_id_from_resource(resource):
    """แกะ task id จาก resource (invoice_id/custom_id มี 'judgepay-N')."""
    for key in ("invoice_id", "custom_id", "invoice_number"):
        v = str((resource or {}).get(key) or "")
        m = re.search(r"judgepay-(\d+)", v)
        if m:
            return int(m.group(1))
    return None


def _route(event_type, body, store, escrow):
    """route event -> (result, task_id|None). ไม่สั่ง money movement ใหม่."""
    resource = body.get("resource") or {}
    resource_id = str(resource.get("id") or "")

    if event_type == "PAYMENT.CAPTURE.REFUNDED":
        task = store.find_task_by_capture_id(resource_id) if resource_id else None
        if not task:
            return "unmapped-resource", None
        if escrow is not None:
            synced = escrow.apply_webhook_refund(task["id"], resource)
            return (f"refund-synced:{len(synced)}" if synced
                    else "refund-already-synced"), task["id"]
        return "recorded-no-escrow", task["id"]

    if event_type in ("PAYMENT.CAPTURE.REVERSED", "PAYMENT.AUTHORIZATION.VOIDED"):
        finder = (store.find_task_by_capture_id
                  if event_type == "PAYMENT.CAPTURE.REVERSED"
                  else store.find_task_by_authorization_id)
        task = finder(resource_id) if resource_id else None
        if not task:
            return "unmapped-resource", None
        note = f"{event_type} reported by PayPal (not initiated by us)"
        store.ledger_append(task["id"], "WEBHOOK_NOTE", ref=resource_id,
                            note=note[:500])
        if task["status"] not in _TERMINAL_STATUSES:
            # PayPal ขยับเงินนอกโฟลว์เรา -> ให้คน reconcile (ไม่ force transition)
            store.update_task(task["id"], status="NEEDS_RECONCILIATION",
                              last_paypal_op=f"webhook:{event_type}")
            return "flagged-needs-reconciliation", task["id"]
        return "noted-terminal-untouched", task["id"]

    if event_type == "PAYMENT.REFUND.FAILED":
        op = (store.find_operation_by_paypal_ref(resource_id)
              if resource_id else None)
        if not op:
            return "unmapped-resource", None
        tid = op["task_id"]
        if op["status"] == "FAILED":
            return "already-failed", tid
        if op["status"] == "SUCCEEDED":
            # failure มาหลัง success (out-of-order) -> ห้ามเขียนทับ success
            return "already-succeeded-noop", tid
        store.transition_operation(op["idempotency_key"], "FAILED")
        if not store.ledger_has(tid, "REFUND_FAILED", resource_id):
            store.ledger_append(tid, "REFUND_FAILED", ref=resource_id,
                                note=f"PayPal reported refund failed "
                                     f"({event_type})")
        return "refund-marked-failed", tid

    if event_type in ("PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
                      "PAYMENT.PAYOUTS-ITEM.DENIED"):
        item_id = (str(resource.get("payout_item_id") or "")
                   or resource_id)
        task = (store.find_task_by_payout_item_id(item_id)
                if item_id else None)
        if not task:
            return "unmapped-resource", None
        new_status = payout_status_mod.normalize_item_status(
            resource.get("transaction_status"))
        store.update_payout_item_status(
            item_id, new_status,
            (resource.get("transaction_status") or "").upper() or "UNKNOWN")
        return f"item-status-synced:{new_status}", task["id"]

    if event_type == "CUSTOMER.DISPUTE.CREATED":
        dispute_id = str(body.get("id") or resource_id)
        tid = _task_id_from_resource(resource)
        task = store.get_task(tid) if tid else None
        if not task:
            return "unmapped-resource", None
        if store.get_open_dispute(task["id"]):
            return "dispute-already-open", task["id"]
        if escrow is not None:
            escrow.open_dispute(
                task["id"], opened_by="paypal-webhook",
                reason=f"PayPal dispute {dispute_id} — auto-opened from "
                       f"webhook; human must review",
                owner_id=task.get("owner_id") or "")
            return "dispute-auto-opened", task["id"]
        return "recorded-no-escrow", task["id"]

    if event_type == "CUSTOMER.DISPUTE.RESOLVED":
        tid = _task_id_from_resource(resource)
        task = store.get_task(tid) if tid else None
        if task:
            # บันทึกเฉยๆ — ห้าม auto-resolve dispute ของเรา (คนตัดสินเท่านั้น)
            store.ledger_append(task["id"], "WEBHOOK_NOTE",
                                ref=str(body.get("id") or ""),
                                note="PayPal dispute resolved externally — "
                                     "our dispute still needs human resolve")
            return "noted-no-auto-resolve", task["id"]
        return "unmapped-resource", None

    return "unknown-event", None


def process_webhook(paypal, store, raw_body, headers, escrow=None):
    """pipeline เต็ม: verify -> dedup -> route -> record.

    คืน {"http_status": int, "result": str, "event_id": str, "event_type": str}.
    pure function (ไม่แตะ Flask) — test ได้โดยตรง.
    """
    # 1. parse
    try:
        text = (raw_body.decode("utf-8") if isinstance(raw_body, bytes)
                else str(raw_body))
        body = json.loads(text)
        if not isinstance(body, dict):
            raise ValueError("not a JSON object")
    except (ValueError, UnicodeDecodeError) as e:
        _audit_quiet(store, "WEBHOOK_MALFORMED", outcome="refused",
                      details={"error": type(e).__name__})
        return {"http_status": 400, "result": "malformed",
                "event_id": "", "event_type": ""}

    # 2. headers
    try:
        tx = extract_transmission_headers(headers)
    except WebhookError as e:
        _audit_quiet(store, e.audit_action, outcome="refused",
                      details={"result": e.result})
        return {"http_status": e.http_status, "result": e.result,
                "event_id": "", "event_type": ""}

    event_id = str(body.get("id") or "")
    event_type = str(body.get("event_type") or "")
    if not event_id:
        _audit_quiet(store, "WEBHOOK_MALFORMED", outcome="refused",
                      details={"error": "missing event id"})
        return {"http_status": 400, "result": "missing-event-id",
                "event_id": "", "event_type": event_type}

    # 3. verify ลายเซ็น — คือ auth ของ route นี้ (PayPal ไม่มี token เรา)
    webhook_id = os.getenv("PAYPAL_WEBHOOK_ID", "")
    try:
        ok = paypal.verify_webhook_signature(
            auth_algo=tx["PAYPAL-AUTH-ALGO"],
            cert_url=tx["PAYPAL-CERT-URL"],
            transmission_id=tx["PAYPAL-TRANSMISSION-ID"],
            transmission_sig=tx["PAYPAL-TRANSMISSION-SIG"],
            transmission_time=tx["PAYPAL-TRANSMISSION-TIME"],
            webhook_id=webhook_id,
            webhook_event=body)
    except Exception as e:
        # transient (network / PayPal ล่ม) -> 500 ให้ PayPal retry
        _audit_quiet(store, "WEBHOOK_VERIFY_ERROR", event_id, event_type,
                      outcome="error",
                      details={"error": type(e).__name__})
        return {"http_status": 500, "result": "verify-error",
                "event_id": event_id, "event_type": event_type}
    if not ok:
        _audit_quiet(store, "WEBHOOK_SIG_FAILED", event_id, event_type,
                      outcome="refused")
        return {"http_status": 400, "result": "sig-failed",
                "event_id": event_id, "event_type": event_type}

    # 4. dedup — INSERT หลัง verify เท่านั้น (กัน squat event_id)
    resource = body.get("resource") or {}
    resource_id = str(resource.get("id") or "")
    body_hash = hashlib.sha256(
        raw_body if isinstance(raw_body, bytes) else text.encode()
    ).hexdigest()
    is_new = store.webhook_event_insert_or_ignore(
        event_id, event_type, resource_id=resource_id, body_hash=body_hash)
    if not is_new:
        existing = store.webhook_event_get(event_id)
        if existing and existing["status"] == "PROCESSED":
            _audit_quiet(store, "WEBHOOK_DUPLICATE", event_id, event_type,
                          outcome="duplicate-ignored")
            return {"http_status": 200, "result": "duplicate-ignored",
                    "event_id": event_id, "event_type": event_type}
        # RECEIVED/FAILED/PROCESSING ค้าง (crash กลางทาง) -> ลอง claim ใหม่
    if not store.webhook_event_claim(event_id):
        _audit_quiet(store, "WEBHOOK_DUPLICATE", event_id, event_type,
                      outcome="inflight-duplicate")
        return {"http_status": 200, "result": "inflight-duplicate",
                "event_id": event_id, "event_type": event_type}

    # 5. route + record
    try:
        result, task_id = _route(event_type, body, store, escrow)
        if task_id:
            store.webhook_event_set_task(event_id, task_id)
        store.webhook_event_mark(event_id, "PROCESSED", result)
        outcome = "ok" if result != "unknown-event" else "unknown-event"
        _audit_quiet(store, "WEBHOOK_PROCESSED", event_id, event_type,
                      outcome=outcome,
                      details={"result": result, "task_id": task_id})
        return {"http_status": 200, "result": result,
                "event_id": event_id, "event_type": event_type}
    except Exception as e:
        store.webhook_event_mark(event_id, "FAILED",
                                 f"{type(e).__name__}: {str(e)[:200]}")
        _audit_quiet(store, "WEBHOOK_PROCESS_ERROR", event_id, event_type,
                      outcome="error", details={"error": type(e).__name__})
        # 500 -> PayPal จะ retry; dedup + idempotent routing ทำให้ปลอดภัย
        return {"http_status": 500, "result": "process-error",
                "event_id": event_id, "event_type": event_type}
