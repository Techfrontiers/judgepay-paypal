"""B2: payout lifecycle — item-level status mapping (single source of truth).

แยก 3 ระดับชัดเจน:
  Task State     — สถานะงานใน JudgePay (COMPLETED / PAYOUT_PENDING / ...)
  Batch State    — ภาพรวม batch จาก PayPal (informational เท่านั้น)
  Item State     — สถานะเงินถึงมือผู้รับแต่ละราย (ตัวตัดสินจริง)

กฎเหล็ก: ห้ามใช้ batch status อย่างเดียวสรุปว่าผู้รับได้เงินแล้ว —
ต้องดู transaction_status ของทุก item. สถานะที่ไม่รู้จัก -> NEEDS_RECONCILIATION
เสมอ ห้ามตีความเป็น success.
"""

# PayPal transaction_status (Payouts API) -> สถานะภายในของเรา
# อ้างอิง: PayPal Payouts API docs (batch/item status values)
ITEM_STATUS_MAP = {
    "SUCCESS": "PAID",
    "PENDING": "PENDING",
    "PROCESSING": "PENDING",
    "ONHOLD": "PENDING",
    "UNCLAIMED": "UNCLAIMED",
    "FAILED": "FAILED",
    "DENIED": "FAILED",
    "BLOCKED": "FAILED",
    "RETURNED": "RETURNED",
    "REFUNDED": "RETURNED",
    "REVERSED": "RETURNED",
    "CANCELED": "CANCELED",
}

# สถานะ item ภายในทั้งหมด (ห้ามเพิ่มโดยไม่ปรับ aggregate_task_status)
OUR_ITEM_STATUSES = ("PAID", "PENDING", "UNCLAIMED", "FAILED",
                     "RETURNED", "CANCELED", "NEEDS_RECONCILIATION")


def normalize_item_status(paypal_status):
    """แปลง transaction_status ดิบ -> สถานะภายใน.

    ไม่รู้จัก / ว่าง -> NEEDS_RECONCILIATION (fail-closed: ไม่มีทางเป็น success).
    """
    return ITEM_STATUS_MAP.get((paypal_status or "").strip().upper(),
                               "NEEDS_RECONCILIATION")


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_payout_item(raw):
    """แปลง payout item ดิบจาก PayPal (real/mock) -> dict สำหรับ payout_items.

    รองรับทั้ง shape จริง ({payout_item: {receiver, amount: {currency_code, value}}})
    และ shape ย่อ ({receiver, amount: {currency, value}}).
    """
    raw = raw or {}
    pi = raw.get("payout_item") or {}
    amt = pi.get("amount") or {}
    errors = raw.get("errors") or {}
    raw_status = raw.get("transaction_status")
    return {
        "payout_item_id": raw.get("payout_item_id"),
        "transaction_id": raw.get("transaction_id") or "",
        "sender_item_id": pi.get("sender_item_id") or "",
        "receiver": pi.get("receiver") or raw.get("receiver") or "",
        "amount": _to_float(amt.get("value")),
        "currency": (amt.get("currency_code") or amt.get("currency") or "").upper(),
        "item_status": normalize_item_status(raw_status),
        "paypal_status": (raw_status or "").strip().upper() or "UNKNOWN",
        "error_name": (errors.get("name") or "")[:120],
        "error_message": (errors.get("message") or "")[:500],
    }


def aggregate_task_status(item_statuses, has_capture):
    """สรุป task status จาก item states — กฎเดียวของทั้งระบบ.

    ลำดับความรุนแรง (เจออันไหนก่อนใช้อันนั้น):
      ว่าง/ไม่รู้จัก -> NEEDS_RECONCILIATION
      มี NEEDS_RECONCILIATION -> NEEDS_RECONCILIATION (ของไม่ชัดห้ามบัง)
      มี FAILED -> PAYOUT_FAILED
      มี CANCELED -> PAYOUT_FAILED (เงินไม่ขยับ ต้องคนตัดสินใจ)
      มี RETURNED -> PAYOUT_RETURNED
      มี PENDING/UNCLAIMED -> PAYOUT_PENDING (เงินยังไม่ถึงมือ)
      ทั้งหมด PAID (+ capture ครบ) -> COMPLETED
    """
    statuses = [s for s in (item_statuses or []) if s]
    if not statuses:
        return "NEEDS_RECONCILIATION"
    if any(s not in OUR_ITEM_STATUSES for s in statuses):
        return "NEEDS_RECONCILIATION"
    if "NEEDS_RECONCILIATION" in statuses:
        return "NEEDS_RECONCILIATION"
    if "FAILED" in statuses:
        return "PAYOUT_FAILED"
    if "CANCELED" in statuses:
        # ยกเลิกก่อนเงินออก — ไม่ใช่ success ต้องคนดู (B2: ห้ามสร้าง payout ใหม่อัตโนมัติ)
        return "PAYOUT_FAILED"
    if "RETURNED" in statuses:
        return "PAYOUT_RETURNED"
    if "PENDING" in statuses or "UNCLAIMED" in statuses:
        return "PAYOUT_PENDING"
    if all(s == "PAID" for s in statuses):
        return "COMPLETED" if has_capture else "NEEDS_RECONCILIATION"
    return "NEEDS_RECONCILIATION"  # กันไว้ — ไม่มีทางตกมาถึง success โดยบังเอิญ
