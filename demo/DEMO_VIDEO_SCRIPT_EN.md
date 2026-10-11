# JudgePay — Demo Video Script (English, ≤ 2:50)

> Record ONLY in PayPal sandbox (`PAYPAL_MOCK=false`). Never record in mock mode.
> Format: screen recording + voiceover, 1080p, no copyrighted music.
> Every shot shows the real system on screen — no mockups standing in for missing features.

## Shot 1 — Problem (0:00–0:25)
**Say:** "Hiring through an AI agent? Today you have two bad options: trust blindly and pay upfront, or micromanage every step. Payments are binary — pay, or don't pay. There's no such thing as paying *for good work*."
**Show:** Title card: "JudgePay — financial judgment for the agent economy" + diagram: Requester → Lock → Submit → AI Judge → Release / Refund.

## Shot 2 — Solution (0:25–0:50)
**Say:** "JudgePay changes that. The requester locks payment with PayPal first. The worker submits the work. An AI judge checks it against the agreed criteria. Only good work gets paid — bad work gets refunded, automatically and provably."
**Show:** Architecture diagram: PayPal Orders (authorize) → AI Judge → Human Approval → Capture + Payout / Void. Labels: "AI evaluates, human approves, PayPal moves money."

## Shot 3 — Live demo: the happy path (0:50–1:50)
**Do (real commands, real sandbox):**
1. `create` — brief: product description for a ceramic mug, budget $1.00 → show approval URL
2. Open sandbox PayPal → log in as test buyer → approve (note: sandbox checkout page may hang on a spinner — the approval still goes through; verify via order status)
3. `confirm` → show authorization ID, status OPEN — "$1.00 is now held by PayPal. Not paid — locked."
4. `submit` — worker submits a good product description
5. `judge` → show verdict **approve**, score, and the AI's reasoning on screen — "The AI judges. But notice: no money moved yet."
6. `settle` (human confirmation) → show capture ID + payout batch ID, batch status SUCCESS
**Say:** "The worker submits. The AI judge reads the brief, checks the deliverable, scores it — pass. A human confirms — the AI can never move money on its own — and PayPal captures the held dollar and pays the worker."

## Shot 4 — Safety: the reject path (1:50–2:20)
**Do (real commands):**
1. New task → submit garbage ("ok done")
2. `judge` → verdict **reject** → status NEEDS_REVIEW
3. `settle` without approval → show the refusal: "task needs explicit human review decision first"
4. `settle --approve-review` → void → REFUNDED — "the hold is released, no money moved"
**Say:** "Bad work? The judge rejects. And even then, settlement needs a human. Nothing moves on AI output alone. The requester loses nothing."

## Shot 5 — Closing: value (2:20–2:50)
**Say:** "For buyers: pay for results, not promises — every dollar is traceable from lock to payout to refund. For workers: fair, criteria-based judgment instead of arbitrary rejection. JudgePay turns PayPal into a decision primitive for the agent economy — escrow, AI evaluation, and human-approved settlement in one flow. Built with PayPal Orders, Payments, and Payouts. Open source — link in the description."
**Show:** Dashboard with the task table (authorization / capture / payout / void columns) → GitHub repo link.

---

## Shot list (checklist for recording day)

| # | What to capture | Verify on screen |
|---|---|---|
| 1 | Title card + diagram | — |
| 2 | Architecture diagram | — |
| 3 | Terminal: create → approval URL; browser: sandbox approval; terminal: confirm / submit / judge / settle; payout batch SUCCESS | order APPROVED, auth CAPTURED, batch SUCCESS |
| 4 | Terminal: reject flow, settle refusal message, void → REFUNDED | NEEDS_REVIEW, refusal text, REFUNDED |
| 5 | Dashboard + repo link | task rows with payment columns |

## System prep (before recording)

- [ ] `git pull` latest main; `pytest` green
- [ ] Fresh DB (`JUDGEPAY_DB` unset or new file) — old task IDs are fine now (keys bind PayPal resources), but clean is cleaner
- [ ] Env: `PAYPAL_MOCK=false`, `PAYPAL_MODE=sandbox`, `JUDGE_LLM=mock`, real sandbox `PAYPAL_CLIENT_ID`/`PAYPAL_CLIENT_SECRET`, 32+ char `JUDGEPAY_AUTH_TOKEN` / `JUDGEPAY_HMAC_SECRET` / `FLASK_SECRET_KEY`
- [ ] Sandbox buyer account logged in (checkout page spinner may hang — approval still lands; confirm via `get`)
- [ ] Payout receiver: use a real sandbox receiver email if you want item SUCCESS on screen; otherwise `worker@example.com` shows UNCLAIMED (honest — say "unclaimed, returns in 30 days" if asked)
- [ ] Terminal font large enough to read at 1080p; clear terminal between shots
- [ ] Script total read-through timed ≤ 2:50

## What NOT to claim in the video

- Do NOT mention a webhook receiver — it is not wired (Future Work).
- Do NOT claim the LLM judge was tested against a real model — mock/heuristic only.
- Do NOT claim refunds were tested end-to-end in sandbox — mock only.
