"""PayPal REST client (sandbox) with offline mock mode.

จริง: OAuth2 client-credentials -> Orders v2 (AUTHORIZE) -> Payments v2
(authorize/capture/void) -> Payouts v1 (จ่าย worker)
Mock: ถ้า PAYPAL_MOCK=true หรือไม่มี credentials จะคืน fake ID ที่สมจริง
และจำลอง state machine ให้โฟลว์ทั้งหมดรันออฟไลน์ได้
"""

import os
import time
import uuid
import random
import string

import requests

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
LIVE_BASE = "https://api-m.paypal.com"


def _fake_id(prefix_chars="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", n=17):
    # เลียนแบบรูปแบบ PayPal ID (17 ตัวอักษร)
    return "".join(random.choice(prefix_chars) for _ in range(n))


class PayPalError(Exception):
    pass


class PayPalClient:
    def __init__(self, client_id=None, client_secret=None, mode=None, mock=None):
        self.client_id = client_id or os.getenv("PAYPAL_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("PAYPAL_CLIENT_SECRET", "")
        self.mode = (mode or os.getenv("PAYPAL_MODE", "sandbox")).lower()
        env_mock = os.getenv("PAYPAL_MOCK", "").lower()
        # mock อัตโนมัติถ้าไม่มี credentials
        self.mock = (
            True
            if mock is True
            else False
            if mock is False
            else (env_mock == "true" or not self.client_id or not self.client_secret)
        )
        self.base = LIVE_BASE if self.mode == "live" else SANDBOX_BASE
        self._token = None
        self._token_exp = 0
        # สถานะจำลองสำหรับ mock mode — persist ลงไฟล์เพื่อให้รอดข้าม process
        # (เหมือน sandbox จริงที่จำสถานะไว้ฝั่ง PayPal)
        root = os.path.dirname(os.path.abspath(__file__))
        default_mock_file = os.path.join(os.path.dirname(root), "data", "paypal_mock.json")
        self._mock_file = os.getenv("PAYPAL_MOCK_FILE", default_mock_file)
        self._orders = {}
        self._auths = {}
        self._captures = {}
        self._payouts = {}
        if self.mock:
            self._load_mock()

    # ---------- mock persistence ----------

    def _load_mock(self):
        import json
        try:
            with open(self._mock_file) as f:
                data = json.load(f)
            self._orders = data.get("orders", {})
            self._auths = data.get("auths", {})
            self._captures = data.get("captures", {})
            self._payouts = data.get("payouts", {})
        except (FileNotFoundError, ValueError):
            pass

    def _save_mock(self):
        import json
        os.makedirs(os.path.dirname(self._mock_file), exist_ok=True)
        with open(self._mock_file, "w") as f:
            json.dump({
                "orders": self._orders, "auths": self._auths,
                "captures": self._captures, "payouts": self._payouts,
            }, f)

    # ---------- auth ----------

    def _get_token(self):
        """ขอ access token (cache ไว้จนใกล้หมดอายุ)."""
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = requests.post(
            f"{self.base}/v1/oauth2/token",
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if r.status_code != 200:
            raise PayPalError(f"OAuth failed: {r.status_code} {r.text[:300]}")
        body = r.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 32400))
        return self._token

    def _headers(self, extra=None):
        h = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._get_token()}",
        }
        if extra:
            h.update(extra)
        return h

    def _post(self, path, payload, extra_headers=None):
        r = requests.post(
            f"{self.base}{path}", json=payload, headers=self._headers(extra_headers), timeout=30
        )
        if r.status_code not in (200, 201):
            raise PayPalError(f"POST {path} -> {r.status_code}: {r.text[:400]}")
        return r.json()

    def _get(self, path):
        r = requests.get(f"{self.base}{path}", headers=self._headers(), timeout=30)
        if r.status_code != 200:
            raise PayPalError(f"GET {path} -> {r.status_code}: {r.text[:400]}")
        return r.json()

    # ---------- Orders v2 ----------

    def create_order(self, amount, currency="USD", description="", reference_id=""):
        """สร้าง order แบบ AUTHORIZE -> คืน order_id + approval_url ให้ buyer ไปกดอนุมัติ."""
        if self.mock:
            order_id = _fake_id()
            ref = reference_id or f"judgepay-{uuid.uuid4().hex[:8]}"
            self._orders[order_id] = {
                "status": "CREATED",
                "amount": f"{amount:.2f}",
                "currency": currency,
                "reference_id": ref,
            }
            self._save_mock()
            return {
                "order_id": order_id,
                "approval_url": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}",
                "status": "CREATED",
            }
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [
                {
                    "reference_id": reference_id or f"judgepay-{uuid.uuid4().hex[:8]}",
                    "custom_id": f"judgepay-task-{int(time.time())}",
                    "description": (description or "JudgePay escrow")[:120],
                    "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
                }
            ],
        }
        res = self._post("/v2/checkout/orders", body)
        approval_url = next(
            (l["href"] for l in res.get("links", []) if l.get("rel") in ("approve", "payer-action")),
            "",
        )
        return {"order_id": res["id"], "approval_url": approval_url, "status": res.get("status")}

    def authorize_order(self, order_id):
        """หลัง buyer อนุมัติแล้ว -> authorize เพื่อล็อกเงินจริง (เก็บ authorization_id)."""
        if self.mock:
            order = self._orders.get(order_id)
            if not order:
                raise PayPalError(f"mock: unknown order {order_id}")
            order["status"] = "APPROVED"  # จำลองว่า buyer กดอนุมัติแล้ว
            auth_id = _fake_id()
            self._auths[auth_id] = {
                "status": "AUTHORIZED",
                "amount": order["amount"],
                "currency": order["currency"],
                "order_id": order_id,
            }
            order["status"] = "COMPLETED"
            self._save_mock()
            return {"authorization_id": auth_id, "status": "AUTHORIZED"}
        res = self._post(f"/v2/checkout/orders/{order_id}/authorize", {})
        auths = (
            res.get("purchase_units", [{}])[0].get("payments", {}).get("authorizations", [])
        )
        if not auths:
            raise PayPalError(f"authorize returned no authorization: {str(res)[:300]}")
        a = auths[0]
        return {"authorization_id": a["id"], "status": a.get("status")}

    # ---------- Payments v2 ----------

    def get_authorization(self, auth_id):
        """poll สถานะ authorization."""
        if self.mock:
            a = self._auths.get(auth_id)
            if not a:
                raise PayPalError(f"mock: unknown authorization {auth_id}")
            return {"authorization_id": auth_id, "status": a["status"]}
        res = self._get(f"/v2/payments/authorizations/{auth_id}")
        return {"authorization_id": res["id"], "status": res.get("status")}

    def capture_authorization(self, auth_id, amount=None, currency="USD"):
        """ปล่อยเงิน (approve) — final_capture=true, ใส่ PayPal-Request-Id กันยิงซ้ำ."""
        if self.mock:
            a = self._auths.get(auth_id)
            if not a:
                raise PayPalError(f"mock: unknown authorization {auth_id}")
            if a["status"] != "AUTHORIZED":
                raise PayPalError(f"mock: cannot capture authorization in status {a['status']}")
            a["status"] = "CAPTURED"
            cap_id = _fake_id()
            self._captures[cap_id] = {
                "status": "COMPLETED",
                "amount": a["amount"],
                "currency": a["currency"],
            }
            self._save_mock()
            return {"capture_id": cap_id, "status": "COMPLETED"}
        payload = {"final_capture": True}
        if amount is not None:
            payload["amount"] = {"currency_code": currency, "value": f"{amount:.2f}"}
        res = self._post(
            f"/v2/payments/authorizations/{auth_id}/capture",
            payload,
            extra_headers={"PayPal-Request-Id": str(uuid.uuid4())},
        )
        return {"capture_id": res["id"], "status": res.get("status")}

    def void_authorization(self, auth_id):
        """ยกเลิก hold — เงินไม่ขยับ (reject / expire path)."""
        if self.mock:
            a = self._auths.get(auth_id)
            if not a:
                raise PayPalError(f"mock: unknown authorization {auth_id}")
            if a["status"] not in ("AUTHORIZED",):
                raise PayPalError(f"mock: cannot void authorization in status {a['status']}")
            a["status"] = "VOIDED"
            self._save_mock()
            return {"authorization_id": auth_id, "status": "VOIDED"}
        res = self._post(f"/v2/payments/authorizations/{auth_id}/void", {})
        return {"authorization_id": auth_id, "status": res.get("status", "VOIDED")}

    # ---------- Payouts v1 ----------

    def payout_to_worker(self, email, amount, currency="USD", note=""):
        """จ่ายเงินให้ worker — sync_mode=true ตอบทันทีไม่ต้อง poll."""
        if self.mock:
            batch_id = _fake_id()
            self._payouts[batch_id] = {"status": "SUCCESS", "receiver": email, "amount": f"{amount:.2f}"}
            self._save_mock()
            return {"batch_id": batch_id, "status": "SUCCESS"}
        batch_id = f"judgepay-batch-{uuid.uuid4().hex}"
        payload = {
            "sender_batch_header": {
                "sender_batch_id": batch_id,
                "email_subject": "You got paid via JudgePay!",
                "email_message": "AI judge approved your work. Payment sent.",
            },
            "items": [
                {
                    "recipient_type": "EMAIL",
                    "amount": {"value": f"{amount:.2f}", "currency": currency},
                    "receiver": email,
                    "note": (note or "JudgePay approved payout")[:4000],
                    "sender_item_id": f"task-{uuid.uuid4().hex[:8]}",
                }
            ],
        }
        res = self._post("/v1/payments/payouts?sync_mode=true", payload)
        header = res.get("batch_header", {})
        return {"batch_id": header.get("payout_batch_id", ""), "status": header.get("batch_status", "")}
