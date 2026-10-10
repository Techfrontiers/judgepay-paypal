# PayPal REST API (Sandbox) — Technical Reference สำหรับ JudgePay PayPal Edition

> ที่มา: เอกสารทางการ developer.paypal.com (ตรวจสอบ ต.ค. 2026)
> ใช้สำหรับ PayPal AI Hackathon 2026 — ระบบ escrow ที่ AI เป็นกรรมการตัดสินก่อนปล่อยเงิน

---

## 0. พื้นฐาน: Base URL + ขอ Access Token

| Environment | Base URL |
|---|---|
| Sandbox | `https://api-m.sandbox.paypal.com` |
| Live | `https://api-m.paypal.com` |

**ขอ token** — `POST /v1/oauth2/token`

```bash
curl -X POST https://api-m.sandbox.paypal.com/v1/oauth2/token \
  -u "<CLIENT_ID>:<CLIENT_SECRET>" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials"
```

- ได้ `access_token` กลับมา (อายุ ~8–9 ชม.) เอาไปใส่ header `Authorization: Bearer <token>` ทุกครั้ง
- **ห้าม**เอา client secret ขึ้น frontend — ขอ token และเรียก API ทุกตัวจาก server เท่านั้น

---

## 1. Orders API v2 — สร้าง Order แบบ AUTHORIZE (ล็อกเงิน)

### 1.1 สร้าง order

`POST /v2/checkout/orders`

```json
{
  "intent": "AUTHORIZE",
  "purchase_units": [
    {
      "reference_id": "task-123",
      "custom_id": "judgepay-task-123",
      "description": "Freelance task escrow: <ชื่องาน>",
      "amount": {
        "currency_code": "USD",
        "value": "100.00"
      }
    }
  ]
}
```

- Response `201`: ได้ `id` (order ID), `status: "CREATED"`, และ `links[]` — เอา link `rel: "approve"` (หรือ `payer-action`) ให้ buyer (นายจ้าง/requester) ไปกดอนุมัติ
- ฟิลด์ที่มีประโยชน์: `reference_id` / `custom_id` เอาไว้ผูกกับ task ID ของ JudgePay เอง

### 1.2 Buyer อนุมัติ

- เปิด approval URL → login ด้วย **sandbox buyer account** (ดูวิธีสร้างข้อ 6) → กดอนุมัติ
- Order จะเปลี่ยนสถานะเป็น `APPROVED`
- มี webhook `CHECKOUT.ORDER.APPROVED` ยิงมาด้วย (ถ้า subscribe ไว้)

### 1.3 Authorize — ล็อกเงินจริง (createTask ของ JudgePay)

`POST /v2/checkout/orders/{order_id}/authorize`

```bash
curl -X POST https://api-m.sandbox.paypal.com/v2/checkout/orders/<ORDER_ID>/authorize \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <ACCESS_TOKEN>" \
  -d '{}'
```

- Response: authorization object มี `id` = **authorization ID** (เก็บตัวนี้ไว้ — เป็นกุญแจของ escrow ทั้งก้อน)
- สถานะ order จะกลายเป็น `COMPLETED`
- **เงินยังไม่ขยับ** — แค่ hold วงเงิน buyer ไว้

---

## 2. State Machine ของ Authorization (สำคัญที่สุด)

```
CREATED ──(authorize)──▶ AUTHORIZED (hold เงิน, เริ่มนับ 29 วัน)
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
          CAPTURED        VOIDED          EXPIRED
        (ปล่อยเงิน)    (ยกเลิก hold)    (ครบ 29 วัน, หลุดเอง)
              │
     ┌────────┴────────┐
     ▼                 ▼
  REFUNDED      PARTIALLY_CAPTURED (capture บางส่วนได้)
```

**กติกาอายุ** (จาก docs ทางการ):
- Authorization มีอายุ **29 วัน** — ครบแล้วหลุดเอง (implicit void)
- **Honor period 3 วันแรก**: PayPal แนะนำให้ capture ภายใน 3 วัน — หลังนั้น capture อาจเฟลถ้าเงินต้นทางไม่พอ/มีความเสี่ยง
- **Reauthorize** ได้ระหว่างวันที่ 4–29 (หลัง honor period หมด): `POST /v2/payments/authorizations/{id}/reauthorize` — จะได้ **authorization ID ใหม่** และเริ่ม honor period 3 วันใหม่ ทำซ้ำได้หลายรอบภายใน 29 วัน วงเงินได้สูงสุด 115% ของเดิม (ไม่เกิน +$75)
- วิธีเช็คสถานะ: `GET /v2/payments/authorizations/{authorization_id}`

---

## 3. Capture — ปล่อยเงิน (approve ของ JudgePay)

`POST /v2/payments/authorizations/{authorization_id}/capture`

```bash
curl -X POST https://api-m.sandbox.paypal.com/v2/payments/authorizations/<AUTH_ID>/capture \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <ACCESS_TOKEN>" \
  -H "PayPal-Request-Id: <uuid-กันยิงซ้ำ>" \
  -d '{
    "amount": { "currency_code": "USD", "value": "100.00" },
    "final_capture": true,
    "invoice_id": "judgepay-task-123"
  }'
```

- Body ไม่บังคับ — ถ้าไม่ส่ง amount จะ capture เต็มจำนวนที่ authorize ไว้
- `final_capture: true` = บอกว่าจะไม่ capture เพิ่มอีกแล้ว (กัน capture ซ้ำ)
- Response `201`: ได้ capture object มี `id` = **capture ID**, `status: "COMPLETED"` (หรือ `PENDING`)
- **เงินขยับตอนนี้**: ตัดจาก buyer → เข้าบัญชี merchant (หักค่าธรรมเนียม)
- ใส่ `PayPal-Request-Id` ทุกครั้ง — กันยิง capture ซ้ำตอน network timeout (idempotency)
- เช็คสถานะ: `GET /v2/payments/captures/{capture_id}`

---

## 4. Void — ยกเลิก hold (reject / timeout ของ JudgePay)

`POST /v2/payments/authorizations/{authorization_id}/void`

```bash
curl -X POST https://api-m.sandbox.paypal.com/v2/payments/authorizations/<AUTH_ID>/void \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <ACCESS_TOKEN>" \
  -d '{}'
```

- **เงินไม่ขยับเลย** — แค่ปล่อย hold คืน buyer
- Authorization → `VOIDED`
- นี่คือ path ของ "AI ตัดสินว่างานไม่ผ่าน" และ "หมดเวลาไม่มีคนส่งงาน" (cron มา void ทิ้ง)

### ถ้า capture ไปแล้วแต่อยากคืนเงิน

`POST /v2/payments/captures/{capture_id}/refund`

```json
{
  "amount": { "currency_code": "USD", "value": "100.00" },
  "note_to_payer": "งานไม่ผ่านเกณฑ์ JudgePay"
}
```

- คืนเต็มหรือคืนบางส่วนก็ได้ — ถ้าไม่ส่ง amount = คืนเต็ม

---

## 5. Payouts API — จ่ายเงินให้ worker

> Payouts เป็น **v1**: `POST /v1/payments/payouts`

```bash
curl -X POST https://api-m.sandbox.paypal.com/v1/payments/payouts \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <ACCESS_TOKEN>" \
  -d '{
    "sender_batch_header": {
      "sender_batch_id": "judgepay-batch-<uuid>",
      "email_subject": "You got paid via JudgePay!",
      "email_message": "AI ตรวจงานของคุณผ่านแล้ว เงินเข้าบัญชีเรียบร้อย"
    },
    "items": [
      {
        "recipient_type": "EMAIL",
        "amount": { "value": "95.00", "currency": "USD" },
        "receiver": "worker@example.com",
        "note": "JudgePay task-123 approved by AI judge",
        "sender_item_id": "task-123"
      }
    ]
  }'
```

- `sender_batch_id` ต้อง unique ทุกครั้ง (กันยิงซ้ำ)
- `recipient_type`: `EMAIL` | `PHONE` | `PAYPAL_ID`
- **โหมด sync** (1 รายการ ตอบทันที ไม่ต้อง poll): `POST /v1/payments/payouts?sync_mode=true` — เหมาะกับเดโม่
- เช็คสถานะ batch: `GET /v1/payments/payouts/{payout_batch_id}`
- เช็ครายการ: `GET /v1/payments/payouts-item/{payout_item_id}`
- ยกเลิกรายการที่ยังไม่มีคนรับ: `POST /v1/payments/payouts-item/{payout_item_id}/cancel`
- เงินตัดจาก **balance** ของบัญชี business (funding_source: BALANCE)

---

## 6. Webhooks — event ที่ต้อง subscribe

ตั้งค่า: Developer Dashboard → Apps & Credentials → เลือก app → Add Webhook → จำ **webhook ID** ไว้ (ต้องใช้ตอน verify)

| Event | เกิดเมื่อ |
|---|---|
| `CHECKOUT.ORDER.APPROVED` | buyer กดอนุมัติ order |
| `PAYMENT.AUTHORIZATION.CREATED` | authorize สำเร็จ (ล็อกเงินแล้ว) |
| `PAYMENT.AUTHORIZATION.VOIDED` | void / หมดอายุ |
| `PAYMENT.CAPTURE.COMPLETED` | capture สำเร็จ (เงินเข้าแล้ว) |
| `PAYMENT.CAPTURE.DENIED` | capture โดนปฏิเสธ |
| `PAYMENT.CAPTURE.PENDING` | capture รอดำเนินการ |
| `PAYMENT.CAPTURE.REFUNDED` | มีการ refund |
| `PAYMENT.PAYOUTSBATCH.SUCCESS` | batch payout เสร็จ |
| `PAYMENT.PAYOUTSBATCH.DENIED` | batch โดนปฏิเสธ |
| `PAYMENT.PAYOUTS-ITEM.SUCCEEDED` | จ่าย worker รายคนสำเร็จ |
| `PAYMENT.PAYOUTS-ITEM.FAILED` | จ่ายไม่สำเร็จ |
| `PAYMENT.PAYOUTS-ITEM.UNCLAIMED` | ส่งไปแล้วแต่ปลายทางยังไม่รับ |

### Verify signature (บังคับ — กันคนปลอม webhook)

`POST /v1/notifications/verify-webhook-signature`

```json
{
  "auth_algo": "<ค่าจาก header paypal-auth-algo>",
  "cert_url": "<ค่าจาก header paypal-cert-url>",
  "transmission_id": "<ค่าจาก header paypal-transmission-id>",
  "transmission_sig": "<ค่าจาก header paypal-transmission-sig>",
  "transmission_time": "<ค่าจาก header paypal-transmission-time>",
  "webhook_id": "<WEBHOOK_ID จาก dashboard>",
  "webhook_event": { ...body ที่ได้รับทั้งก้อน... }
}
```

- Response `verification_status: "SUCCESS"` = ของจริง
- กฎเหล็ก: **dedupe ด้วย `event_id`** — webhook มาซ้ำ/มั่วลำดับได้
- มี webhook simulator ใน dashboard (`POST /v1/notifications/simulate-event`) ไว้เทส

---

## 7. Sandbox — วิธีตั้งค่า + ข้อควรรู้

1. **สร้าง app**: developer.paypal.com → Dashboard → Apps & Credentials → แท็บ **Sandbox** → Create App → ได้ **Client ID + Secret**
2. **เปิด feature**: ใน Sandbox App Settings เปิด **Payouts** (ไม่งั้นเรียก Payouts API ไม่ได้)
3. **บัญชีทดสอบ**: Testing Tools → Sandbox Accounts — มี personal (buyer) + business (seller) สร้างให้อัตโนมัติ สร้างเพิ่มได้, กด View/Edit เพื่อดู email + password (เอาไว้ login ตอนอนุมัติ order)
4. **เทสโฟลว์จริง**: สร้าง order → เปิด approve link → login ด้วย sandbox buyer → อนุมัติ → กลับมา authorize/capture จาก server

### อะไรใช้ไม่ได้ / ไม่เหมือนจริงใน sandbox

- **ไม่ใช่เงินจริง** — authorization ไม่ได้ hold เงินจริง แค่จำลองสถานะ
- **รอ 29 วันไม่ได้** — เทส timeout ต้อง void ด้วยมือ (หรือ mock เวลา) อย่ารอให้หมดอายุเอง
- **Payouts ต้องเปิด feature ก่อน** และถ้าส่งไป email ที่ไม่มีบัญชี sandbox ปลายทางจะค้างสถานะ `UNCLAIMED` (ต้อง cancel ทิ้งเอง)
- **Webhook อาจดีเลย์** — ใน sandbox มาช้ากว่าจริง ใช้ simulator เทส listener ก่อน
- **Honor period / reauthorize** ตรรกะเหมือนจริง แต่ไม่ต้องรอ 3 วันจริงก็ reauthorize ได้ตามเงื่อนไข API
- **ค่าธรรมเนียม** จำลองหักเหมือนจริง (ดูใน activity ของ sandbox business account)

---

## 8. Flow ที่แนะนำ — JudgePay PayPal Edition

```
Requester (นายจ้าง)
  │ 1. สร้าง task
  ▼
POST /v2/checkout/orders  (intent=AUTHORIZE)
  │ 2. buyer อนุมัติ (approval URL + sandbox login)
  ▼  [webhook: CHECKOUT.ORDER.APPROVED]
POST /v2/checkout/orders/{id}/authorize
  │ 3. ล็อกเงิน → เก็บ authorization_id  [webhook: PAYMENT.AUTHORIZATION.CREATED]
  │    สถานะ task: LOCKED
  ▼
Worker ส่งงาน → AI Judge ตรวจคุณภาพ
  │
  ├─ ผ่าน ✅ ──▶ POST /v2/payments/authorizations/{id}/capture (final_capture=true)
  │              [webhook: PAYMENT.CAPTURE.COMPLETED]
  │              เงินเข้า platform account แล้ว
  │              ──▶ POST /v1/payments/payouts → จ่าย worker (หักค่าธรรมเนียม platform)
  │              [webhook: PAYMENT.PAYOUTS-ITEM.SUCCEEDED]
  │              สถานะ task: COMPLETED
  │
  ├─ ไม่ผ่าน ❌ ─▶ POST /v2/payments/authorizations/{id}/void
  │              [webhook: PAYMENT.AUTHORIZATION.VOIDED]
  │              เงินไม่ขยับ, hold คืน buyer
  │              สถานะ task: REFUNDED
  │
  └─ หมดเวลา ⏰ ─▶ cron ตรวจ deadline → void เหมือนกัน
                 สถานะ task: EXPIRED
```

**ทำไมแบบนี้ดี (ตรงเกณฑ์กรรมการ):**
- ใช้ PayPal **ลึก**: Orders v2 + Payments v2 (authorize/capture/void/reauthorize) + Payouts v1 + Webhooks — ไม่ใช่แค่ปุ่มจ่ายเงิน
- AI อยู่ตรงกลางจริง: เป็นคนตัดสินปล่อย/ยึดเงิน (ไม่ใช่ของตกแต่ง)
- เงินไม่ขยับถ้างานไม่ผ่าน — นี่คือ "financial judgment" ไม่ใช่ "payment"

---

## 9. ตาราง endpoint สรุป

| ขั้นตอน | Method + Endpoint |
|---|---|
| ขอ token | `POST /v1/oauth2/token` |
| สร้าง order (AUTHORIZE) | `POST /v2/checkout/orders` |
| ดู order | `GET /v2/checkout/orders/{id}` |
| Authorize (ล็อกเงิน) | `POST /v2/checkout/orders/{id}/authorize` |
| ดู authorization | `GET /v2/payments/authorizations/{id}` |
| Capture (ปล่อยเงิน) | `POST /v2/payments/authorizations/{id}/capture` |
| Void (ยกเลิก hold) | `POST /v2/payments/authorizations/{id}/void` |
| Reauthorize (ต่ออายุ) | `POST /v2/payments/authorizations/{id}/reauthorize` |
| ดู capture | `GET /v2/payments/captures/{id}` |
| Refund (หลัง capture) | `POST /v2/payments/captures/{id}/refund` |
| สร้าง payout | `POST /v1/payments/payouts` (+ `?sync_mode=true` สำหรับรายการเดียวแบบ sync) |
| ดู payout batch | `GET /v1/payments/payouts/{payout_batch_id}` |
| ดู payout item | `GET /v1/payments/payouts-item/{payout_item_id}` |
| ยกเลิก payout ที่ไม่รับ | `POST /v1/payments/payouts-item/{payout_item_id}/cancel` |
| Verify webhook | `POST /v1/notifications/verify-webhook-signature` |
| จำลอง webhook | `POST /v1/notifications/simulate-event` |

*อ้างอิง: developer.paypal.com — Orders v2, Payments v2, Payouts v1, Webhooks docs (ตรวจสอบ ต.ค. 2026)*
