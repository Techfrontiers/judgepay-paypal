# JudgePay PayPal Edition — Runbook (B5)

> **Framing: this is a HACKATHON COMPETITION ENTRY / demo prototype —
> NOT production financial infrastructure.** The B5 security work below
> demonstrates security understanding to judges. It does **not** make the
> system safe for real money. Never deploy with real funds, never claim
> "production-ready".

## 1. Config checklist (fail-closed)

`app.py` เรียก `validate_startup_config()` ตอน start — ไม่ผ่าน = ไม่รัน:

| env | กฎ |
|---|---|
| `JUDGEPAY_AUTH_TOKEN` | ต้องมี, ยาว ≥ 32 (shared operator token — **ไม่ใช่** multi-user) |
| `JUDGEPAY_HMAC_SECRET` | ต้องมี, ยาว ≥ 32 (ผูก settlement confirmation) |
| `FLASK_SECRET_KEY` | ต้องมี, ยาว ≥ 32 (session/CSRF; ห้ามใช้ค่า default) |
| `PAYPAL_MOCK=true` | ข้าม client id/secret check; ถ้าไม่ใช่ `true` ต้องมี `PAYPAL_CLIENT_ID` + `PAYPAL_CLIENT_SECRET` |
| `WEBHOOKS_ENABLED=true` | ต้องมี `PAYPAL_WEBHOOK_ID` (ไว้ verify ลายเซ็น) |
| `PAYPAL_MODE=live` | ถูกบล็อกถ้าไม่มี `JUDGEPAY_ALLOW_LIVE=1` (demo นี้ = sandbox เท่านั้น) |

ตรวจเร็ว: `python -c "from judgepay_paypal.config import validate_startup_config; print(validate_startup_config())"`
(คืนเฉพาะ "configured" — ไม่มีค่า secret หลุด)

## 2. Health / readiness

- `GET /healthz` → `{"ok": true}` (ไม่ต้อง auth, ไม่แตะ DB)
- `GET /readyz` → `{"ready": bool, "checks": {"db": bool, "config": bool}}`
  (boolean เท่านั้น — ไม่เปิดเผย internals)

## 3. Webhooks — สถานะ: ยังไม่ wire เข้า demo (Future Work)

- Demo path เป็น synchronous ทั้งหมด (authorize → judge → capture+payout/void)
  + `reconcile` polling — ไม่มี receiver ใน `app.py` โดยตั้งใจ
- มีให้แล้ว (unit-tested, mock): `PayPalClient.verify_webhook_signature()`
  (postback `POST /v1/notifications/verify-webhook-signature`),
  ตาราง `webhook_events` (UNIQUE event_id = dedup), และ pure pipeline
  `judgepay_paypal/webhooks.process_webhook()` (verify → dedup → route → record)
- ถ้าจะต่อ receiver ภายหลัง: route ต้อง **ไม่**ใช้ `require_auth`
  (PayPal ไม่มี token เรา) — ลายเซ็นคือ auth; verify **ก่อน** insert event_id
  (กัน attacker squat event_id ดัก event จริง); กฎเหล็ก "webhook ห้ามสั่ง
  money movement ใหม่ — confirm/record เท่านั้น"

### Webhook failure modes (สำหรับอนาคต / อ้างอิงตอน demo Q&A)

| อาการ | รับมือ |
|---|---|
| ลายเซ็นไม่ผ่าน | ทิ้ง, ไม่แตะ state ใดๆ, audit `WEBHOOK_SIG_FAILED` |
| verify ล่ม (network) | ถือว่า transient → ให้ PayPal retry; ยังไม่ dedup |
| event ซ้ำ | `UNIQUE(event_id)` → `duplicate-ignored`, เอฟเฟกต์ครั้งเดียว |
| crash กลางทาง | event ค้าง `RECEIVED` → redelivery มา claim ใหม่แบบ atomic |
| event ผิดลำดับ / resource ไม่รู้จัก | ไม่เปลี่ยน state → audit + `NEEDS_RECONCILIATION` |
| event type ไม่รู้จัก | 200 + audit `WEBHOOK_UNKNOWN_EVENT`, ไม่ทำอะไร |

## 4. Reconciliation (ธุรกรรมค้าง)

```bash
python -m judgepay_paypal.cli reconcile <task_id>   # task เดียว
python -m judgepay_paypal.cli reconcile --all        # ทั้ง DB (ตอน start / หลัง restart)
python -m judgepay_paypal.cli ledger <task_id>       # ดูประวัติเงิน
```

กฎ: timeout/ผลคลุมเครือ → `NEEDS_RECONCILIATION` เสมอ (ห้ามสรุปว่าล้มเหลว);
replay ใช้ idempotency key เดิม — PayPal คืนผลเดิม ไม่ทำซ้ำ.

## 5. Audit trail

- ตาราง `audit_log` — append-only (ไม่มี update/delete API)
- ทุก row: actor, action, target, outcome, correlation_id, timestamp;
  `details` ถูก scrub (key เข้าข่าย secret → `[redacted]`, ค่ายาว → truncate)
- เหตุการณ์ที่บันทึก: `TASK_CREATED`, `AUTHORIZED`, `WORK_SUBMITTED`,
  `JUDGE_VERDICT`, `SETTLE_INTENT`/`SETTLE_RESULT`, `OP_INTENT`/`OP_RESULT`,
  `DISPUTE_OPENED`/`DISPUTE_EVIDENCE`/`DISPUTE_RESOLVED`,
  `REFUND_INTENT`/`REFUND_RESULT`, `RECONCILE`, `AUTH_FAILED`, `WEBHOOK_*`
- **audit เขียนล้มเหลว → money op ถูกปฏิเสธ (fail-closed)** ก่อน provider ถูกยิง;
  op ที่ไม่ใช่เงิน → ลง stderr แล้วไปต่อ
- ดูย้อนหลัง: `store.audit_list(target_type="task", target_id="<id>")`

## 6. Incident response (demo)

1. หยุดก่อน — อย่ายิงซ้ำด้วย key ใหม่ (เสี่ยงทำซ้ำ)
2. ดู `ledger <task_id>` + `audit_log` ว่าเงินไปถึงไหนแล้ว
3. รัน `reconcile <task_id>` — ปลอดภัย, idempotent
4. ถ้ายัง `NEEDS_RECONCILIATION` → ตรวจ PayPal dashboard (sandbox) เทียบกับ DB
5. อย่าแก้ DB ตรงๆ เพื่อ "ให้ยอดตรง" — ledger เป็น append-only

## 7. Secret rotation (manual — ไม่มี automation โดยตั้งใจ)

1. สร้างค่าใหม่ (≥ 32 chars, สุ่ม)
2. อัปเดต env (`JUDGEPAY_AUTH_TOKEN` / `JUDGEPAY_HMAC_SECRET` / `FLASK_SECRET_KEY`)
3. restart app — session เก่าตายหมด (ตั้งใจ: token เก่าใช้ต่อไม่ได้)
4. หมายเหตุ: `owner_id = sha256(token)` — เปลี่ยน token = owner_id เปลี่ยน
   (task เก่าผูก owner เดิม; ตั้งใจบันทึกไว้ตรงๆ — shared-token model
   ไม่ใช่ multi-user isolation)

## 8. ข้อห้าม (non-negotiable)

- ห้าม Live credentials / เงินจริง — `PAYPAL_MODE=live` ถูกบล็อกโดย default
- ห้ามอ้างว่า multi-user isolation — เป็น shared operator token
- ห้ามอ้างว่า "production-ready" — นี่คืองานส่งแข่ง hackathon
- ห้าม log token / Authorization header / webhook body ดิบ (เก็บแค่ hash)
- AI judge ตัดสิน dispute ไม่ได้ — คนเท่านั้น
