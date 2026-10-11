# Implementation Plan — JudgePay PayPal Edition
**อ้างอิง:** AUDIT_REPORT.md → CODE_REVIEW_P0P3.md → Issue #1 + GPT second-pass

## ✅ Phase A CLOSED — Sandbox Validation ผ่าน (2026-10-11)
**Commits:** `e0963823` (Phase A A1–A7) → `037adbef` (fix void HTTP 204 + 2 tests)
**Branch:** `main` (Techfrontiers/judgepay-paypal) — working tree ตรงกับ `037adbef` เป๊ะ
(ตรวจ hash ทุกไฟล์แล้ว)
**Regression:** `pytest tests/` = **35/35 ผ่าน** รันจริงบนโค้ด `037adbef` (mock mode)

### หลักฐาน Sandbox E2E (PayPal API จริง, ไม่ใช่ mock)
- order `65G04713TY848981P` ($1.00 USD, AUTHORIZE) → buyer อนุมัติ (API: APPROVED)
- authorization `642909306S196335X` → capture `4FY74880HL962064J` (API: CAPTURED)
- payout batch `NJL9RKEWPGE4A` (API: SUCCESS) → DB task = COMPLETED
- ตรวจแยก 3 ฝ่าย (PayPal auth / PayPal batch / DB) ตรงกันทั้งหมด
- judge ให้ 100/100 แล้ว **capture_id/payout_batch_id ว่าง** = พิสูจน์ว่า AI แตะเงินไม่ได้

### แยกผลทดสอบ (ห้ามอ้างข้ามกลุ่ม)
- **Unit/Mock (35 tests):** auth, HMAC, policy, atomic settle, fail-safe, 204 void — ผ่านจริง
- **Sandbox E2E:** happy path + reject/void/reconcile — ผ่านจริง (PayPal sandbox)
- **ยังไม่ได้ทดสอบจริง:** LLM judge mode, payout UNCLAIMED/FAILED ของจริง,
  webhook (ยังไม่มี), refund API (ยังไม่มี), concurrent settle บน sandbox จริง

### บั๊กที่แก้แล้ว
- `void_authorization` รับแค่ HTTP 200/201 แต่ PayPal void สำเร็จตอบ **204**
  → เจอจาก E2E จริง, แพตช์ + regression tests ใน `037adbef`

### Known Limitations → Production: **NO-GO**
- shared admin token (demo gate), rate limit in-memory, recipient ไม่ verified กับ PayPal
- ไม่มี dispute/refund flow, ไม่มี webhook, ไม่มี audit trail
- **ห้ามใช้เงินจริงจนกว่า Phase B blockers จะปิดครบ** (ดู PHASE_B_PLAN.md)

## เดิม: Implementation status (2026-10-11) — Phase A เสร็จ, รอรีวิวก่อน push
ทำตามลำดับ A1 → A3 → A2 → A4 → A5 → A6 → A7 ครบทุกข้อ (โค้ดอยู่ branch ท้องถิ่น
`~/workspace/judgepay-paypal/`, **ยังไม่ push**). Regression: **33/33 ผ่าน**
(`pytest tests/`, mock mode). E2E ผ่าน CLI จริง: create → confirm → submit →
judge (91/100, ไม่แตะเงิน) → settle (มนุษย์ยืนยัน) → COMPLETED → reconcile
ยืนยันตรงกับ PayPal mock. Live guard ตรวจแล้ว: `PAYPAL_MODE=live` ถูกบล็อก
เว้นแต่ `JUDGEPAY_ALLOW_LIVE=1`. DB เก่า migrate อัตโนมัติ (ข้อมูลเดิมไม่หาย).
ดูรายละเอียดไฟล์ที่เปลี่ยนท้ายเอกสาร.

**สถานะเดิมของแผน:** แผนรอเจ้าของอนุมัติ — **ห้ามแก้โค้ด / ห้ามใช้เงินจริงจนกว่าจะอนุมัติ**
→ อนุมัติแล้ว (GPT work order 2026-10-11 06:46) เฉพาะ Phase A / Sandbox เท่านั้น

---

## Phase A — ก่อน Sandbox Demo (เล็กสุดที่ปลอดภัยพอ)

### A1. Server-side auth + task ownership ทุก route ที่เปลี่ยนสถานะ/แตะเงิน
- **ไฟล์:** ใหม่ `judgepay_paypal/auth.py`; แก้ `app.py` (decorator ทุก POST route);
  `store.py` (+คอลัมน์ `owner_id`)
- **วิธีทำ:**
  - `require_auth` decorator: อ่าน `JUDGEPAY_AUTH_TOKEN` จาก env เทียบด้วย
    `hmac.compare_digest` รับจาก header `X-Auth-Token` หรือ form field
    (ไม่ฝังใน JS frontend, ไม่ log)
  - `owner_id` = hash ของ token ตอนสร้าง task; ทุก route เช็ค
    `task.owner_id == current_owner` (IDOR/ข้าม task กันได้)
  - โทเคนแอดมินตัวเดียว = ประตูชั่วคราวสำหรับเดโม่เท่านั้น (ตามที่ GPT อนุมัติ)
- **ทดสอบ:** POST ไม่มี token → 403; token ผิด → 403; token A แตะ task ของ B → 403
- **Acceptance:** 4 routes (`/create` POST, `/confirm`, `/submit`, `/judge`) ปฏิเสธ
  ทุก request ที่ไม่มี token ถูกต้อง; ข้าม task ไม่ได้
- **ประเมิน:** 1–2 รอบ

### A2. Human confirmation gate ก่อน capture/payout (server-enforced)
- **ไฟล์:** แก้ `escrow.py` (แยก `judge_and_settle` → `judge_only()` +
  `settle_approved()`); แก้ `app.py` (+หน้า `/task/<id>/review` และ POST `/settle`)
- **วิธีทำ:**
  - `judge_only()`: รัน AI เก็บ score/verdict/reasoning สถานะค้าง `JUDGED` (ไม่แตะเงิน)
  - หน้า review แสดง: task, ยอด, สกุลเงิน, **ผู้รับเงิน (ที่ bind แล้ว)**,
    หลักฐานงาน, verdict+score+reasoning
  - ฟอร์มยืนยันผูกด้วย HMAC (server secret) ครอบ
    `task_id+amount+currency+recipient+verdict+score` — ค่าเปลี่ยน/เก่า → reject
- **ทดสอบ:** settle ไม่มี HMAC ถูกต้อง → reject; แก้ยอดในฟอร์ม → reject;
  กดยืนยันซ้ำ → ครั้งที่สอง reject (status guard)
- **Acceptance:** ไม่มี code path ใดจาก AI verdict ไป capture ได้โดยไม่ผ่าน
  HMAC confirmation ที่ valid
- **ประเมิน:** 2 รอบ

### A3. Recipient binding + ตรวจ invariants ก่อนจ่าย
- **ไฟล์:** แก้ `store.py` (+คอลัมน์ `worker_email_locked`), `escrow.py`
  (`submit_work`, `_settle_approve`, `confirm_authorization`)
- **วิธีทำ:**
  - `worker_email` ตั้งได้ครั้งเดียว (ตอนสร้างงานโดย requester หรือ submit ครั้งแรก)
    แล้ว lock — เปลี่ยนอีกไม่ได้
  - ก่อน capture: `get_authorization()` แล้ว assert ยอด/สกุลเงินตรงกับ task;
    ก่อน payout: assert ผู้รับ == locked email
- **ทดสอบ:** submit ครั้งที่สองเปลี่ยนอีเมล → ถูกปฏิเสธ/ไม่เปลี่ยน; แก้ยอดใน DB
  → capture ถูกบล็อก
- **Acceptance:** เงิน payout ไปได้เฉพาะ locked email; ยอด/สกุลเงินตรวจจาก
  server ทุกครั้งก่อน capture
- **ประเมิน:** 1–2 รอบ

### A4. Atomic settlement + fail-safe เมื่อผลคลุมเครือ
- **ไฟล์:** แก้ `escrow.py`, `store.py` (+เมธอด `claim_for_settlement`,
  สถานะ `SETTLING`, `NEEDS_RECONCILIATION`)
- **วิธีทำ:**
  - ก่อน settle: `UPDATE tasks SET status='SETTLING'
    WHERE id=? AND status IN ('JUDGED')` — rowcount=0 แปลว่ามีอีก process ชนะไปแล้ว
  - exception/timeout หลังยิง capture/payout: ตั้ง `NEEDS_RECONCILIATION`
    **ห้าม retry อัตโนมัติด้วย idempotency key ใหม่**
  - เพิ่ม CLI `reconcile(task_id)`: poll PayPal (authorization/payout status)
    แล้วซ่อม DB ให้ตรงความจริง
- **ทดสอบ:** ยิง settle พร้อมกัน 2 process → มีแค่หนึ่งเดียวที่ดำเนินการ;
  จำลอง crash หลัง capture (mock raise) → สถานะ NEEDS_RECONCILIATION และ
  reconcile แล้วไม่เกิด capture ซ้ำ
- **Acceptance:** concurrent test ผ่าน; crash test ไม่ทำให้เกิด side-effect ซ้ำ
- **ประเมิน:** 2–3 รอบ (ยากสุดใน Phase A)

### A5. Fail-closed judge (rubric + deterministic hard-fail)
- **ไฟล์:** ใหม่ `judgepay_paypal/policy.py`; แก้ `judge.py`, `store.py`
  (+คอลัมน์ `acceptance_criteria` JSON), `app.py` (ฟอร์ม checklist ตอนสร้างงาน)
- **วิธีทำ:**
  - ตอนสร้างงาน requester ติ๊ก checklist (ข้อบังคับ/ไม่บังคับ)
  - Hard-fail ก่อนเรียก AI: ก็อป brief เกิน X% → reject; keyword repetition
    ผิดปกติ → reject; ขาดข้อบังคับ → reject; สั้นกว่าเกณฑ์ → reject
  - ผ่านด่านแรก → AI ให้คะแนน; **threshold ค่าเดียว = 70** ทุกโหมด;
    verdict ถูก derive จาก score เสมอ (ไม่เชื่อ verdict string ที่ขัดกับ score);
    คะแนน 60–79 → `NEEDS_REVIEW` (คนตัดสินใจ, ไม่ auto-settle)
- **ทดสอบ:** 3 ตัวอย่างโจมตีจาก audit (stuffing/copy/missing) → reject พร้อม
  ระบุว่าโดน check ข้อไหน; งานก้ำกึ่ง → NEEDS_REVIEW
- **Acceptance:** ตัวอย่าง adversarial ทั้งหมดถูก reject/escalate; threshold
  แหล่งเดียว
- **ประเมิน:** 2 รอบ

### A6. Regression tests
- **ไฟล์:** ใหม่ `tests/test_auth.py`, `tests/test_settlement.py`,
  `tests/test_judge_policy.py` (pytest, mock mode)
- **ครอบคลุม:** unauthenticated access, cross-task ownership, recipient tampering,
  stuffing/copy-brief/missing-criteria, inconsistent judge output, concurrent
  settle, capture-ok/DB-fail, payout pending/failed
- **Acceptance:** `pytest` เขียวทั้งหมดก่อนเดโม่
- **ประเมิน:** 1–2 รอบ

### A7. Surface errors + limits พื้นฐาน
- **ไฟล์:** แก้ `app.py` (เลิก `except EscrowError: pass` → flash message),
  `escrow.py`/`app.py` (`MAX_OUTPUT_LEN`, rate limit ง่ายๆ ต่อ token/IP)
- **Acceptance:** judge ล้ม → เห็นเหตุผล; output เกินลิมิต → ถูกปฏิเสธพร้อมข้อความ
- **ประเมิน:** 1 รอบ

**รวม Phase A ประมาณ:** 10–14 รอบเทส

---

## Phase B — ก่อนใช้เงินจริง (hard blockers, ยังไม่ทำ)
1. Idempotency key คงที่ต่อ logical operation + reconciliation เต็มรูป
2. Identity/authorization จริง (ไม่ใช่ admin token) + verify payout recipient
3. แยกสถานะ payout (PENDING/SUCCESS/UNCLAIMED/FAILED) + retry/alert
4. PayPal webhook + verify signature + periodic reconciliation
5. Dispute/appeal + refund หลัง capture
6. LLM hardening: sanitize input, schema validation, normalize score/verdict,
   วัด variance, prompt-injection tests, abstain/escalate ชัดเจน
7. Rate limit, input limits, audit log (ใครทำอะไรเมื่อไหร่), secret management,
   concurrency + failure-injection tests

---

## ลำดับที่ GPT อนุมัติ (สรุป)
A1 → A2 → A3 → A4 → A5 → A6 → A7 — ทำตามนี้ก่อนเดโม่; Phase B ทั้งหมดคือ
blocker ก่อนเงินจริง ห้ามข้าม

---

## ไฟล์ที่เปลี่ยน/สร้างใน Phase A (2026-10-11)

**สร้างใหม่:**
- `judgepay_paypal/auth.py` — `require_auth` (hmac.compare_digest, 403 JSON),
  `rate_limited` (30/min), login/logout session + CSRF
- `judgepay_paypal/policy.py` — fail-closed policy แหล่งเดียว
  (thresholds 80/60, hard-fail 4 ข้อ, normalize_verdict, band_for)
- `tests/conftest.py`, `tests/test_auth.py`, `tests/test_settlement.py`,
  `tests/test_judge_policy.py` — 33 tests

**แก้ไข:**
- `judgepay_paypal/store.py` — คอลัมน์ `owner_id`, `worker_email_locked`,
  `acceptance_criteria`, `last_paypal_op` (+ migrate DB เก่า),
  `claim_for_settlement()` (atomic UPDATE, rowcount==1),
  สถานะใหม่ JUDGED/NEEDS_REVIEW/SETTLING/NEEDS_RECONCILIATION
- `judgepay_paypal/escrow.py` — แยก `judge_only()` / `settle_approved()`,
  HMAC confirmation token (ผูก task_id|amount|currency|recipient|verdict|score
  + expiry 30 นาที), `_assert_payment_invariants()` ก่อน capture,
  recipient lock, `MAX_OUTPUT_LEN=20000`, `manual_evaluate()`, `reconcile()`,
  payout ไม่ SUCCESS → NEEDS_RECONCILIATION (ไม่ mark COMPLETED)
- `judgepay_paypal/paypal_client.py` — live guard, `get_authorization`
  คืน amount/currency, `get_payout_batch()`, `PAYPAL_MOCK_PAYOUT_STATUS` test seam
- `judgepay_paypal/judge.py` — mock verdict ใช้กฎ >=70 (ตรง LLM prompt)
- `judgepay_paypal/cli.py` — `judge` (ไม่แตะเงิน), `settle`, `reconcile`,
  `evaluate` (manual, ไม่จ่ายทันที), `--criteria`, ต้องมี JUDGEPAY_AUTH_TOKEN
- `app.py` — auth ทุก POST (create/confirm/submit/judge/settle) + ownership,
  หน้า `/task/<id>/review`, `/login`, ป้าย SANDBOX ทุกหน้า,
  เลิกกลืน EscrowError, ฟอร์ม acceptance criteria, startup live guard
- `.env.example` — `JUDGEPAY_AUTH_TOKEN`, `JUDGEPAY_HMAC_SECRET`,
  `JUDGEPAY_ALLOW_LIVE`

## Known limitations / ความเสี่ยงที่เหลือ (Phase B blockers — ยังไม่ทำ)
1. Auth เป็น shared token ชั่วคราวสำหรับเดโม่ — ไม่ใช่ identity จริง
2. LLM judge mode ยังไม่ถูกทดสอบ (มีแต่ mock)
3. ไม่มี webhook/signature verify — reconcile เป็นแบบ manual/CLI เท่านั้น
4. ไม่มี dispute/appeal/refund workflow
5. Recipient ยังไม่ verified กับ PayPal (แค่ lock ไม่ให้เปลี่ยน)
6. Rate limit เป็น in-memory (รีสตาร์ทแล้วรีเซ็ต)
7. UNCLAIMED payout ยังไม่มี flow แจ้งเตือน/ตามเงินโดยเฉพาะ
