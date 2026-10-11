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
        # B4: refunds — persist ด้วย (กัน crash แล้วไม่รู้ว่าคืนเงินไปหรือยัง)
        self._refunds = {}
        self._idem_refund = {}
        # lock กัน race ใน mock (จำลอง server-side idempotency ของ PayPal)
        self._mock_lock = threading.Lock()
        # ตัวนับ "ยิงจริง" (ไม่นับ replay) — ไว้ assert ในเทส
        self._mock_calls = {"capture_exec": 0, "payout_exec": 0,
                            "void_exec": 0, "refund_exec": 0}
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
            self._refunds = data.get("refunds", {})
            self._idem_refund = data.get("idem_refund", {})
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
                "refunds": self._refunds,
                "idem_refund": self._idem_refund,
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

    def _mock_build_payout_items(self, email, amount, currency, item_key,
                                   batch_status):
        """สร้าง mock payout items — จำลอง shape จริงของ PayPal.

        PAYPAL_MOCK_ITEMS_JSON: '[{"transaction_status":"UNCLAIMED",...}, ...]'
          สำหรับ multi-item / mixed-status test
        PAYPAL_MOCK_ITEM_STATUS: บังคับ item เดียว (batch status ไม่เปลี่ยน) —
          จำลองเคสจริง batch SUCCESS แต่ item UNCLAIMED
        ค่าเริ่มต้น: item เดียว สถานะตาม batch (เหมือนจริง: batch PENDING -> item PENDING)
        """
        import json as _json
        specs = None
        items_json = os.getenv("PAYPAL_MOCK_ITEMS_JSON", "")
        if items_json:
            try:
                specs = _json.loads(items_json)
            except ValueError:
                specs = None
        if specs is None:
            forced = os.getenv("PAYPAL_MOCK_ITEM_STATUS", "")
            specs = [{"transaction_status": forced or batch_status}]
        items = []
        for i, spec in enumerate(specs):
            st = str(spec.get("transaction_status", "SUCCESS")).upper()
            rec = spec.get("receiver", email)
            val = spec.get("amount", f"{amount:.2f}")
            item = {
                "payout_item_id": _fake_id(),
                "transaction_id": _fake_id(),
                "transaction_status": st,
                "payout_batch_id": "",  # เติมตอนสร้าง batch
                "payout_item": {
                    "receiver": rec,
                    "amount": {"currency": currency, "value": str(val)},
                    "sender_item_id": (spec.get("sender_item_id")
                                       or (item_key if i == 0 else f"{item_key}-{i}")),
                },
            }
            if st in ("FAILED", "DENIED", "BLOCKED"):
                item["errors"] = {
                    "name": spec.get("error_name", "RECEIVER_UNCONFIRMED"),
                    "message": spec.get("error_message",
                                        "Receiver cannot receive payment"),
                }
            items.append(item)
        return items

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
                items = self._mock_build_payout_items(email, amount, currency,
                                                      item_key, status)
                for it in items:
                    it["payout_batch_id"] = batch_id
                self._payouts[batch_id] = {"status": status, "receiver": email,
                                           "amount": f"{amount:.2f}",
                                           "items": items,
                                           "sender_batch_id": batch_key}
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
        """ดึง batch + items — B2: item-level คือตัวตัดสินว่าเงินถึงมือจริงไหม.

        คืน {"batch_id", "status" (batch_status), "items": [...]} โดย item แต่ละตัว
        เป็น shape มาตรฐาน: {payout_item_id, transaction_id, transaction_status,
        receiver, amount: {currency, value}, errors?}
        """
        if self.mock:
            p = self._payouts.get(batch_id)
            if not p:
                raise PayPalError(f"mock: unknown payout batch {batch_id}")
            return {"batch_id": batch_id, "status": p["status"],
                    "receiver": p.get("receiver"), "amount": p.get("amount"),
                    "items": [self._normalize_mock_item(it)
                              for it in p.get("items", [])]}
        res = self._get(f"/v1/payments/payouts/{batch_id}")
        header = res.get("batch_header", {}) or {}
        return {"batch_id": header.get("payout_batch_id", batch_id),
                "status": header.get("batch_status"),
                "items": [self._normalize_real_item(it)
                          for it in (res.get("items") or [])]}

    @staticmethod
    def _normalize_mock_item(it):
        pi = it.get("payout_item", {}) or {}
        amt = pi.get("amount", {}) or {}
        return {
            "payout_item_id": it.get("payout_item_id"),
            "transaction_id": it.get("transaction_id", ""),
            "transaction_status": it.get("transaction_status", ""),
            "receiver": pi.get("receiver", ""),
            "amount": {"currency": (amt.get("currency") or "").upper(),
                       "value": str(amt.get("value", "0"))},
            "sender_item_id": pi.get("sender_item_id", ""),
            **({"errors": it["errors"]} if it.get("errors") else {}),
        }

    @staticmethod
    def _normalize_real_item(it):
        # shape จริง: {payout_item_id, transaction_id, transaction_status,
        #   payout_batch_id, payout_item: {receiver, amount: {currency_code, value}},
        #   errors?: {name, message, ...}}
        pi = it.get("payout_item", {}) or {}
        amt = pi.get("amount", {}) or {}
        norm = {
            "payout_item_id": it.get("payout_item_id"),
            "transaction_id": it.get("transaction_id", ""),
            "transaction_status": it.get("transaction_status", ""),
            "receiver": pi.get("receiver", ""),
            "amount": {"currency": (amt.get("currency_code")
                                    or amt.get("currency") or "").upper(),
                       "value": str(amt.get("value", "0"))},
            "sender_item_id": pi.get("sender_item_id", ""),
        }
        if it.get("errors"):
            norm["errors"] = it["errors"]
        return norm

    def get_payout_item(self, payout_item_id):
        """ดึง payout item เดี่ยว — ไว้ polling / B5."""
        if self.mock:
            for b in self._payouts.values():
                for it in b.get("items", []):
                    if it.get("payout_item_id") == payout_item_id:
                        return self._normalize_mock_item(it)
            raise PayPalError(f"mock: unknown payout item {payout_item_id}")
        res = self._get(f"/v1/payments/payouts-item/{payout_item_id}")
        return self._normalize_real_item(res)

    # ---------- Payments v2: capture + refund (B4) ----------

    def _mock_refunded_total(self, capture_id):
        """ยอดที่คืนสำเร็จแล้วของ capture นี้ (mock)."""
        return sum(
            float(r["amount"]["value"])
            for r in self._refunds.values()
            if r.get("capture_id") == capture_id and r.get("status") == "COMPLETED"
        )

    def get_capture(self, capture_id):
        """ดึง capture — ตรวจสถานะ/ยอดก่อน refund (B4).

        คืน {"capture_id", "status", "amount": {"currency", "value"}}.
        สถานะ mock derived จาก refund ที่สำเร็จแล้ว เหมือน PayPal จริง
        (COMPLETED -> PARTIALLY_REFUNDED -> REFUNDED).
        """
        if self.mock:
            c = self._captures.get(capture_id)
            if not c:
                raise PayPalError(f"mock: unknown capture {capture_id}")
            total = float(c["amount"])
            refunded = self._mock_refunded_total(capture_id)
            if refunded + 1e-9 >= total:
                status = "REFUNDED"
            elif refunded > 0:
                status = "PARTIALLY_REFUNDED"
            else:
                status = c["status"]
            return {"capture_id": capture_id, "status": status,
                    "amount": {"currency": c["currency"], "value": c["amount"]}}
        res = self._get(f"/v2/payments/captures/{capture_id}")
        amt = res.get("amount", {}) or {}
        return {"capture_id": res["id"], "status": res.get("status"),
                "amount": {"currency": amt.get("currency_code"),
                           "value": amt.get("value")}}

    def refund_capture(self, capture_id, amount=None, currency="USD",
                       idempotency_key=None, note=""):
        """คืนเงินจาก capture — เงินกลับเข้า PayPal balance ของผู้จ่าย (B4).

        amount=None = คืนเต็มจำนวนคงเหลือ; ระบุ amount = คืนบางส่วน.
        สำคัญ: นี่คืนเงินจาก capture เท่านั้น — ดึงเงินคืนจากผู้รับ payout ไม่ได้.
        B1: ส่ง PayPal-Request-Id ที่เสถียร — retry ด้วย key เดิมไม่คืนซ้ำ.
        """
        fail_mode = os.getenv("PAYPAL_MOCK_FAIL_MODE", "")
        if self.mock:
            key = idempotency_key or f"mock-{uuid.uuid4().hex}"
            with self._mock_lock:
                # replay ด้วย key เดิม -> คืนผลเดิม ไม่คืนซ้ำ
                if key in self._idem_refund:
                    return dict(self._idem_refund[key])
                c = self._captures.get(capture_id)
                if not c:
                    raise PayPalError(f"mock: unknown capture {capture_id}")
                if fail_mode == "error_422_refund":
                    raise PayPalError(
                        "POST /v2/payments/captures/x/refund -> 422: "
                        "UNPROCESSABLE_ENTITY: refund not allowed for this capture")
                total = float(c["amount"])
                refundable = total - self._mock_refunded_total(capture_id)
                if amount is None:
                    amount = round(refundable, 2)
                amount = float(amount)
                if amount <= 0 or amount > refundable + 1e-9:
                    raise PayPalError(
                        "POST /v2/payments/captures/x/refund -> 422: "
                        f"refund amount {amount:.2f} exceeds refundable "
                        f"{refundable:.2f}")
                status = os.getenv("PAYPAL_MOCK_REFUND_STATUS", "COMPLETED").upper()
                if status not in ("COMPLETED", "PENDING", "FAILED"):
                    status = "COMPLETED"
                refund_id = _fake_id()
                self._refunds[refund_id] = {
                    "status": status,
                    "amount": {"currency": currency, "value": f"{amount:.2f}"},
                    "capture_id": capture_id,
                    "note": note or "",
                }
                result = {"refund_id": refund_id, "status": status,
                          "amount": {"currency": currency,
                                     "value": f"{amount:.2f}"}}
                self._idem_refund[key] = result
                self._save_mock()
                self._mock_calls["refund_exec"] += 1
                if fail_mode == "timeout_after_refund":
                    # ฝั่ง server สำเร็จแล้ว แต่ client เจอ timeout (จำลองของจริง)
                    raise requests.Timeout("mock: timeout after refund succeeded")
                return result
        key = idempotency_key or str(uuid.uuid4())
        body = {}
        if amount is not None:
            body["amount"] = {"currency_code": currency,
                              "value": f"{float(amount):.2f}"}
        if note:
            body["note_to_payer"] = note[:1000]
        res = self._post(f"/v2/payments/captures/{capture_id}/refund", body,
                         extra_headers={"PayPal-Request-Id": key})
        amt = res.get("amount", {}) or {}
        return {"refund_id": res["id"], "status": res.get("status"),
                "amount": {"currency": amt.get("currency"),
                           "value": amt.get("total") or amt.get("value")}}

    def get_refund(self, refund_id):
        """ดึงสถานะ refund — ไว้ reconcile / sync (B4)."""
        if self.mock:
            r = self._refunds.get(refund_id)
            if not r:
                raise PayPalError(f"mock: unknown refund {refund_id}")
            return {"refund_id": refund_id, "status": r["status"],
                    "amount": dict(r["amount"])}
        res = self._get(f"/v2/payments/refunds/{refund_id}")
        amt = res.get("amount", {}) or {}
        return {"refund_id": res["id"], "status": res.get("status"),
                "amount": {"currency": amt.get("currency"),
                           "value": amt.get("total") or amt.get("value")}}

    def cancel_payout_item(self, payout_item_id):
        """ยกเลิก payout item ที่ UNCLAIMED — เงินกลับเข้า balance ผู้ส่ง (B4).

        ทำได้เฉพาะ item ที่ผู้รับยังไม่รับเท่านั้น —
        เงินที่ถึงมือแล้ว (SUCCESS/PAID) เรียกคืนไม่ได้เด็ดขาด.
        หมายเหตุ: เงินกลับเข้า PayPal balance ของผู้ส่ง ไม่ใช่กลับเข้า capture.
        """
        if self.mock:
            with self._mock_lock:
                for b in self._payouts.values():
                    for it in b.get("items", []):
                        if it.get("payout_item_id") == payout_item_id:
                            st = (it.get("transaction_status") or "").upper()
                            if st != "UNCLAIMED":
                                raise PayPalError(
                                    f"mock: cannot cancel payout item in status "
                                    f"{st} (only UNCLAIMED can be cancelled)")
                            it["transaction_status"] = "CANCELED"
                            self._save_mock()
                            return {"payout_item_id": payout_item_id,
                                    "transaction_status": "CANCELED"}
                raise PayPalError(f"mock: unknown payout item {payout_item_id}")
        r = requests.post(
            f"{self.base}/v1/payments/payouts-item/{payout_item_id}/cancel",
            json={}, headers=self._headers(), timeout=30,
        )
        if r.status_code == 204:
            # 204 No Content = ยกเลิกสำเร็จ (เหมือน void)
            return {"payout_item_id": payout_item_id,
                    "transaction_status": "CANCELED"}
        if r.status_code not in (200, 201):
            raise PayPalError(
                f"POST /v1/payments/payouts-item/{payout_item_id}/cancel "
                f"-> {r.status_code}: {r.text[:400]}")
        body = r.json()
        return {"payout_item_id": payout_item_id,
                "transaction_status": body.get("transaction_status", "CANCELED")}

    # ---------- B5: webhook signature verification ----------

    def verify_webhook_signature(self, *, auth_algo, cert_url,
                                 transmission_id, transmission_sig,
                                 transmission_time, webhook_id,
                                 webhook_event):
        """ตรวจลายเซ็น webhook ผ่าน PayPal postback API (B5).

        POST /v1/notifications/verify-webhook-signature — ส่ง transmission
        headers + webhook_id + event body ดิบ; PayPal ตอบ verification_status.
        คืน True ก็ต่อเมื่อ verification_status == "SUCCESS" เท่านั้น.

        กฎ:
        - ห้ามเชื่อ body/headers ที่เข้ามาโดยไม่ผ่านฟังก์ชันนี้ก่อน
        - mock mode: จำลองผ่าน PAYPAL_MOCK_WEBHOOK_VERIFY=valid|invalid|missing
          (แยกชัดว่าไม่ใช่พฤติกรรม PayPal จริง)
        - real mode ไม่มี webhook_id -> PayPalError (config ผิด = fail-closed)
        - network/timeout -> PayPalError (caller ต้องถือว่า transient -> retry)
        """
        if self.mock:
            mode = os.getenv("PAYPAL_MOCK_WEBHOOK_VERIFY", "valid").lower()
            if mode == "valid":
                return True
            if mode == "invalid":
                return False
            raise PayPalError(
                "mock webhook verification unavailable "
                "(PAYPAL_MOCK_WEBHOOK_VERIFY=missing)")
        if not webhook_id:
            raise PayPalError(
                "PAYPAL_WEBHOOK_ID not configured — cannot verify webhook "
                "signature (fail-closed)")
        payload = {
            "auth_algo": auth_algo,
            "cert_url": cert_url,
            "transmission_id": transmission_id,
            "transmission_sig": transmission_sig,
            "transmission_time": transmission_time,
            "webhook_id": webhook_id,
            "webhook_event": webhook_event,
        }
        resp = self._post("/v1/notifications/verify-webhook-signature",
                          payload)
        return (resp.get("verification_status") or "").upper() == "SUCCESS"
