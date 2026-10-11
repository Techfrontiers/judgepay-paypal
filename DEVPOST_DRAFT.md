# Devpost Submission Draft — JudgePay (PayPal Edition)

> Copy-paste ready. Items marked TODO need sydear's input. Do NOT invent features,
> test results, or capabilities the system doesn't have.

---

## Project name
**JudgePay — Financial Judgment for the Agent Economy**

## Tagline
AI agents are becoming economic actors — but they can't judge quality. JudgePay locks payment with PayPal, lets an AI judge evaluate the work, and releases funds only when the work is actually good.

## Problem
Hiring through an AI agent today gives you two bad options: trust blindly and pay upfront, or micromanage every step. Payments are binary — pay, or don't pay. There is no such thing as paying *for good work*. Workers face the mirror problem: arbitrary rejection with no criteria-based recourse.

## Solution
JudgePay is a PayPal-native escrow with an AI judge in the middle:
1. The requester creates a task with a written brief, acceptance criteria, and budget → PayPal **authorizes** (locks) the funds. Not paid — held.
2. The worker submits the deliverable.
3. An **AI judge** evaluates the submission against the frozen criteria and returns a strict verdict (`approve`/`reject`) with a 0–100 score and reasoning.
4. A **human approves** the settlement — the AI can never move money on its own.
5. On approval PayPal **captures** the held payment and a **Payout** sends it to the worker. On rejection the authorization is **voided** — the hold is released, no money moves.

## AI innovation
- The AI judge is the only thing standing between locked money and payout — a strict JSON schema (integer score 0–100, exact verdict enum), frozen criteria hashed at task creation, prompt-injection defenses (submission wrapped as untrusted data), and fail-closed behavior: any judge error or ambiguity becomes human review, never an approval.
- Separation of powers by design: evaluation (AI) is structurally separated from money movement (human-confirmed settlement). Tests prove the judge path makes zero PayPal mutation calls.

## How PayPal is used (central, not bolted on)
- **Orders v2 (intent=AUTHORIZE)** — creates the payable task; buyer approves in PayPal checkout.
- **Payments v2 authorizations** — the escrow lock itself; `authorization_id` is the key to the whole state machine.
- **Payments v2 capture** (`final_capture=true`) — releases the held funds on human-approved settlement, with stable idempotency keys bound to the authorization (found and fixed a real cross-authorization replay bug during sandbox E2E).
- **Payments v2 void** — reject/expire path; money never moves.
- **Payments v2 refund** — dispute-resolution refunds from capture, with refundable-balance accounting.
- **Payouts v1** — pays the worker; item-level status tracking (a batch marked SUCCESS does not mean the recipient was paid — we track each payout item to `SUCCESS` before marking a task `COMPLETED`).

## Architecture & tech stack
Python + Flask + SQLite. Modules: `paypal_client` (PayPal REST + sandbox mock), `store` (durable ops/ledger/disputes/audit), `escrow` (state machine), `judge` (AI evaluation), `policy` (frozen criteria), `webhooks` (signature-verify/dedup helpers), `config`/`audit`/`logutil` (fail-closed config, append-only audit trail, scrubbed structured logging). CLI + AG Grid dashboard. 162 automated tests (mock/failure-injection/security/XSS), all green.

## Try it
```bash
git clone https://github.com/Techfrontiers/judgepay-paypal
cd judgepay-paypal
pip install -r requirements.txt
export PAYPAL_MOCK=true JUDGE_LLM=mock
export JUDGEPAY_AUTH_TOKEN=<32+ chars> JUDGEPAY_HMAC_SECRET=<32+ chars> FLASK_SECRET_KEY=<32+ chars>
python -m judgepay_paypal.cli create -d "<brief>" -a 25
# approve at the URL, then: confirm → submit → judge → settle
python -m pytest tests/   # 162 tests
```
For real PayPal sandbox: `PAYPAL_MOCK=false` + sandbox `PAYPAL_CLIENT_ID`/`PAYPAL_CLIENT_SECRET` (Payouts enabled on the app).

## Links
- GitHub: https://github.com/Techfrontiers/judgepay-paypal (public, MIT License)
- Demo video (< 3 min, YouTube): TODO
- Live demo URL: TODO (or "run locally per instructions above")

## Known limitations (honest)
- Hackathon prototype — not production financial infrastructure; do not use with real money.
- Auth is a single shared operator token (documented; no multi-user isolation claim).
- AI judge tested in mock/heuristic mode only — no real LLM evaluation run yet.
- Refund/dispute flows mock-tested only — not run against PayPal sandbox.
- Webhook receiver helpers exist and are unit-tested, but no webhook route is wired (Future Work).
- Sandbox E2E verified 2026-10-11: order → authorize → capture → payout **batch** SUCCESS, but payout **item** UNCLAIMED (test receiver not a registered PayPal account — the worker did NOT receive the money; PayPal holds it 30 days then returns it). Void on reject. Not re-run after final doc/security commits (PayPal integration code unchanged).

## Built with
PayPal Developer Platform (Orders, Payments, Payouts — sandbox), Python, Flask, SQLite, AG Grid. AI: mock/heuristic judge with OpenAI-compatible LLM interface (real-LLM path implemented, not yet exercised against a live model).

## Team
TODO — names/handles of team members (solo or team).

## Deadline note
Submit on Devpost before **Nov 12, 2026, 2:00 PM PT** (Nov 13, 04:00 Bangkok time). Submission is done by sydear in the Devpost UI — the assistant does not submit on their behalf.
