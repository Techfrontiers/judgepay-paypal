# Phase B Plan — ก่อนเงินจริง (Blockers)
**สถานะ:** แผนรอตรวจ — **ห้ามเริ่มแก้โค้ด ห้ามใช้เงินจริงจนกว่าแผนจะอนุมัติ**
**อ้างอิงโค้ดปัจจุบัน:** commit `037adbef` (main, Techfrontiers/judgepay-paypal)

หลักการ: ความปลอดภัยและความถูกต้องของธุรกรรมมาก่อน — ทำตามลำดับความเสี่ยง B1 → B2 → B3/B5 → B4

---

## B1: Idempotency และ Transaction Recovery — P0
**ความเสี่ยง:** ยิงซ้ำ = เงินออกซ้ำ ที่โค้ดปัจจุบัน:
- `paypal_client.py:232` — `capture_authorization` ส่ง `PayPal-Request-Id: uuid4()` **ใหม่ทุกครั้ง**
  (timeout แล้ว retry = capture ซ้ำที่ PayPal)
- `payout_to_worker` — `sender_batch_id = judgepay-batch-{uuid4}` ใหม่ทุกครั้ง
  (PayPal dedupe ด้วย sender_batch_id → batch ใหม่ = จ่ายซ้ำ)
- ไม่มี operation log — crash ระหว่าง PayPal call กับ DB write เหลือแค่ NEEDS_RECONCILIATION
  โดยไม่รู้ว่า intent คืออะไร

**งาน:**
1. ตาราง `operations`: task_id, op_type (capture/payout/void/refund),
   idempotency_key เสถียร (`judgepay-{task_id}-{op_type}`), status
   (INTENDED → SENT → CONFIRMED/FAILED), paypal_ref, timestamps
2. เขียน INTENT **ก่อน** เรียก PayPal ทุกครั้ง; อัปเดตหลังได้ผล
3. ส่ง stable key ทุกครั้ง: capture ใช้ PayPal-Request-Id เดิม, payout ใช้ sender_batch_id เดิม
4. `reconcile --all` ตอน start +  periodic: ไล่ op ที่ค้าง SENT ไปถาม PayPal แล้วซ่อม
5. กฎเหล็กคงเดิม: ห้าม retry ด้วย key ใหม่เมื่อผลคลุมเครือ

**ไฟล์ที่กระทบ:** `paypal_client.py` (รับ idempotency_key), `store.py` (ตาราง operations),
`escrow.py` (`_settle_approve`/`_settle_reject` เขียน intent), `cli.py` (`reconcile --all`)

**Acceptance:**
- kill -9 ระหว่าง PayPal call กับ DB write → restart → reconcile กู้ได้ exactly-once
- ยิง capture ซ้ำด้วย key เดิม → PayPal ไม่สร้าง capture ที่สอง (ตรวจจาก API)
- regression tests เดิม 35 + ใหม่ ต้องเขียว

---

## B2: Payout Lifecycle — P0
**ความเสี่ยง:** งานถูก mark ว่าจ่ายแล้วทั้งที่เงินไม่ถึงมือ ที่โค้ดปัจจุบัน:
- `get_payout_batch` คืนแค่ `{batch_id, status}` — ไม่มี item-level
- non-SUCCESS → NEEDS_RECONCILIATION แล้วตัน (ไม่มีทางไปต่อที่นิยามไว้)

**งาน:**
1. เก็บ payout item: item_id, item_status
   (PENDING/SUCCESS/UNCLAIMED/FAILED/REFUNDED/RETURNED/BLOCKED), receiver, amount
2. Poller ตรวจ batch ที่ค้าง (cron หรือ on-demand) อัปเดต item states
3. นิยามทางออกแต่ละสถานะ:
   - UNCLAIMED (หมดอายุ 30 วัน) → แจ้งเตือน + เงินค้าง ไม่ mark COMPLETED
   - FAILED (ยืนยันแล้ว) → ออก batch ใหม่ด้วย sender_batch_id ใหม่ได้ **หลังยืนยันของเดิมล้มเหลว**
   - SUCCESS (item) → task COMPLETED ได้
4. แยกคอลัมน์ `payout_status` ออกจาก task status โดยสมบูรณ์

**ไฟล์ที่กระทบ:** `paypal_client.py` (parse items), `store.py` (ตาราง/คอลัมน์ payout),
`escrow.py` (payout state machine), worker/cron ใหม่

**Acceptance:**
- payout UNCLAIMED ไม่มีทาง mark task COMPLETED (test)
- ออก batch ใหม่หลัง FAILED ที่ยืนยันแล้ว ไม่เกิดจ่ายซ้ำ (sandbox test)
- item states ดูได้แยกจาก batch

**Dependency:** B1 (ต้องมี stable key ก่อนออก batch ใหม่ปลอดภัย)

---

## B3: LLM Judge Hardening — P1
**ความเสี่ยง:** ตัดสินผิด → จ่ายผิดคน/จ่ายงานห่วย ที่โค้ดปัจจุบัน (`judge.py`):
- `llm_evaluate` แทรก description/output ดิบลง prompt — ช่อง prompt injection เปิดโล่ง
- `json.loads` ตรงๆ ไม่มี schema validation — output ผิดรูป = crash (ไม่ fail-closed ตรงนี้)
- ไม่เคยทดสอบ LLM จริงเลย; ไม่มี variance/prompt-injection tests

**งาน:**
1. Schema validation: score int 0–100, verdict enum, reasoning str — ผิดรูป → NEEDS_REVIEW
   (fail-closed, ห้าม approve)
2. Prompt hardening: delimiter + สั่ง ignore คำสั่งที่แทรกมา; จำกัดความยาว input
3. Regression corpus: prompt injection, stuffing, copy-brief, verdict/score ขัดกัน,
   JSON ผิดรูป, variance (รันซ้ำ N ครั้ง — spread เกินเกณฑ์ → escalate)
4. เก็บ `judge_version` (model + prompt hash + policy version) ทุกครั้งที่ตัดสิน
5. คง invariant: AI evaluation แยกจาก settlement เด็ดขาด (มี test กันถดถอย)

**ไฟล์ที่กระทบ:** `judge.py`, `policy.py` (version), `store.py` (คอลัมน์ judge_version),
`tests/` (corpus ใหม่)

**Acceptance:**
- injection corpus ทั้งหมดไม่ขยับคะแนน / ถูก escalate
- output ผิด schema → NEEDS_REVIEW เสมอ ไม่ approve
- ทดสอบกับ LLM จริงใน sandbox (ควบคุม)

---

## B4: Dispute และ Refund — P1
**ความเสี่ยง:** จ่ายไปแล้วมีปัญหา ไม่มีทางแก้ ที่โค้ดปัจจุบัน: ไม่มีเลย

**งาน:**
1. Flow: เปิด dispute → สถานะ DISPUTED (settle ไม่ได้) → คนตรวจหลักฐาน →
   ตัดสิน: ยืนตามเดิม / refund / ชดเชย
2. ข้อจำกัด PayPal (ต้องตรวจก่อนออกแบบ): capture ขอ refund ได้
   (`/v2/payments/captures/{id}/refund`, เต็ม/บางส่วน); **payout ไป email ภายนอก
   ย้อนผ่าน API ไม่ได้** — ต้องขอคืนจากผู้รับ
3. ตาราง disputes + audit ของทุกการตัดสินใจ

**ไฟล์ที่กระทบ:** `store.py` (ตาราง disputes), `escrow.py`/`app.py` (routes),
`paypal_client.py` (`refund_capture`)

**Acceptance:**
- task ที่ DISPUTED settle ไม่ได้ (test)
- refund capture ผ่าน sandbox จริง
- มีบันทึกใครตัดสินอะไรเมื่อไหร่

**Dependency:** B1 (refund ต้อง idempotent), B5 (audit)

---

## B5: Security & Operational Readiness — P1 (P0 ถ้าจะเปิด public)
**ความเสี่ยง:** โดนเจาะ/เงินหายโดยไม่รู้ตัว ที่โค้ดปัจจุบัน:
- shared admin token ตัวเดียว, rate limit in-memory, ไม่มี webhook,
  ไม่มี audit trail, secret จัดการด้วย env อย่างเดียว

**งาน:**
1. Auth จริง: แทน shared token (per-user credential/SSO) + session ที่เหมาะสม
2. PayPal webhooks: listener + **verify signature** ทุก event, dedup ด้วย event_id,
   webhook → trigger reconcile
3. Audit trail: append-only (actor, action, task_id, timestamp, IP) ครอบทุก money action
4. Secret management: vault/rotation, สแกนห้ามมี secret ใน repo
5. Recipient verification (risk-based): ยืนยันว่า email เป็นของ worker จริงก่อนจ่ายก้อนใหญ่
6. Chaos tests: concurrency, partial failure, DB↔PayPal consistency checker

**ไฟล์ที่กระทบ:** `auth.py`, `app.py`, ใหม่ `webhooks.py`, `store.py` (audit_log),
deploy docs

**Acceptance:**
- webhook ปลอม/ซ้ำถูกปฏิเสธ (tests)
- ทุก money action มี audit entry
- สแกน repo ไม่เจอ secret; chaos tests เขียว

---

## Blockers ก่อนเงินจริง (checklist)
- [ ] B1: idempotency + recovery ครบ + ทดสอบ kill-9 จริง
- [ ] B2: payout lifecycle ครบ รวม UNCLAIMED/FAILED ของจริง
- [ ] B3: LLM hardened หรือ **ปิด LLM mode** เหลือ mock/policy อย่างเดียว
- [ ] B4: dispute/refund path นิยาม + ทดสอบ
- [ ] B5: auth จริง + webhook verify + audit trail + secret rotation
- [ ] credentials ใหม่ (rotate ของเดิม), pilot วงเงินจำกัด, แผน rollback

**คำตัดสินปัจจุบัน: Production NO-GO** จนกว่า checklist นี้จะครบ
