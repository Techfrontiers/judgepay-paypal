#!/usr/bin/env python3
"""JudgePay PayPal Edition — web UI (Flask).

Phase A / sandbox only. กฎ:
- ทุก POST ที่เปลี่ยนสถานะ/แตะเงิน ต้องผ่าน require_auth + rate limit + task ownership
- AI judge ไม่จ่ายเงิน — ต้องมีมนุษย์ยืนยันบนหน้า review (HMAC token) ก่อน settle
- ห้าม PAYPAL_MODE=live เว้นแต่ JUDGEPAY_ALLOW_LIVE=1 (guard ทั้งตอน start และใน client)

หน้า:
  /                 รายการ task ทั้งหมด
  /task/<id>        รายละเอียด + ฟอร์มส่งงาน + ปุ่ม Run AI judge
  /task/<id>/review ตรวจงาน + ยืนยัน settlement (ต้อง login)
  /create           ฟอร์มสร้าง task (ต้อง login)
  /dashboard        ตาราง AG Grid (CDN)
  /login, /logout   ประตู token ชั่วคราวสำหรับเดโม่
รัน:  python app.py   (แล้วเปิด http://localhost:5000)
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, g, request, redirect, url_for, render_template_string, session, jsonify  # noqa: E402

from judgepay_paypal.escrow import EscrowService, EscrowError  # noqa: E402
from judgepay_paypal.auth import (  # noqa: E402
    require_auth, rate_limited, current_owner_id,
    login_with_token, logout as auth_logout, new_csrf_token,
)

# ---- startup guard: live ถูกบล็อกใน Phase A ----
if os.getenv("PAYPAL_MODE", "sandbox").lower() == "live" \
        and os.getenv("JUDGEPAY_ALLOW_LIVE") != "1":
    raise RuntimeError(
        "PAYPAL_MODE=live is BLOCKED (Phase A = sandbox only). "
        "Set JUDGEPAY_ALLOW_LIVE=1 to override."
    )

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "judgepay-demo-secret")


def svc():
    return EscrowService()


def paypal_mode():
    try:
        return svc().paypal.mode + (" (mock)" if svc().paypal.mock else "")
    except Exception:
        return "unknown"


BASE_HTML = """
<!doctype html><html><head><meta charset="utf-8">
<title>JudgePay — PayPal Edition</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:960px;margin:2em auto;padding:0 1em;color:#222}
 h1 a{color:#003087;text-decoration:none}
 .badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:.85em;color:#fff}
 .AWAITING_APPROVAL{background:#f0a500}.OPEN{background:#0070ba}
 .SUBMITTED{background:#7b1fa2}.JUDGED{background:#00838f}.NEEDS_REVIEW{background:#ef6c00}
 .SETTLING{background:#455a64}.COMPLETED{background:#2e7d32}
 .REFUNDED{background:#757575}.EXPIRED{background:#c62828}.NEEDS_RECONCILIATION{background:#b71c1c}
 table{border-collapse:collapse;width:100%;margin:1em 0}
 th,td{border:1px solid #ddd;padding:8px;text-align:left;font-size:.9em}
 th{background:#003087;color:#fff}
 .card{border:1px solid #ddd;border-radius:8px;padding:1em;margin:1em 0;background:#fafafa}
 .btn{background:#0070ba;color:#fff;border:0;padding:8px 16px;border-radius:6px;cursor:pointer}
 .btn.warn{background:#c62828}.btn.go{background:#2e7d32}
 input,textarea,select{width:100%;padding:8px;margin:4px 0 12px;box-sizing:border-box}
 pre{background:#111;color:#0f0;padding:1em;border-radius:8px;overflow:auto;font-size:.8em}
 .nav a{margin-right:1em}
 .flash{background:#fff3cd;border:1px solid #ffc107;padding:8px;border-radius:6px;margin:1em 0}
 .sandbox{background:#e3f2fd;border:2px dashed #0070ba;padding:8px;border-radius:6px;margin:1em 0;font-weight:bold}
 code{background:#eee;padding:2px 6px;border-radius:4px}
</style></head><body>
<h1><a href="/">🏛️ JudgePay <small style="color:#0070ba">PayPal Edition</small></a></h1>
<div class="sandbox">🧪 SANDBOX MODE — simulated money, NOT real funds (%%MODE%%)</div>
<div class="nav"><a href="/">Tasks</a><a href="/create">+ New task</a><a href="/dashboard">📊 Dashboard</a>
%%AUTHNAV%%</div>
<hr>%%BODY%%</body></html>
"""


def page(body, flash=""):
    f = f'<div class="flash">{flash}</div>' if flash else ""
    authed = bool(session.get("authed"))
    authnav = '<a href="/logout">🔓 Logout</a>' if authed else '<a href="/login">🔑 Login</a>'
    html = BASE_HTML.replace("%%BODY%%", f + body)
    html = html.replace("%%AUTHNAV%%", authnav)
    html = html.replace("%%MODE%%", paypal_mode())
    return render_template_string(html)


def badge(status):
    return f'<span class="badge {status}">{status}</span>'


def csrf_field():
    tok = session.get("csrf") or new_csrf_token()
    return f'<input type="hidden" name="csrf_token" value="{tok}">'


def _owned(task_id):
    """ดึง task + เช็คว่าเป็นของ token นี้; ไม่ใช่ -> (None, error_response)."""
    try:
        t = svc().get_task(task_id)
    except EscrowError as e:
        return None, (page("", flash=str(e)), 404)
    if t.get("owner_id") != current_owner_id():
        return None, (jsonify({"error": "forbidden: not your task"}), 403)
    return t, None


def parse_criteria(text):
    """บรรทัดละ 1 ข้อ; ขึ้นต้นด้วย ! = ข้อบังคับ."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        required = line.startswith("!")
        out.append({"text": line[1:].strip() if required else line, "required": required})
    return out


# ---------- read-only ----------

@app.route("/")
def index():
    tasks = svc().list_tasks()["tasks"]
    rows = "".join(
        f"<tr><td><a href='/task/{t['id']}'>#{t['id']}</a></td>"
        f"<td>{(t['description'] or '')[:60]}</td>"
        f"<td>{t['amount']:.2f} {t['currency']}</td>"
        f"<td>{badge(t['status'])}</td>"
        f"<td>{t['judge_score'] if t['judge_score'] is not None else '-'}</td>"
        f"<td>{t['created_at'][:16].replace('T', ' ')}</td></tr>"
        for t in tasks
    )
    return page(f"""
<h2>Tasks ({len(tasks)})</h2>
<table><tr><th>ID</th><th>Description</th><th>Amount</th><th>Status</th>
<th>AI score</th><th>Created</th></tr>{rows or '<tr><td colspan=6>No tasks yet — <a href="/create">create one</a></td></tr>'}</table>
""")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if login_with_token(request.form.get("token", "")):
            return redirect(url_for("index"))
        return page("", flash="Invalid token.")
    return page("""
<h2>🔑 Operator login</h2>
<p>Demo gate: enter the <code>JUDGEPAY_AUTH_TOKEN</code> (never shown or stored in the page).</p>
<form method="post">
<label>Token</label><input name="token" type="password" autocomplete="off" required>
<button class="btn" type="submit">Login</button></form>""")


@app.route("/logout")
def logout():
    auth_logout()
    return redirect(url_for("index"))


# ---------- mutating (auth ทั้งหมด) ----------

@app.route("/create", methods=["GET", "POST"])
@require_auth
@rate_limited
def create():
    if request.method == "POST":
        try:
            r = svc().create_task(
                request.form["description"],
                float(request.form["amount"]),
                request.form.get("currency", "USD"),
                int(request.form.get("deadline", 24)),
                request.form.get("requester_email", ""),
                owner_id=current_owner_id(),
                acceptance_criteria=parse_criteria(request.form.get("criteria", "")),
            )
        except EscrowError as e:
            return page("", flash=f"Error: {e}")
        return redirect(url_for("detail", task_id=r["task_id"]))
    return page(f"""
<h2>New escrow task</h2>
<form method="post">{csrf_field()}
<label>Task brief (what the worker must deliver)</label>
<textarea name="description" rows="4" required placeholder="e.g. Write a 200-word product description for a ceramic coffee mug, mention dishwasher-safe and 350ml"></textarea>
<label>Acceptance criteria (one per line; prefix <code>!</code> = required)</label>
<textarea name="criteria" rows="4" placeholder="!mention dishwasher-safe&#10;!mention 350ml&#10;at least 150 words"></textarea>
<label>Amount</label><input name="amount" type="number" step="0.01" min="0.01" value="25.00" required>
<label>Currency</label><select name="currency"><option>USD</option><option>THB</option><option>EUR</option></select>
<label>Deadline (hours)</label><input name="deadline" type="number" value="24" min="1">
<label>Requester email (PayPal buyer)</label><input name="requester_email" type="email" placeholder="buyer@example.com">
<button class="btn" type="submit">Create task → get PayPal approval link</button>
</form>""")


@app.route("/task/<int:task_id>")
def detail(task_id):
    msg = request.args.get("msg", "")
    try:
        t = svc().get_task(task_id)
    except EscrowError as e:
        return page("", flash=str(e))
    t = {k: (v if v is not None else "") for k, v in t.items() if k != "success"}

    approve_box = ""
    if t["status"] == "AWAITING_APPROVAL":
        approve_box = f"""
<div class="card"><b>Step 1 — Requester approves the PayPal order</b><br>
<a class="btn" href="/task/{task_id}/approve-link" target="_blank">Open PayPal approval page</a>
<span style="margin-left:1em">แล้วกดปุ่ม confirm ด้านล่าง (จำลองว่า buyer กดอนุมัติแล้ว)</span><br><br>
<form method="post" action="/task/{task_id}/confirm" style="display:inline">{csrf_field()}
<button class="btn go" type="submit">✓ Confirm authorization (lock funds)</button></form></div>"""

    submit_box = ""
    if t["status"] == "OPEN":
        submit_box = f"""
<div class="card"><b>Step 2 — Worker submits work</b>
<form method="post" action="/task/{task_id}/submit">{csrf_field()}
<textarea name="output" rows="5" required placeholder="paste the deliverable here"></textarea>
<label>Worker PayPal email (for payout — locked on first submit)</label>
<input name="worker_email" type="email" placeholder="worker@example.com">
<button class="btn" type="submit">Submit work</button></form></div>"""

    judge_box = ""
    if t["status"] == "SUBMITTED":
        judge_box = f"""
<div class="card"><b>Step 3 — AI judge scores the work (no money moves)</b><br>
<form method="post" action="/task/{task_id}/judge" style="display:inline">{csrf_field()}
<button class="btn go" type="submit">🤖 Run AI judge</button></form>
<span style="margin-left:1em">score ≥ 80 → human review → settle · 60–79 → human decides · &lt;60 → void</span></div>"""

    review_box = ""
    if t["status"] in ("JUDGED", "NEEDS_REVIEW"):
        review_box = f"""
<div class="card"><b>Step 4 — Human review &amp; settlement</b><br>
<a class="btn go" href="/task/{task_id}/review">👁 Review &amp; approve settlement</a>
<span style="margin-left:1em">AI cannot move money by itself.</span></div>"""

    criteria_html = ""
    try:
        crits = json.loads(t.get("acceptance_criteria") or "[]")
        if crits:
            items = "".join(
                f"<li>{'❗' if c.get('required') else '·'} {c.get('text', '')}</li>"
                for c in crits)
            criteria_html = f"<div class='card'><b>Acceptance criteria:</b><ul>{items}</ul></div>"
    except ValueError:
        pass

    return page(f"""
<h2>Task #{t['id']} {badge(t['status'])}</h2>
<div class="card">
<b>Brief:</b> {t['description']}<br>
<b>Amount:</b> {t['amount']} {t['currency']} · <b>Deadline:</b> {t['deadline_at'][:16].replace('T',' ')}<br>
<b>Order:</b> <code>{t['order_id']}</code> · <b>Auth:</b> <code>{t['authorization_id']}</code><br>
<b>Capture:</b> <code>{t['capture_id']}</code> · <b>Payout batch:</b> <code>{t['payout_batch_id']}</code><br>
<b>Requester:</b> {t['requester_email']} · <b>Worker (locked):</b> {t['worker_email']}
</div>
{criteria_html}
{t['worker_output'] and f"<div class='card'><b>Worker output:</b><pre style='background:#fff;color:#222'>{t['worker_output'][:2000]}</pre></div>" or ""}
{t['judge_verdict'] and f"<div class='card'><b>AI judge:</b> score {t['judge_score']} → {t['judge_verdict'].upper()}<br><i>{t['judge_reasoning']}</i></div>" or ""}
{approve_box}{submit_box}{judge_box}{review_box}
""", flash=msg)


@app.route("/task/<int:task_id>/approve-link")
def approve_link(task_id):
    try:
        t = svc().get_task(task_id)
    except EscrowError as e:
        return page("", flash=str(e))
    oid = t.get("order_id", "")
    url = f"https://www.sandbox.paypal.com/checkoutnow?token={oid}"
    return page(f"<h2>PayPal approval</h2><p>Order <code>{oid}</code></p>"
                f"<p><a class='btn' href='{url}' target='_blank'>Open in PayPal sandbox</a></p>"
                f"<p><code>{url}</code></p>"
                f"<p><a href='/task/{task_id}'>← back to task</a></p>")


@app.route("/task/<int:task_id>/review")
@require_auth
def review(task_id):
    t, err = _owned(task_id)
    if err:
        return err
    if t["status"] not in ("JUDGED", "NEEDS_REVIEW"):
        return page("", flash=f"Task is {t['status']} — nothing to review."), 400
    try:
        sig = svc().make_confirmation_token(task_id)
    except EscrowError as e:
        return page("", flash=f"Error: {e}")
    try:
        crits = json.loads(t.get("acceptance_criteria") or "[]")
        crit_items = "".join(
            f"<li>{'❗ required' if c.get('required') else 'optional'}: {c.get('text', '')}</li>"
            for c in crits) or "<li>(none)</li>"
    except ValueError:
        crit_items = "<li>(unparseable)</li>"
    human_box = ""
    if t["status"] == "NEEDS_REVIEW":
        human_box = ('<label><input type="checkbox" name="human_approve" value="yes" '
                     'style="width:auto"> I reviewed this borderline result and '
                     'approve settlement anyway</label><br><br>')
    warn = ("<p style='color:#b71c1c'><b>⚠ Borderline score — no auto-settle. "
            "Your explicit decision is required.</b></p>"
            if t["status"] == "NEEDS_REVIEW" else "")
    return page(f"""
<h2>👁 Review task #{t['id']} {badge(t['status'])}</h2>
{warn}
<div class="card"><b>Brief:</b> {t['description']}<br>
<b>Amount:</b> {t['amount']:.2f} {t['currency']}<br>
<b>Payout recipient (locked):</b> <code>{t['worker_email']}</code><br>
<b>Authorization:</b> <code>{t['authorization_id']}</code></div>
<div class="card"><b>Acceptance criteria:</b><ul>{crit_items}</ul></div>
<div class="card"><b>Worker output:</b>
<pre style='background:#fff;color:#222'>{(t['worker_output'] or '')[:4000]}</pre></div>
<div class="card"><b>AI judge:</b> score {t['judge_score']} →
{t['judge_verdict'].upper() if t['judge_verdict'] else '-'}<br>
<i>{t['judge_reasoning']}</i></div>
<div class="card" style="border-color:#2e7d32">
<b>Settlement confirmation</b> — this button captures
{t['amount']:.2f} {t['currency']} and pays <code>{t['worker_email']}</code>.
The confirmation is cryptographically bound to these exact values.<br><br>
<form method="post" action="/task/{task_id}/settle">{csrf_field()}
<input type="hidden" name="sig" value="{sig}">
{human_box}
<button class="btn go" type="submit">✓ Confirm — capture &amp; payout</button></form></div>
""")


@app.route("/task/<int:task_id>/settle", methods=["POST"])
@require_auth
@rate_limited
def settle(task_id):
    t, err = _owned(task_id)
    if err:
        return err
    try:
        r = svc().settle_approved(
            task_id,
            request.form.get("sig", ""),
            human_approve=request.form.get("human_approve") == "yes",
        )
        msg = f"Settled: {r['status']}."
        if r.get("note"):
            msg += f" {r['note']}"
    except EscrowError as e:
        msg = f"Error: {e}"
    return redirect(url_for("detail", task_id=task_id, msg=msg))


@app.route("/task/<int:task_id>/confirm", methods=["POST"])
@require_auth
@rate_limited
def confirm(task_id):
    t, err = _owned(task_id)
    if err:
        return err
    try:
        svc().confirm_authorization(task_id)
        msg = "Funds locked — authorization created."
    except EscrowError as e:
        msg = f"Error: {e}"
    return redirect(url_for("detail", task_id=task_id, msg=msg))


@app.route("/task/<int:task_id>/submit", methods=["POST"])
@require_auth
@rate_limited
def submit(task_id):
    t, err = _owned(task_id)
    if err:
        return err
    try:
        svc().submit_work(task_id, request.form["output"],
                           request.form.get("worker_email", ""))
        msg = "Work submitted — run the AI judge."
    except EscrowError as e:
        msg = f"Error: {e}"
    return redirect(url_for("detail", task_id=task_id, msg=msg))


@app.route("/task/<int:task_id>/judge", methods=["POST"])
@require_auth
@rate_limited
def judge(task_id):
    t, err = _owned(task_id)
    if err:
        return err
    try:
        r = svc().judge_only(task_id)
        msg = (f"Judge: score {r['score']} → {r['verdict']} ({r['status']}). "
               f"{r.get('reasoning', '')}")
    except EscrowError as e:
        # A7: เลิกกลืน error — แสดงเหตุผลให้ operator เห็น
        msg = f"Error: {e}"
    return redirect(url_for("detail", task_id=task_id, msg=msg))


@app.route("/dashboard")
def dashboard():
    tasks = svc().list_tasks()["tasks"]
    data = json.dumps([{
        "id": t["id"], "description": (t["description"] or "")[:50],
        "amount": t["amount"], "currency": t["currency"], "status": t["status"],
        "order_id": t["order_id"] or "", "authorization_id": t["authorization_id"] or "",
        "capture_id": t["capture_id"] or "", "payout_batch_id": t["payout_batch_id"] or "",
        "judge_score": t["judge_score"], "judge_verdict": t["judge_verdict"] or "",
        "worker_email": t["worker_email"] or "",
        "created_at": (t["created_at"] or "")[:16].replace("T", " "),
    } for t in tasks])
    return render_template_string("""
<!doctype html><html><head><meta charset="utf-8"><title>JudgePay Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/ag-grid-community/dist/ag-grid-community.min.js"></script>
<style>body{font-family:system-ui;margin:1em}h1 a{color:#003087;text-decoration:none}
.sandbox{background:#e3f2fd;border:2px dashed #0070ba;padding:8px;border-radius:6px;font-weight:bold}</style>
</head><body>
<h1><a href="/">🏛️ JudgePay</a> — escrow dashboard</h1>
<div class="sandbox">🧪 SANDBOX MODE — simulated money, NOT real funds</div>
<div id="grid" class="ag-theme-alpine" style="height:70vh"></div>
<script>
const rowData = %%DATA%%;
const gridOptions = {
  rowData,
  columnDefs: [
    {field:"id", width:60}, {field:"description", flex:2},
    {field:"amount", width:100}, {field:"currency", width:90},
    {field:"status", width:170}, {field:"judge_score", width:110},
    {field:"judge_verdict", width:130}, {field:"order_id", flex:1},
    {field:"authorization_id", flex:1}, {field:"capture_id", flex:1},
    {field:"payout_batch_id", flex:1}, {field:"worker_email", flex:1},
    {field:"created_at", width:150},
  ],
  defaultColDef: {sortable:true, filter:true, resizable:true},
};
agGrid.createGrid(document.getElementById("grid"), gridOptions);
</script></body></html>""".replace("%%DATA%%", data))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
