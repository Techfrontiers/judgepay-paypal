# JudgePay PayPal Edition — รายงานการตรวจสอบระบบ (Audit Report)
**วันที่:** 2026-10-11
**มุมมอง:** กรรมการตรวจรับงาน + ผู้ว่าจ้างที่ต้องจ่ายเงินจริง
**สถานะ:** ตรวจสอบโค้ด + ทดสอบจริง — **ยังไม่มีการแก้ไขโค้ดใดๆ ตามคำสั่ง**

## คำตัดสินสรุป (อ่านตรงนี้ก่อน)

**ระบบเหมาะสำหรับ: Sandbox Testing เท่านั้น**
**ไม่พร้อมสำหรับ:** Controlled Pilot ด้วยเงินจริง และ Production อย่างเด็ดขาด

ประเด็นที่ผู้ว่าจ้างกังวลได้รับการ**ยืนยันว่าเป็นจริง**: ระบบสามารถจ่ายเงินให้งานขยะได้
ทดสอบจริงพบว่า "งานขยะยัด keyword" ได้คะแนน **83/100 (ผ่าน)** สูงกว่า "งานดีจริง" ที่ได้
**74/100** — AI judge ตัวปัจจุบันแยกงานดีกับงานหลอกไม่ได้

---

## 1. สถาปัตยกรรมการตรวจรับ + อนุมัติจ่ายเงิน (ปัจจุบัน)

```
Requester                          PayPal Sandbox              Worker
   |                                    |                        |
   |-- create_task(desc, amount) ------->|                        |
   |    POST /v2/checkout/orders        |                        |
   |    (intent=AUTHORIZE)              |                        |
   |<-- approval_url -------------------|                        |
   |-- [browser] buyer อนุมัติ -------->|                        |
   |-- confirm_authorization() -------->|                        |
   |    POST .../orders/{id}/authorize  |                        |
   |<-- authorization_id (เงินโดน hold) -|                        |
   |                               OPEN |                        |
   |                                    |<-- submit_work(output) -|
   |                              SUBMITTED                      |
   |-- judge_and_settle()               |                        |
   |    AIJudge.evaluate(desc, output)  |                        |
   |    approve -> capture + payout     |                        |
   |      POST .../authorizations/{id}/capture (final_capture=true)
   |      POST /v1/payments/payouts -> worker_email
   |    reject -> POST .../authorizations/{id}/void
```

**จุดสำคัญ:** เส้นทางจาก "AI ตัดสิน" ไป "เงินออก" เป็น**เส้นตรง ไม่มีด่านมนุษย์ ไม่มี
policy engine** — `judge_and_settle()` เรียก `AIJudge.evaluate()` แล้วถ้า verdict
เป็น approve จะยิง capture + payout ทันทีในฟังก์ชันเดียวกัน (`_settle_approve`)

---

## 2. ตอบ 10 ประเด็น

### 2.1 Scope of Work — ใครกำหนด เก็บที่ไหน ใช้อะไรอ้างอิง
- ผู้กำหนด: requester ผ่านพารามิเตอร์ `description` (ข้อความอิสระ) ตอน `create_task`
- เก็บที่: คอลัมน์ `tasks.description` (TEXT) ใน SQLite — **ไม่มีโครงสร้าง**
- แหล่งอ้างอิงตอนตรวจ: สตริง `description` ตัวเดียวกัน ถูกส่งให้ AI ทั้งดุ้น
- **ช่องว่าง:** ไม่มีฟิลด์สำหรับ deliverables list, ข้อห้าม, รูปแบบไฟล์ที่รับ,
  ไม่มี versioning — ถ้า requester แก้ไข scope หลังสร้าง task จะไม่มีร่องรอย

### 2.2 Acceptance Criteria — มีเกณฑ์ชัดก่อนเริ่มงานหรือไม่
- **ไม่มี** เกณฑ์แบบมีโครงสร้างเลย โค้ดปัจจุบันมีแค่ `description` ข้อความเดียว
- ไม่มี: checklist ทีละข้อ, เกณฑ์ผ่าน/ตกแบบเจาะจง (เช่น "ต้องมี X ห้ามมี Y"),
  เงื่อนไข auto-reject ที่ไม่ขึ้นกับคะแนนรวม
- mock judge มีกฎซ่อนอยู่ 2 ข้อ (keyword coverage 70% + ความยาว 30%) แต่**ไม่ได้
  แสดงให้ requester/worker เห็นก่อนเริ่มงาน** และไม่ใช่ acceptance criteria จริง

### 2.3 AI Evaluation — ใช้หลักฐานอะไร คะแนน 74 มาจากไหน
**Mock judge** (ที่ใช้ใน sandbox test):
- หลักฐานเดียว: **keyword overlap** ระหว่าง brief กับ output (ตัด stopwords,
  คำยาว >3 ตัวอักษร) ผสม **ความยาวตัวอักษร** (output/200)
- สูตร: `score = 100 × (0.7 × coverage + 0.3 × length_score)`
- **ไม่ได้ตรวจทีละข้อ** — ไม่มีการแตก brief เป็นข้อๆ แล้วเช็คทีละข้อ
- เกณฑ์ผ่าน: **score ≥ 60** (แต่ LLM prompt เขียนว่า ≥ 70 — **ไม่ตรงกัน**)
- คะแนน 74 ใน sandbox มาจาก: keyword coverage 62% (5/8 คำ) + ความยาว 310 ตัวอักษร
  → `100 × (0.7×0.62 + 0.3×1.0)` ≈ 74 — คือ "มีคำตรงกันพอสมควร + ยาวพอ" เท่านั้น
  **ไม่ใช่การยืนยันคุณภาพ**

**LLM judge** (โหมดจริง): ส่ง brief + output ให้ LLM ตอบ JSON {score, verdict, reasoning}
- ดีกว่า mock แต่: ไม่มี structured rubric, ไม่มี few-shot examples, ไม่มี
  temperature/seed control ที่รับประกันความคงเส้นคงวา, **ไม่เคยถูกทดสอบเลย**
  (ไม่มี API key ตอนเทส — โหมดนี้เป็นโค้ดที่ยังไม่เคยรัน)

### 2.4 Quality Assurance — จับงานไม่ดีได้ไหม มี verification อิสระไหม
**ผลทดสอบจริง (รัน 2026-10-11, mock judge):**

| งานที่ส่ง | คะแนน | ผล | หมายเหตุ |
|---|---|---|---|
| งานดีจริง (สรุป 3 ข้อถูกต้อง) | 74 | ✅ ผ่าน | baseline |
| **ขยะยัด keyword** ("prizes rules deadline hackathon paypal" วนซ้ำ) | **83** | ✅ **ผ่าน** | **สูงกว่างานดีจริง** |
| **ก็อป brief มาส่งดุ้นๆ** | **83** | ✅ **ผ่าน** | ไม่ได้ทำงานเลยแต่ผ่าน |
| งานไม่เกี่ยว (พิซซ่า/แมว) | 12 | ❌ ตก | จับได้เฉพาะกรณีชัดๆ |
| งานเปล่า | 0 | ❌ ตก | |
| "done ok" | 1 | ❌ ตก | |

- **ไม่มี verification อิสระจาก AI เลย** — ไม่มี plagiarism check, ไม่มี format
  validator, ไม่มี human review queue, ไม่มี second-opinion model
- งานปลอม/งานคัดลอก/งานที่ดูถูกแต่ใช้ไม่ได้จริง: **จับไม่ได้** ถ้ามี keyword ครบ

### 2.5 Payment Authorization — AI สั่งจ่ายตรงได้ไหม มีด่านก่อนเงินออกไหม
- **AI สั่งจ่ายตรงได้** — `judge_and_settle` → `_settle_approve` ยิง capture +
  payout ในทันที ไม่มี policy engine ไม่มีมนุษย์ยืนยัน
- เงื่อนไขก่อนปล่อยเงิน (ทั้งหมดที่มี): task ต้องอยู่สถานะ SUBMITTED + AI verdict
  เป็น approve — **แค่นั้น**
- ไม่มี: วงเงินสูงสุดต่อ task, ยืนยันตัวตน worker, ตรวจสอบว่า worker_email ตรงกับ
 คนส่งงาน, ระงับเมื่อพฤติกรรมผิดปกติ, รายชื่อผู้มีอำนาจอนุมัติ

### 2.6 Fraud / Adversarial Cases
| การโจมตี | ผลกับระบบปัจจุบัน |
|---|---|
| ยัด keyword ให้ครบ brief | **ผ่าน 83/100 → ได้เงิน** |
| ก็อป brief มาส่งเป็น "งาน" | **ผ่าน 83/100 → ได้เงิน** |
| ส่งงาน → เปลี่ยน worker_email เป็นอีเมลตัวเอง | ได้ — ไม่มีผูกตัวตน worker กับงาน |
| ใครก็ได้กดปุ่ม "Run AI judge" บนเว็บ | ได้ — **ไม่มี login/auth ใดๆ ทุก route** |
| ยิง judge_and_settle ซ้ำหลัง capture | ติด state guard (COMPLETED แล้ว) — แต่ถ้า process ตายระหว่าง capture กับบันทึก DB → รันซ้ำจะยิง capture ซ้ำ (PayPal-Request-Id สุ่มใหม่ทุกครั้ง ไม่ idempotent) |
| ส่งงานคุณภาพก้ำกึ่งหลายรอบ | AI ให้คะแนนไม่คงที่ (โดยเฉพาะ LLM) — ไม่มีกลไกกันคะแนนแกว่ง |

### 2.7 Dispute / Appeal
- **ไม่มีเลย** — ไม่มีสถานะ DISPUTED ที่ใช้งานจริง (มีใน STATUS_NAMES ของโค้ดเก่า
  blockchain แต่ PayPal edition ไม่ใช้), ไม่มีปุ่มโต้แย้ง, ไม่มี hold เงินรอคนกลาง,
  ไม่มีกลไกกันจ่ายซ้ำเมื่อมีข้อพิพาท
- ถ้า requester ไม่พอใจหลังจ่ายแล้ว: เงินออกไปแล้วผ่าน capture+payout — **เรียกคืนไม่ได้**
  (มีแค่ refund หลัง capture ซึ่งโค้ดยังไม่ implement)

### 2.8 Auditability
**มี:** description, worker_output, judge_score/verdict/reasoning, order_id,
authorization_id, capture_id, payout_batch_id, created_at, deadline_at —
ตรวจสอบย้อนหลังได้ว่า "งานนี้ได้คะแนนเท่าไหร่เพราะเหตุผลอะไร และเงินไปไหน"

**ไม่มี:** ประวัติเวอร์ชันของเกณฑ์, บันทึกว่า**ใคร**กดปุ่มไหนเมื่อไหร่ (ไม่มี actor
log), hash ของ output ป้องกันแก้ไขหลังส่ง, การยืนยันความถูกต้องของ webhook
(ไม่มี webhook handler เลย — สถานะเงินรู้แค่ตอน poll)

### 2.9 Testing Evidence
**ที่ผ่านการทดสอบแล้ว (มีหลักฐาน):**
- Mock E2E: create→confirm→submit→judge→settle ครบ 3 เส้นทาง (approve/reject/expire)
- Sandbox E2E จริง: OAuth, create order, buyer approve, authorize, submit,
  AI judge (mock), capture, payout (หลังแก้ sync_mode) — มี transaction IDs จริง
- Error paths: double-confirm, submit ซ้ำ, task ไม่มีอยู่

**ยังไม่เคยทดสอบ:**
- LLM judge mode (ไม่มี API key — โค้ดส่วนนี้ไม่เคยรันแม้แต่ครั้งเดียว)
- Webhook (ไม่มีโค้ด)
- Concurrent judge_and_settle สอง process พร้อมกัน (race condition)
- Process ตายระหว่าง capture→DB update (crash consistency)
- PayPal API ล่ม/timeout ระหว่างทาง (retry มีแค่ PayPal-Request-Id ต่อ request)
- Authorization หมดอายุ 29 วันแล้วค่อย judge
- Payout ไปอีเมลที่ไม่มีบัญชี (UNCLAIMED) — จะเกิดอะไรต่อไม่มีโค้ดรองรับ

### 2.10 Gap Analysis

| มีอยู่จริงในโค้ด | ผ่านการทดสอบแล้ว |
|---|---|
| Escrow state machine (6 สถานะ) | ✅ mock + sandbox |
| PayPal: OAuth, Orders(authorize), capture, void, payouts(async+poll) | ✅ sandbox จริง |
| Mock AI judge | ✅ (และพิสูจน์แล้วว่าหลอกง่าย) |
| Web UI + AG Grid dashboard | ✅ เฉพาะ HTTP 200 (ยังไม่เคยเปิดในเบราว์เซอร์จริง) |
| SQLite audit trail | ✅ |

| ยังไม่มี (ต้องสร้าง) | หมายเหตุ |
|---|---|
| Structured acceptance criteria | ไม่มีเลย |
| Per-requirement checking | ไม่มีเลย |
| Human-in-the-loop / requester confirm | ไม่มี — AI ตัดสินแล้วเงินออกทันที |
| Authentication ทุก route | ไม่มี — ใครก็กดจ่ายเงินได้ |
| Dispute/appeal flow | ไม่มี |
| Fraud detection (plagiarism, stuffing) | ไม่มี |
| Crash-safe settle (idempotency) | PayPal-Request-Id สุ่มใหม่ทุกครั้ง |
| Webhook handler + reconciliation | ไม่มี |
| LLM judge ที่ทดสอบแล้ว | โค้ดมีแต่ไม่เคยรัน |
| Refund หลัง capture | ไม่มี |

| ที่เป็นแค่แผน/ข้อเสนอแนะ (อย่าเอาไปอ้าง) |
|---|
| "AI ตรวจงานอย่างชาญฉลาด" — ของจริงคือ keyword matching |
| "ปลอดภัยสำหรับเงินจริง" — ยังไม่มีหลักฐาน |
| Dashboard ใช้ประเมินความเสี่ยงได้ — เป็นแค่ตารางแสดงผล |

---

## 3. รายการช่องโหว่ที่ทำให้จ่ายเงินให้งานไม่ผ่าน (เรียงตามความรุนแรง)

1. **P0 — AI judge หลอกง่าย (พิสูจน์แล้ว):** keyword stuffing / ก็อป brief ได้ 83/100
   → เงินออกให้งานขยะโดยถูกต้องตามโค้ด
2. **P0 — ไม่มี authentication:** ใครเข้าถึงเว็บได้ก็สร้าง task ด้วยเงิน requester,
   ส่งงาน, กด judge ให้จ่ายเงินได้
3. **P0 — ไม่มี human approval:** AI verdict = คำสั่งจ่ายเงินทันที ไม่มีมนุษย์ขวาง
4. **P1 — ไม่ผูกตัวตน worker:** คนส่งงานใส่อีเมลใครก็ได้ เงิน payout ไปอีเมลนั้น
5. **P1 — เกณฑ์ผ่านไม่ตรงกัน:** mock ≥60 แต่ LLM prompt ≥70 — ย้ายโหมดแล้ว
   พฤติกรรมเปลี่ยนโดยไม่มีใครรู้
6. **P1 — ไม่ idempotent:** process ตายจังหวะ capture→DB เสี่ยงยิง capture ซ้ำ
7. **P2 — ไม่มี dispute:** จ่ายแล้วจบ เรียกคืนไม่ได้ (ไม่มี refund flow)
8. **P2 — LLM judge ไม่เคยทดสอบ:** ความคงเส้นคงวา/ prompt injection ไม่รู้เลย
9. **P2 — ไม่มี webhook/reconciliation:** สถานะเงินจริงกับ DB อาจไม่ตรงกันโดยไม่รู้ตัว
10. **P3 — Payout ไปอีเมลลอย:** ได้สถานะ UNCLAIMED ไม่มีโค้ดตามต่อ

---

## 4. Test cases ที่ต้องมีก่อนขยับสถานะ (ยังไม่รัน — เสนอให้อนุมัติก่อน)

- [ ] T1 งานผ่านจริง → approve + เงินถึง worker ที่ถูกตัว
- [ ] T2 งานขยะยัด keyword → ต้อง reject (ปัจจุบัน FAIL — ต้องแก้ judge ก่อน)
- [ ] T3 ก็อป brief มาส่ง → ต้อง reject (ปัจจุบัน FAIL)
- [ ] T4 งานขาดองค์ประกอบสำคัญ (เช่น สรุป 2 ข้อจากที่ขอ 3) → ต้อง reject
- [ ] T5 ส่งงานซ้ำหลัง settle → ต้องถูกปฏิเสธโดยไม่ยิงเงินซ้ำ
- [ ] T6 process ตายหลัง capture → restart แล้วต้องไม่ capture ซ้ำ
- [ ] T7 authorization หมดอายุก่อน judge → ต้องจัดการอย่างสง่างาม
- [ ] T8 payout ล้ม (อีเมลไม่มีบัญชี) → ต้องมีสถานะค้างและ retry ได้
- [ ] T9 เรียก API พร้อมกัน 2 ครั้ง (double-click) → เงินออกครั้งเดียว
- [ ] T10 LLM judge ให้คะแนนงานชุดเดิม 10 รอบ → วัดความแกว่ง

---

## 5. ข้อสรุปสถานะ

| ระดับ | เหมาะสม? | เหตุผล |
|---|---|---|
| **Sandbox Testing** | ✅ **ใช่ (ปัจจุบัน)** | เงินจำลอง ใช้สาธิตแนวคิดและเดโม่ hackathon ได้ |
| **Controlled Pilot (เงินจริง, วงจำกัด)** | ❌ ยังไม่ได้ | ต้องมี P0 ครบ 3 ข้อก่อน: judge ที่หลอกยาก + auth + human approval |
| **Production** | ❌ ห่างไกล | ต้องมี P0–P2 ทั้งหมด + webhook reconciliation + dispute + การทดสอบโหลด/ความปลอดภัย |

**ความเห็นกรรมการ:** โครง escrow (authorize→capture/void) ถูกต้องและพิสูจน์แล้วว่า
ทำงานได้จริงกับ PayPal — นี่คือส่วนที่แข็งแรงที่สุดของระบบ ส่วนที่อันตรายคือ
**ชั้นตัดสินคุณภาพ** ซึ่งปัจจุบันเป็นเพียง keyword matching ที่หลอกได้ใน 10 วินาที
และ**ไม่มีมนุษย์อยู่ในวงจรเงินเลย** อย่าสับสนระหว่าง "โอนเงินสำเร็จ" กับ "จ่ายเงิน
อย่างถูกต้อง" — ระบบทำอย่างแรกได้ แต่อย่างหลังยังไม่ได้
