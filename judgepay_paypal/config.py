"""Startup configuration validation — fail-closed.

Hackathon demo/prototype (NOT production financial infrastructure).
แสดงความเข้าใจเรื่อง secret hygiene ต่อกรรมการ — ไม่ได้ทำให้ระบบ
ปลอดภัยพอสำหรับเงินจริง.

กฎ: config สำคัญขาดหรืออ่อนแอ -> ปฏิเสธการ start ทันที (fail-closed)
ห้ามใช้ค่า default ที่ไม่ปลอดภัยแล้วเงียบ.
"""

import os

MIN_SECRET_LEN = 32


def _need(name, min_len=MIN_SECRET_LEN):
    """env ต้องมีและยาวพอ — ไม่ผ่าน -> RuntimeError (fail-closed)."""
    v = os.getenv(name, "") or ""
    if len(v) < min_len:
        raise RuntimeError(
            f"{name} is missing or too short (< {min_len} chars) — "
            "refusing to start. Set a strong random value."
        )
    return v


def validate_startup_config():
    """ตรวจ config ตอน startup; ไม่ผ่าน -> raise RuntimeError.

    คืน dict สรุปแบบ redacted (ไม่มีค่า secret ใดๆ ทั้งสิ้น).
    """
    _need("JUDGEPAY_AUTH_TOKEN")   # ประตู operator (shared token — ดู auth.py)
    _need("JUDGEPAY_HMAC_SECRET")  # ผูก confirmation token ของ settlement
    # session/CSRF ต้องมี secret แข็งแรง — default "judgepay-demo-secret" โดนปฏิเสธ
    _need("FLASK_SECRET_KEY")

    paypal_mock = os.getenv("PAYPAL_MOCK", "").lower() == "true"
    if not paypal_mock:
        if not os.getenv("PAYPAL_CLIENT_ID") or not os.getenv("PAYPAL_CLIENT_SECRET"):
            raise RuntimeError(
                "PAYPAL_MOCK is not 'true' but PAYPAL_CLIENT_ID / "
                "PAYPAL_CLIENT_SECRET are missing — refusing to start."
            )

    webhooks_enabled = os.getenv("WEBHOOKS_ENABLED", "").lower() == "true"
    if webhooks_enabled and not os.getenv("PAYPAL_WEBHOOK_ID"):
        raise RuntimeError(
            "WEBHOOKS_ENABLED=true but PAYPAL_WEBHOOK_ID is missing — "
            "cannot verify webhook signatures. Refusing to start."
        )

    return {
        "auth_token": "configured",
        "hmac_secret": "configured",
        "flask_secret": "configured",
        "paypal_mode": os.getenv("PAYPAL_MODE", "sandbox"),
        "paypal_mock": paypal_mock,
        "webhooks_enabled": webhooks_enabled,
    }
