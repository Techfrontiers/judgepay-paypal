# 🏛️ JudgePay — PayPal Edition

> "We're not building payments for agents. We're building **financial judgment** for agents."

Adaptation of [JudgePay](README.classic.md) (conditional USDC escrow on Base, built for the
OpenClaw USDC Hackathon) to **PayPal rails**, for the **PayPal AI Hackathon 2026**.

The core idea is unchanged: money is **locked first**, a worker submits output, an **AI judge**
evaluates quality, and funds move **only if the work passes**. What changed is the money rail —
from smart contracts to the PayPal platform (Orders v2 + Payments v2 + Payouts v1).
(Webhook signature-verification helpers exist; no webhook receiver route is wired —
see Known limitations.)

## Architecture

```
┌────────────┐   create    ┌──────────────┐  approve (sandbox login)  ┌─────────────┐
│ Requester  │ ──────────▶ │ PayPal Order │ ───────────────────────▶ │ Authorize   │
│ (buyer)    │  intent=    │  (AUTHORIZE) │                           │ (lock funds)│
└────────────┘  AUTHORIZE └──────────────┘                           └──────┬──────┘
                                                                          │ OPEN
┌────────────┐  submit     ┌──────────────┐  evaluate   ┌────────────┐       │
│  Worker    │ ──────────▶ │ SQLite store │ ──────────▶ │ AI Judge   │ ◀─────┘
└────────────┘  output     │  (SUBMITTED) │  brief+output└─────┬──────┘
                           └──────────────┘                   │
                                              ┌──────────────┴──────────────┐
                                              ▼                             ▼
                                        approve                         reject
                                   capture + payout                   void (no money
                                   → COMPLETED                         moves) → REFUNDED
                                              timeout → void → EXPIRED
```

## Blockchain → PayPal mapping

| JudgePay (USDC/Base) | JudgePay PayPal Edition |
|---|---|
| `createTask` — USDC `transferFrom` locks funds in contract | `POST /v2/checkout/orders` (intent=AUTHORIZE) → buyer approves → `POST .../authorize` locks funds |
| `authorization_id` ↔ contract escrow balance | `authorization_id` — the key to the whole escrow |
| `evaluate(approve=true)` → release USDC | `POST /v2/payments/authorizations/{id}/capture` (`final_capture=true`) → then `POST /v1/payments/payouts` to worker |
| `evaluate(approve=false)` → refund USDC | `POST /v2/payments/authorizations/{id}/void` — hold released, money never moves |
| `claimTimeout` → requester reclaims | `expire_task` → void after deadline |
| on-chain events | PayPal API polling + reconcile (webhook receiver not wired — future work) |
| `PRIVATE_KEY` signs transactions | OAuth2 client-credentials (server-side only) |

## Why this fits the hackathon brief

- **Deep PayPal usage** (judging criterion: *Technological Implementation*): Orders v2,
  Payments v2 (authorize / capture / void), Payouts v1 — not a
  bolted-on pay button. (Webhook receiver is future work; state comes from API + reconcile.)
- **AI at the center** (*Innovation*): the AI judge is the only thing standing between
  locked money and payout. Money does not move on human clicks alone.
- **Real problem, real audience** (*Potential Impact*): freelancers and clients who don't
  trust each other; social-commerce sellers taking chat orders.
- **Coherent product + 3-min demo** (*Design*, *Presentation*): Flask UI with task flow
  and an AG Grid dashboard (also eligible for the AG Grid sponsor prize).
- **Agentic commerce angle**: an AI agent that holds and releases money autonomously —
  aligned with the "Best Use of Agentic Commerce" special prize.

## Setup

**Python 3.11+** required (3.12 tested; the code avoids 3.12-only syntax).

```bash
cd ~/workspace/judgepay-paypal
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 0) secrets บังคับ (fail-closed ถ้าขาด — ดู judgepay_paypal/config.py)
#    สร้างค่าจริงด้วย: python3 -c "import secrets; print(secrets.token_hex(32))"
export JUDGEPAY_AUTH_TOKEN=<64-hex-chars>   # ประตู operator
export JUDGEPAY_HMAC_SECRET=<64-hex-chars>   # ผูก confirmation token
export FLASK_SECRET_KEY=<64-hex-chars>       # session/CSRF

# 1) โหมด mock (ไม่ต้องมี PayPal credentials — เดโม่ได้ทันที)
export PAYPAL_MOCK=true JUDGE_LLM=mock

# 2) โหมด sandbox จริง — สร้าง app ที่ developer.paypal.com
#    (Apps & Credentials -> Sandbox -> Create App, เปิด feature Payouts)
export PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=...
export PAYPAL_MOCK=false
```

No secrets are committed — see `.env.example`. Never put credentials in code, logs, or git.

## Demo script (mock mode, offline)

```bash
export PAYPAL_MOCK=true JUDGE_LLM=mock
alias jp="python -m judgepay_paypal.cli"

# 1. requester สร้าง task -> ได้ approval_url
jp create -d "Write a 200-word product description for a ceramic coffee mug, mention dishwasher-safe and 350ml capacity" -a 25 --requester-email buyer@example.com

# 2. (จำลอง) buyer กดอนุมัติ -> authorize ล็อกเงิน
jp confirm 1

# 3. worker ส่งงาน
jp submit 1 -o "This 350ml ceramic coffee mug is dishwasher-safe and ..." --worker-email worker@example.com

# 4. AI ตัดสิน (ไม่แตะเงิน)
jp judge 1

# 5. คนยืนยันแล้ว settle (capture+payout ถ้าผ่าน, void ถ้าไม่ผ่าน)
jp settle 1

# ดูผล
jp get 1

# web UI + dashboard
python app.py   # http://localhost:5000  |  /dashboard = AG Grid
```

## CLI commands

| Command | What it does |
|---|---|
| `create -d … -a …` | create task + PayPal order (returns `approval_url`) |
| `confirm <id>` | authorize after buyer approval (locks funds) |
| `get <id>` | task details as JSON |
| `submit <id> -o …` | worker submits output |
| `evaluate <id> --approve/--reject` | manual verdict (no AI), then settle path |
| `judge <id>` | AI judge evaluates only — **no money moves** |
| `settle <id> [--approve-review]` | human-confirmed settlement: capture+payout (approve) **or** void (reject) |
| `expire <id>` | void + mark EXPIRED |
| `list [--status …]` | list tasks |

## Hackathon submission notes

- Repo must be **public** with a license; judges run it or open the hosted demo URL.
- Submission needs: text description, **working demo** (hosted URL or run instructions),
  tools used, and a **< 3 min YouTube demo video** showing end-to-end flow.
- Sandbox checklist before recording: real PayPal sandbox app (Payouts enabled),
  sandbox buyer login for the approval step, `PAYPAL_MOCK=false`.
- Suggested video beats: (1) create task → approval link, (2) authorize locks funds,
  (3) worker submits, (4) AI judge reasoning on screen, (5) approve → payout email /
  reject → void, (6) AG Grid dashboard.

## Project layout

```
judgepay_paypal/      # NEW — PayPal edition (this hackathon)
  paypal_client.py    # PayPal REST client + mock mode
  store.py            # SQLite task store
  judge.py            # AI judge (mock heuristic / OpenAI-compatible LLM)
  escrow.py           # state machine: create→confirm→submit→judge→settle→expire
  cli.py              # CLI mirroring the original command names
app.py                # Flask UI + AG Grid dashboard
scripts/              # ORIGINAL JudgePay (USDC/Base) — kept as history, untouched
contracts/            # ORIGINAL Solidity contracts — kept as history
research/             # PayPal API reference used to build this
```
