# JudgePay PayPal Edition — Demo Video Script (under 3:00)

> ⚠ SUPERSEDED — the canonical script is `DEMO_VIDEO_SCRIPT_EN.md` (used for the
> recorded video). This Thai draft is kept for reference only and still mentions
> the old `judge-settle` command (now split into `judge` + `settle`).
>
> ถ่ายตอน sandbox พร้อมแล้วเท่านั้น — ห้ามถ่ายด้วย mock mode
> Format: screen recording + voiceover, 1080p, เสียงชัด, ไม่มีเพลงมีลิขสิทธิ์

## Shot 0 — Hook (0:00–0:20)
**พูด:** "AI agents are becoming economic actors — but they can't judge quality.
Payments today are binary: pay, or don't pay. JudgePay gives AI agents
financial judgment: money moves only when the work is actually good."
**ภาพ:** หน้า landing / โลโก้ + สมการ Requester → Lock → Submit → AI Judge → Release/Refund

## Shot 1 — The problem in 15 seconds (0:20–0:35)
**พูด:** "Hiring a freelancer through an agent? Today you either trust blindly
or micromanage. JudgePay locks the payment with PayPal, lets the worker submit,
then an AI judge checks the deliverable against the brief — release on pass,
refund on fail."
**ภาพ:** dashboard เปล่า + ลูกศรโฟลว์

## Shot 2 — Create task, lock money (0:35–1:05)
**ทำจริง:**
1. รัน `create` — ใส่ brief + งบ $25
2. โชว์ approval URL → เปิด sandbox PayPal → ล็อกอิน test buyer → กดอนุมัติ
3. รัน `confirm` → โชว์ authorization ID, status AUTHORIZED
**พูด:** "The requester's $25 is now held by PayPal — not paid, just locked.
Nobody can run away with it."

## Shot 3 — Submit + AI judges (1:05–1:50)
**ทำจริง:**
1. รัน `submit` — ส่งงานตัวอย่างที่ดี
2. รัน `judge-settle` → โชว์ verdict: **approve**, score, reasoning ของ AI
3. โชว์ capture ID + payout batch ID → status COMPLETED
**พูด:** "The worker submits. The AI judge reads the brief, checks the work,
scores it — 74, pass. PayPal captures the held payment and pays the worker.
All automatic."

## Shot 4 — Reject path, fast (1:50–2:10)
**ทำจริง:** สร้าง task ที่สอง → submit งานห่วย ("ok done") → judge-settle →
verdict **reject** → void → REFUNDED, "no money moved"
**พูด:** "Bad work? The judge rejects, the hold is voided. The requester
loses nothing."

## Shot 5 — Dashboard (2:10–2:30)
**ภาพ:** เปิด /dashboard — AG Grid ตาราง tasks ทุกสถานะ + คอลัมน์ payment
(authorization / capture / payout / void)
**พูด:** "Every task, every dollar, fully auditable."

## Shot 6 — Close (2:30–2:55)
**พูด:** "JudgePay turns PayPal into a decision primitive for the agent economy —
escrow, AI evaluation, and settlement in one flow. Built with PayPal Orders,
Payments, Payouts, and Webhooks. Open source, link in the description."
**ภาพ:** สรุป 4 API ที่ใช้ + ลิงก์ GitHub repo

## Checklist ก่อนอัปโหลด
- [ ] ความยาว < 3:00 (กรรมการไม่ดูเกิน)
- [ ] เห็นของทำงานจริงบนจอทุก shot (ไม่ใช่ mockup)
- [ ] ไม่มีโลโก้/เพลงบุคคลที่สามโดยไม่ได้รับอนุญาต
- [ ] อัปโหลด YouTube แบบ public, แปะลิงก์ในฟอร์ม Devpost
- [ ] ใน description เขียน tools ที่ใช้: PayPal (Orders/Payments/Payouts/Webhooks) + AI judge (ระบุ model)
