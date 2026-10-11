"""Append-only audit trail — hackathon demo/prototype.

บันทึก actor/action/target/outcome/correlation ลงตาราง audit_log
แบบ append-only (ไม่มี update/delete) เพื่อให้ตรวจสอบย้อนหลังได้.

กฎเหล็ก:
- details ถูก scrub ก่อนบันทึกเสมอ: key ที่เข้าข่าย secret ถูกตัดทิ้ง,
  ค่ายาวเกิน 500 ตัวอักษรถูก truncate. ห้ามมี token/secret ใน audit log.
- audit เขียนล้มเหลว -> AuditError; caller ที่เป็น money-moving op
  ต้อง REFUSE operation (fail-closed) ห้ามเดินหน้าต่อแบบเงียบๆ.
"""

import json

from judgepay_paypal.logutil import get_logger, log_event  # noqa: E402

_log = get_logger("judgepay.audit")


class AuditError(Exception):
    """เขียน audit log ไม่สำเร็จ — caller ต้องตัดสินใจ fail-closed เอง."""


# substring ของ key ที่ถือว่าเป็นความลับ (case-insensitive)
_SCRUB_SUBSTRINGS = (
    "token", "secret", "password", "passwd", "authorization",
    "api_key", "apikey", "private_key", "card", "bank",
    "account_number", "ssn", "id_card",
)


def _is_secret_key(key):
    kl = str(key).lower()
    return any(s in kl for s in _SCRUB_SUBSTRINGS)


def scrub_value(value, _depth=0):
    """คืนค่า copy ที่ scrub แล้ว — ไม่แตะต้นฉบับ."""
    if isinstance(value, dict) and _depth < 4:
        return {
            k: ("[redacted]" if _is_secret_key(k)
                else scrub_value(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)) and _depth < 4:
        return [scrub_value(v, _depth + 1) for v in value]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "...[truncated]"
    return value


def audit(store, actor, action, target_type="", target_id="",
          outcome="ok", correlation_id="", details=None):
    """เขียน audit row หนึ่งแถว (details ถูก scrub ก่อนเสมอ).

    ล้มเหลว -> raise AuditError (ห้ามกลืน).
    """
    cleaned = scrub_value(details or {})
    try:
        payload = json.dumps(cleaned, ensure_ascii=False, default=str)
    except Exception:
        payload = "{}"
    try:
        return store.audit_append(
            actor=actor or "unknown",
            action=action,
            target_type=target_type or "",
            target_id=str(target_id or ""),
            outcome=outcome,
            correlation_id=correlation_id or "",
            details=payload,
        )
    except AuditError:
        raise
    except Exception as e:
        # ไม่รวมข้อความ error ดิบ (อาจพ่วง secret/path) — แค่ชนิด error
        log_event(_log, 40, "audit_write_failed",
                  f"audit append failed ({type(e).__name__})",
                  correlation_id=correlation_id)
        raise AuditError(f"audit write failed: {type(e).__name__}")
