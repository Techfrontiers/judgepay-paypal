"""PayPal REST client (sandbox) with offline mock mode.

จริง: OAuth2 client-credentials -> Orders v2 (AUTHORIZE) -> Payments v2
(authorize/capture/void) -> Payouts v1 (จ่าย worker)
Mock: ถ้า PAYPAL_MOCK=true หรือไม่มี credentials จะคืน fake ID ที่สมจริง
และจำลอง state machine ให้โฟลว์ทั้งหมดรันออฟไลน์ได้

ข้อเท็จจริงจาก PayPal docs (ฝังไว้กันลืม — แยกจากข้อสมมติ):
- PayPal-Request-Id (REST POST): server จำ ID ไว้; retry ด้วย ID เดิม
  จะคืนสถานะล่าสุดของ request เดิม ไม่ทำ action ซ้ำ. ใช้ retry ตอน
  network timeout / HTTP 5xx. (Payouts doc: จำ key ไว้ 30 วัน)
- Payouts sender_batch_id: "PayPal does not process duplicate payouts.
  ถ้าส่ง sender_batch_id ที่ใช้ไปแล้วใน 30 วัน API จะ REJECT พร้อม error
  ที่บอก sender_batch_id เดิม + HATEOAS link ไป payout ต้นฉบับ."
  เจอ HTTP 5xx ให้ retry ด้วย sender_batch_id เดิมได้อย่างปลอดภัย.
- sender_item_id ซ้ำใน 30 วัน -> item นั้นไม่ถูกประมวลผล (batch DUPLICATE).
- 409 DUPLICATE_TRANSACTION = key นี้สำเร็จไปแล้ว -> ไปดึง resource เดิม
  อย่าส่งใหม่.
- ข้อสมมติ (อย่าพึ่งพา): ระยะเวลาเก็บ PayPal-Request-Id ของ Payments v2
  (authorize/capture/void) doc บอกแค่ "a period of time" — โค้ดห้ามพึ่งว่า
  จำตลอดไป; operations log ใน DB คือ durable source of truth ของเรา.
"""

import os
import re
import threading
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


def _parse_duplicate_payout_link(err_text):
    """หา HATEOAS link ของ original payout จาก error text.

    PayPal จะ reject sender_batch_id ซ้ำพร้อม link ไป batch ต้นฉบับ —
    คืน payout_batch_id หรือ None ถ้าไม่ใช่ duplicate error.

    หมายเหตุ (bug จาก sandbox E2E จริง): error จริงของ PayPal เป็น
    USER_BUSINESS_ERROR + "Batch with given sender_batch_id already exists"
    ไม่มีคำว่า DUPLICATE — ต้องจับทั้งสองรูป.
    """
    upper = err_text.upper()
    is_dup = ("DUPLICATE" in upper
              or ("SENDER_BATCH_ID" in upper and "ALREADY EXISTS" in upper))
    if not is_dup:
        return None
    m = re.search(r"/v1/payments/payouts/([A-Za-z0-9_-]+)", err_text)
    return m.group(1) if m else None


class PayPalClient:
    def __init__(self, client_id=None, client_secret=None, mode=None, mock=None):
        self.client_id = client_id or os.getenv("PAYPAL_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("PAYPAL_CLIENT_SECRET", "")
        self.mode = (mode or os.getenv("PAYPAL_MODE", "sandbox")).lower()
        # Phase A = sandbox เท่านั้น: ห้าม live เด็ดขาด เว้นแต่ปลดล็อกชัดแจ้ง
        if self.mode == "live" and os.getenv("JUDGEPAY_ALLOW_LIVE") != "1":
            raise PayPalError(
                "live mode is BLOCKED (Phase A = sandbox only). "
                "Set JUDGEPAY_ALLOW_LIVE=1 to override."
            )
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
        # B1: mock จำลอง idempotency ของ PayPal (key -> ผลลัพธ์) — persist ด้วย
        self._idem_capture = {}
        self._payout_idem = {}
        self._void_idem = {}
        # lock กัน race ใน mock (จำลอง server-side idempotency ของ PayPal)
        self._mock_lock = threading.Lock()
        # ตัวนับ "ยิงจริง" (ไม่นับ replay) — ไว้ assert ในเทส
        self._mock_calls = {"capture_exec": 0, "payout_exec": 0, "void_exec": 0}
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
            self._idem_capture = data.get("idem_capture", {})
            self._payout_idem = data.get("payout_idem", {})
            self._void_idem = data.get("void_idem", {})
        except (FileNotFoundError, ValueError):
            pass

    def _save_mock(self):
        import json
        os.makedirs(os.path.dirname(self._mock_file), exist_ok=True)
        with open(self._mock_file, "w") as f:
            json.dump({
                "orders": self._orders, "auths": self._auths,
                "captures": self._captures, "payouts": self._payouts,
                "idem_capture": self._idem_capture,
                "payout_idem": self._payout_idem,
                "void_idem": self._void_idem,
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
        """poll สถานะ authorization — คืนยอด/สกุลเงินด้วย (ไว้ตรวจ invariant)."""
        if self.mock:
            a = self._auths.get(auth_id)
            if not a:
                raise PayPalError(f"mock: unknown authorization {auth_id}")
            return {"authorization_id": auth_id, "status": a["status"],
                    "amount": a["amount"], "currency": a["currency"]}
        res = self._get(f"/v2/payments/authorizations/{auth_id}")
        amt = res.get("amount", {}) or {}
        return {"authorization_id": res["id"], "status": res.get("status"),
                "amount": amt.get("value"), "currency": amt.get("currency_code")}

    def capture_authorization(self, auth_id, amount=None, currency="USD",
                              idempotency_key=None):
        """ปล่อยเงิน (approve) — final_capture=true.

        B1: ส่ง PayPal-Request-Id ที่เสถียร (derive จาก task) — retry ด้วย key
        เดิม PayPal จะคืนผลเดิม ไม่ capture ซ้ำ. ถ้าไม่ส่ง key มา จะสุ่มให้
        (ใช้ได้เฉพาะ caller ที่ยิงครั้งเดียว ไม่ retry).
        """
        fail_mode = os.getenv("PAYPAL_MOCK_FAIL_MODE", "")
        if self.mock:
            key = idempotency_key or f"mock-{uuid.uuid4().hex}"
            with self._mock_lock:
                # replay ด้วย key เดิม -> คืนผลเดิมทันที ไม่ทำซ้ำ
                if key in self._idem_capture:
                    return dict(self._idem_capture[key])
                a = self._auths.get(auth_id)
                if not a:
                    raise PayPalError(f"mock: unknown authorization {auth_id}")
                if a["status"] != "AUTHORIZED":
                    raise PayPalError(
                        f"mock: cannot capture authorization in status {a['status']}")
                if fail_mode == "error_500_capture":
                    raise PayPalError(
                        "POST /v2/payments/authorizations/x/capture -> 500: "
                        "INTERNAL_SERVER_ERROR")
                if fail_mode == "error_422_capture":
                    raise PayPalError(
                        "POST /v2/payments/authorizations/x/capture -> 422: "
                        "UNPROCESSABLE_ENTITY: authorization already completed")
                a["status"] = "CAPTURED"
                cap_id = _fake_id()
                self._captures[cap_id] = {
                    "status": "COMPLETED",
                    "amount": a["amount"],
                    "currency": a["currency"],
                }
                result = {"capture_id": cap_id, "status": "COMPLETED"}
                self._idem_capture[key] = result
                self._save_mock()
                self._mock_calls["capture_exec"] += 1
                if fail_mode == "timeout_after_capture":
                    # ฝั่ง server สำเร็จแล้ว แต่ client เจอ timeout (จำลองของจริง)
                    raise requests.Timeout("mock: timeout after capture succeeded")
                return result
        key = idempotency_key or str(uuid.uuid4())
        if idempotency_key is None:
            # เตือน: ไม่มี key = retry ไม่ปลอดภัย (caller นี้ต้องยิงครั้งเดียว)
            import logging
            logging.getLogger(__name__).warning(
                "capture_authorization without idempotency_key — retry is unsafe")
        payload = {"final_capture": True}
        if amount is not None:
            payload["amount"] = {"currency_code": currency, "value": f"{amount:.2f}"}
        res = self._post(
            f"/v2/payments/authorizations/{auth_id}/capture",
            payload,
            extra_headers={"PayPal-Request-Id": key},
        )
        return {"capture_id": res["id"], "status": res.get("status")}

    def void_authorization(self, auth_id, idempotency_key=None):
        """ยกเลิก hold — เงินไม่ขยับ (reject / expire path).

        B1: รับ idempotency_key ไว้ (mock จำผลตาม key; ของจริง void เรียกซ้ำ
        ด้วย key เดิมได้โดยไม่ void ซ้ำ — 204 คือสำเร็จอยู่แล้วเลย idempotent
        ตามธรรมชาติ).
        """
        if self.mock:
            key = idempotency_key or f"mock-{uuid.uuid4().hex}"
            with self._mock_lock:
                if key in self._void_idem:
                    return dict(self._void_idem[key])
                a = self._auths.get(auth_id)
                if not a:
                    raise PayPalError(f"mock: unknown authorization {auth_id}")
                if a["status"] not in ("AUTHORIZED",):
                    raise PayPalError(
                        f"mock: cannot void authorization in status {a['status']}")
                a["status"] = "VOIDED"
                result = {"authorization_id": auth_id, "status": "VOIDED"}
                self._void_idem[key] = result
                self._save_mock()
                self._mock_calls["void_exec"] += 1
                return result
        # PayPal ตอบ 204 No Content = void สำเร็จ (ไม่มี body) — ห้าม treat เป็น error
        r = requests.post(
            f"{self.base}/v2/payments/authorizations/{auth_id}/void",
            json={}, headers=self._headers(), timeout=30,
        )
        if r.status_code == 204:
            return {"authorization_id": auth_id, "status": "VOIDED"}
        if r.status_code not in (200, 201):
            raise PayPalError(
                f"POST /v2/payments/authorizations/{auth_id}/void "
                f"-> {r.status_code}: {r.text[:400]}"
            )
        return {"authorization_id": auth_id, "status": r.json().get("status", "VOIDED")}

    # ---------- Payouts v1 ----------

    def payout_to_worker(self, email, amount, currency="USD", note="",
                         sender_batch_id=None, sender_item_id=None):
        """จ่ายเงินให้ worker — async mode (PayPal เลิกให้ใช้ sync_mode กับ integration ใหม่)
        ส่ง batch แล้ว poll สถานะจนกว่าจะ SUCCESS/FAILED (timeout ~60 วิ).

        B1: sender_batch_id ต้องเสถียรต่อ logical payout — PayPal ไม่ประมวลผล
        batch ซ้ำ (30 วัน) ถ้า API reject ว่า duplicate จะตาม HATEOAS link ไปเอา
        batch ต้นฉบับแทนการยิงใหม่.
        """
        key = sender_batch_id or f"judgepay-batch-{uuid.uuid4().hex}"
        item_key = sender_item_id or f"task-{uuid.uuid4().hex[:8]}"
        try:
            return self._payout_send(email, amount, currency, note, key, item_key)
        except PayPalError as e:
            orig = _parse_duplicate_payout_link(str(e))
            if orig:
                # PayPal บอกว่า sender_batch_id นี้ส่งไปแล้ว — ตาม link ไปเอา
                # สถานะของเดิม ห้ามยิง batch ใหม่
                return self.get_payout_batch(orig)
            raise

    def _payout_send(self, email, amount, currency, note, batch_key, item_key):
        fail_mode = os.getenv("PAYPAL_MOCK_FAIL_MODE", "")
        if self.mock:
            with self._mock_lock:
                # จำลอง PayPal จริง: sender_batch_id ซ้ำใน 30 วัน -> reject + link
                if fail_mode == "duplicate_batch" and batch_key in self._payout_idem:
                    orig = self._payout_idem[batch_key]["batch_id"]
                    raise PayPalError(
                        "POST /v1/payments/payouts -> 400: DUPLICATE_SENDER_BATCH_ID: "
                        f"sender_batch_id {batch_key} already used in the last 30 days; "
                        "\"links\": [{\"rel\": \"original_payout\", \"href\": "
                        f"\"https://api-m.sandbox.paypal.com/v1/payments/payouts/{orig}\"}}]"
                    )
                # replay ด้วย key เดิม -> คืนผลเดิม ไม่สร้าง batch ใหม่
                if batch_key in self._payout_idem:
                    return dict(self._payout_idem[batch_key])
                if fail_mode == "error_422_payout":
                    raise PayPalError(
                        "POST /v1/payments/payouts -> 422: UNPROCESSABLE_ENTITY: "
                        "receiver invalid")
                if fail_mode == "error_500_payout":
                    raise PayPalError(
                        "POST /v1/payments/payouts -> 500: INTERNAL_SERVER_ERROR")
                # test seam: PAYPAL_MOCK_PAYOUT_STATUS=PENDING/UNCLAIMED/DENIED ไว้เทส path ไม่สำเร็จ
                status = os.getenv("PAYPAL_MOCK_PAYOUT_STATUS", "SUCCESS").upper()
                batch_id = _fake_id()
                self._payouts[batch_id] = {"status": status, "receiver": email,
                                           "amount": f"{amount:.2f}"}
                result = {"batch_id": batch_id, "status": status}
                self._payout_idem[batch_key] = result
                self._save_mock()
                self._mock_calls["payout_exec"] += 1
                if fail_mode == "timeout_after_payout":
                    raise requests.Timeout("mock: timeout after payout batch created")
                return result
        payload = {
            "sender_batch_header": {
                "sender_batch_id": batch_key,
                "email_subject": "You got paid via JudgePay!",
                "email_message": "AI judge approved your work. Payment sent.",
            },
            "items": [
                {
                    "recipient_type": "EMAIL",
                    "amount": {"value": f"{amount:.2f}", "currency": currency},
                    "receiver": email,
                    "note": (note or "JudgePay approved payout")[:4000],
                    "sender_item_id": item_key,
                }
            ],
        }
        res = self._post("/v1/payments/payouts", payload)
        header = res.get("batch_header", {})
        payout_batch_id = header.get("payout_batch_id", "")
        # poll สถานะ batch (async) — ปกติ sandbox เสร็จในไม่กี่วินาที
        status = header.get("batch_status", "PENDING")
        for _ in range(12):
            if status in ("SUCCESS", "DENIED", "CANCELED"):
                break
            time.sleep(5)
            try:
                st = self._get(f"/v1/payments/payouts/{payout_batch_id}")
                status = st.get("batch_header", {}).get("batch_status", status)
            except PayPalError:
                break
        return {"batch_id": payout_batch_id, "status": status}

    def get_payout_batch(self, batch_id):
        """poll สถานะ payout batch (ไว้ reconcile)."""
        if self.mock:
            p = self._payouts.get(batch_id)
            if not p:
                raise PayPalError(f"mock: unknown payout batch {batch_id}")
            return {"batch_id": batch_id, "status": p["status"],
                    "receiver": p.get("receiver"), "amount": p.get("amount")}
        res = self._get(f"/v1/payments/payouts/{batch_id}")
        header = res.get("batch_header", {}) or {}
        return {"batch_id": header.get("payout_batch_id", batch_id),
                "status": header.get("batch_status")}
