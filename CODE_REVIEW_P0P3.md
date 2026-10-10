# Code-Level Security Review — ตรวจ P0–P3 เทียบโค้ดจริง
**วันที่:** 2026-10-11 | **ผู้ตรวจ:** มีนา | **ตามใบสั่ง:** GitHub Issue #1 (GPT review)
**ขอบเขต:** `judgepay_paypal/` + `app.py` (PayPal edition) — ยังไม่มีการแก้โค้ด

## 1. ผลตรวจ P0–P3 เทียบโค้ดจริง (มีหลักฐานบรรทัด)

### P0-1 — ไม่มี authentication/authorization ทุก route ✅ ยืนยัน
- `app.py:85` `/create` (GET+POST), `:179` `/confirm`, `:189` `/submit`,
  `:198` `/judge`, `:112` ดู task, `:207` `/dashboard` — **ไม่มี auth check ใดๆ**
- `app.py:22` `secret_key` default = `"judgepay-demo-secret"` (hardcoded fallback)
- ไม่มี CSRF token ทุกฟอร์ม
- **Impact:** ใครเข้าถึงเว็บได้ = สร้าง task ด้วยเงิน requester, ส่งงาน, กด judge
  ให้ capture+payout ได้ทันที
- **Exploit preconditions:** เข้าถึง URL ได้ (ไม่มี login)

### P0-2 — AI verdict ตรงไปสั่งจ่ายเงิน ไม่มีมนุษย์ขวาง ✅ ยืนยัน
- `escrow.py:99-114` `judge_and_settle()`: `verdict["verdict"]=="approve"` →
  `_settle_approve()` ทันที
- `escrow.py:116-157` `_settle_approve()`: ยิง `capture_authorization` + 
  `payout_to_worker` ต่อกัน ไม่มี confirm step
- `app.py:198-203` ปุ่ม "Run AI judge" → เรียกตรง ไม่มีหน้ายืนยัน
- **Impact:** คำตัดสิน AI (ที่หลอกได้ — ดู P0-3) = คำสั่งโอนเงิน

### P0-3 — Judge วัดแค่ keyword overlap หลอกง่าย ✅ ยืนยัน (มีหลักฐานทดสอบ)
- `judge.py:42-62` `mock_evaluate()`: `score = 100×(0.7×coverage + 0.3×length/200)`,
  เกณฑ์ผ่าน `score >= 60` (บรรทัด 56)
- ทดสอบจริง: ขยะยัด keyword = 83/approve, ก็อป brief = 83/approve,
  งานดีจริง = 74/approve
- **ไม่ได้ตรวจทีละข้อ** — brief ถูกส่งเป็นข้อความดุ้นเดียว ไม่มี structured rubric
- `judge.py:87-90` `llm_evaluate()`: รับ `score`+`verdict` จาก LLM ตรงๆ —
  ถ้า LLM ตอบ score=90/verdict=reject โค้ด**เก็บตามนั้นทั้งคู่** (inconsistent ได้)

### P0-4 (GPT เพิ่ม) — ผูกตัวตน worker กับผู้รับเงิน ❌ ยืนยันว่าหลวม
- `app.py:192` `submit_work(task_id, request.form["output"],
  request.form.get("worker_email",""))` — **คนส่งงานพิมพ์อีเมลอะไรก็ได้**
- `escrow.py:125` payout ไป `task["worker_email"]` ตรงๆ ไม่มีการ verify
- **Impact:** ผู้โจมตีส่งงานขยะ + ใส่อีเมลตัวเอง → ถ้า AI ผ่าน (ซึ่งหลอกง่าย) เงินเข้าตัวเอง
- เงื่อนไข exploit: ต้องผ่าน P0-1 (ไม่มี auth) มาก่อน — สองข้อนี้ผูกกัน

### P1-high (GPT ยกระดับ) — crash consistency / idempotency ✅ ยืนยัน + แก้ความเข้าใจ
- `paypal_client.py`: `capture_authorization` สร้าง `PayPal-Request-Id: uuid4()`
  **ใหม่ทุกครั้ง** — ไม่ใช่ idempotency key ที่ stable ต่อ logical operation
- **แก้ความเข้าใจจาก AUDIT_REPORT:** ถ้า process ตายหลัง capture สำเร็จแต่ก่อน
  `store.update_task` (escrow.py:147) แล้วรันซ้ำ — จะยิง capture ซ้ำด้วย request
  id ใหม่ → PayPal **reject** (authorization ถูกปิดด้วย final_capture=true แล้ว)
  → ไม่ได้ double-charge แต่ task **ค้างที่ SUBMITTED** ทั้งที่เงินถูก capture
  ไปแล้ว = สถานะเสีย ไม่ใช่แค่ "เสี่ยงยิงซ้ำ"
- `escrow.py:99-114` check-then-act ไม่ atomic: สอง process เรียก
  `judge_and_settle` พร้อมกันผ่านเช็ค `status==SUBMITTED` ได้ทั้งคู่ (race)
- **Impact:** เงินออกจากระบบแต่ DB บอกว่ายังไม่จ่าย → ตามเงินยาก

### P1 — เกณฑ์ผ่านไม่ตรงกันระหว่างโหมด ✅ ยืนยัน
- `judge.py:56` mock: `>= 60`; `judge.py:90` LLM fallback: `>= 70`;
  prompt บอก LLM ว่า approve เมื่อ `>= 70` — สามที่ไม่ตรงกัน

### P1 — payout ทำเครื่องหมาย COMPLETED ทั้งที่เงินอาจไม่ถึงมือ ✅ ยืนยัน (GPT ชี้)
- `paypal_client.py` (poll loop): break เมื่อ batch_status ใน
  `("SUCCESS","DENIED","CANCELED")` — **ไม่เช็ค item-level status**
  (UNCLAIMED/FAILED ระดับรายการไม่ถูกตรวจ)
- `escrow.py:147` mark COMPLETED ทันทีที่มี batch_id — งาน "เสร็จ" แต่ worker
  อาจยังไม่ได้เงิน และไม่มีโค้ดตามต่อ

### P2 — ไม่มี dispute/appeal/refund ✅ ยืนยัน
- ไม่มี route/fuction สำหรับ dispute; ไม่มี `refund` หลัง capture (grep ไม่เจอ);
  `STATUSES` ไม่มี DISPUTED

### P2 — ไม่มี webhook/reconciliation ✅ ยืนยัน
- grep ทั้ง repo ไม่เจอ webhook handler; สถานะเงินรู้แค่ตอน poll ตอน action

### P2 — LLM judge ไม่เคยรัน + prompt injection ✅ ยืนยัน
- `judge.py:64-83` เอา brief+output แปะลง user message ตรงๆ ไม่ sanitize;
  ไม่มี test ใดๆ (ไม่มี API key ตอน audit)

### P3 — misc (ยืนยัน)
- `app.py:198-203` `except EscrowError: pass` — กลืน error, operator ไม่รู้ว่า
  settle ล้มเพราะอะไร
- ไม่จำกัดขนาด output (submit ยาวเท่าไหร่ก็ได้ → ส่งเข้า LLM = เสียเงิน/DoS)
- `store.update_task` สร้าง SET clause จาก kwargs — keys มาจากโค้ดภายในเท่านั้น
  (ไม่ใช่ user input ตรงๆ) → ความเสี่ยง SQL injection ต่ำ แต่เป็น code smell

## 2. สิ่งที่ AUDIT_REPORT เขียนไม่ตรง/ต้องแก้ความเข้าใจ
1. **Double-capture:** ไม่ใช่ "เสี่ยงชาร์จซ้ำ" — PayPal จะ reject capture ครั้งที่สอง
   (final_capture ปิด auth แล้ว) ปัญหาจริงคือ **task ค้าง SUBMITTED ทั้งที่เงินออก
   แล้ว** → ต้องแก้ด้วย reconciliation ไม่ใช่แค่ idempotency key
2. **UNCLAIMED:** รายงานจัด P3 — เห็นด้วยกับ GPT ว่าควรแยกสถานะ payout ออกจาก
   task status และไม่ควร mark COMPLETED จนกว่าเงินถึงมือ (หรืออย่างน้อยต้องมี
   สถานะ PENDING_PAYOUT)
3. นอกนั้นผลตรวจตรงกับรายงานเดิมทั้งหมด

## 3. ช่องโหว่ที่รายงานเดิมตกหล่น (เจอรอบนี้)
- **A. Score/verdict inconsistent ได้** (`judge.py:87-90`) — LLM ตอบขัดกันเองได้
  โค้ดไม่ normalize
- **B. กลืน exception ที่ route judge** (`app.py:201`) — ล้มเงียบ ตรวจสอบยาก
- **C. ไม่ validate authorization amount** — `confirm_authorization` ไม่เช็คว่า
  ยอดที่ lock ตรงกับ task amount (ปัจจุบันโอเคเพราะ order สร้างจากระบบเอง
  แต่ไม่มี assertion กัน regression)
- **D. ไม่มี rate limit / input size limit** — ยิง judge รัวๆ ได้ (เสียค่า LLM /
  ถล่ม PayPal API)

## 4. แยกตามเป้าหมาย

### ก่อนเดโม่ Sandbox (hackathon, ก่อน 13 พ.ย.) — ชุดเล็กสุดที่ปลอดภัยพอ
1. **Auth ขั้นต่ำทุก route ที่เปลี่ยนสถานะ/แตะเงิน** — server-side check ง่ายๆ
   (เช่น โทเคน/รหัสผ่านแอดมินจาก env) + ownership: requester เห็นเฉพาะ task ตัวเอง
2. **Human confirmation gate ก่อน capture/payout** — หน้า "ยืนยันการจ่าย" แสดง
   task, ผู้รับ, ยอด, หลักฐาน, verdict ก่อนยิงเงินจริง (server-enforced ไม่ใช่แค่ซ่อนปุ่ม)
3. **Judge fail-closed:** structured rubric ขั้นต่ำ (checklist จาก brief ที่ requester
   ติ๊กตอนสร้างงาน) + deterministic hard-fail (ก็อป brief/keyword stuffing/ความยาว
   ต่ำกว่าเกณฑ์ → reject ทันทีไม่ต้องถาม AI) + คะแนนแกว่ง/ไม่แน่ใจ → ส่งคนตรวจ
4. **กัน settlement ซ้ำ:** DB-level guard (update status แบบ compare-and-swap หรือ
   lock) + ปิด retry อัตโนมัติหลังผลคลุมเครือจนกว่าจะ reconcile
5. **แก้ threshold ให้ตรงกัน** (เลือกค่าเดียวทั้ง mock/LLM/prompt)
6. **Regression tests:** stuffing, copy-brief, missing-requirement, unauthorized
   settle, recipient tampering, double-settle, capture-ok/DB-fail

### ก่อนใช้เงินจริง (pilot/production) — ห้ามข้าม
- Idempotency key คงที่ต่อ operation + reconciliation กับ PayPal ก่อน retry
  (แก้ความเข้าใจข้อ 2.2 ข้างบน)
- ผูกตัวตน worker (เช่น PayPal login/PII check) + verify payout recipient
- แยกสถานะ payout (PENDING/SUCCESS/UNCLAIMED/FAILED) + retry/แจ้งเตือน
- Webhook + signature verify + periodic reconciliation
- Dispute/appeal flow + refund หลัง capture
- LLM hardening: sanitize input, schema validation, normalize score/verdict,
  วัด variance, prompt-injection tests
- Rate limiting, input size limits, audit log ว่าใครทำอะไรเมื่อไหร่

## 5. ทางลัดที่ปลอดภัยสำหรับเดโม่ (ตามที่ GPT ขอ)
- เงิน sandbox + ป้ายชัดว่า "simulated money" ทุกหน้า
- Human gate แบบง่าย (รหัสมนุษย์ยืนยันก่อนยิง capture) แทนระบบสิทธิ์เต็มรูป
- Mock judge ที่ hard-fail ชัดเจนดีกว่า LLM ที่ควบคุมไม่ได้ — สำหรับเดโม่
  "ตรวจแบบมีเกณฑ์ตายตัว" เล่าเรื่องง่ายกว่าและไม่แกว่ง
- อย่าเคลมว่า "AI ประเมินคุณภาพได้อย่างน่าเชื่อถือ" — พูดว่า "AI ช่วยกรอง
  เบื้องต้น + มนุษย์ตัดสินใจสุดท้าย"

## 6. ยังไม่ได้รับการยืนยัน (อย่าอ้าง)
- พฤติกรรม LLM judge จริง (ไม่เคยรัน), variance ของคะแนน
- พฤติกรรม PayPal เมื่อ capture ซ้ำบน auth ที่ปิดแล้ว (อ้างจาก docs ไม่ได้เทส)
- ประสิทธิภาพ/ความปลอดภัยภายใต้โหลด
