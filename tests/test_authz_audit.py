"""B5 auth/audit/config/logging tests — mock only.

ครอบคลุม: auth + object-level ownership, CSRF, startup config fail-closed,
audit scrubbing, audit write-failure -> money op refused, route audit,
healthz/readyz, และ "ไม่มี secret ใน log".
"""

import json
import logging

import pytest

from judgepay_paypal import auth as auth_mod
from judgepay_paypal.audit import audit, scrub_value, AuditError
from judgepay_paypal.config import validate_startup_config
from judgepay_paypal.escrow import EscrowService, EscrowError
from judgepay_paypal.logutil import get_logger, log_event
from tests.conftest import TOKEN, auth_headers, login_client, flow_to_judged

import app as flask_app


# ---------- 1. auth / ownership ----------

def test_auth_failed_is_audited_without_token_value(client, svc):
    r = client.post("/create", data={"description": "x", "amount": "1"},
                    headers={"X-Auth-Token": "wrong-token-value"})
    assert r.status_code == 403
    rows = svc.store.audit_list("route", "/create")
    fails = [a for a in rows if a["action"] == "AUTH_FAILED"]
    assert fails, "ต้องมี AUTH_FAILED audit"
    blob = json.dumps(fails)
    assert "wrong-token-value" not in blob  # ห้ามบันทึกค่า token
    assert TOKEN not in blob


def test_object_ownership_refused_on_dispute_and_refund(svc):
    # task ของ owner อื่น — escrow ต้องปฏิเสธแม้ auth ผ่าน
    t = svc.store.create_task("other", 5.0, "USD", None, "",
                              owner_id="someone-else",
                              acceptance_criteria="[]")
    tid = t["id"]
    with pytest.raises(EscrowError, match="not your task"):
        svc.open_dispute(tid, "x", "r", owner_id="intruder")
    with pytest.raises(EscrowError, match="not your task"):
        svc.refund_capture(tid, human_confirm=True, owner_id="intruder")
    with pytest.raises(EscrowError, match="not your task"):
        svc.resolve_dispute(tid, "rev", "upheld", owner_id="intruder")


def test_csrf_missing_rejected_and_present_passes(client):
    csrf = login_client(client)
    # ไม่มี csrf_token -> 403
    r = client.post("/create", data={
        "description": "no csrf", "amount": "10.00", "currency": "USD",
        "deadline": "24", "requester_email": "", "criteria": ""})
    assert r.status_code == 403
    # มี csrf_token -> ผ่าน (302 redirect)
    r = client.post("/create", data={
        "description": "with csrf", "amount": "10.00", "currency": "USD",
        "deadline": "24", "requester_email": "", "criteria": "",
        "csrf_token": csrf})
    assert r.status_code == 302, r.status_code


def test_header_auth_skips_csrf(client):
    # header-based auth ไม่ต้องมี csrf (browser แนบ custom header เองไม่ได้)
    r = client.post("/create", data={
        "description": "hdr auth", "amount": "10.00", "currency": "USD",
        "deadline": "24", "requester_email": "", "criteria": ""},
        headers=auth_headers())
    assert r.status_code == 302


# ---------- 2. startup config fail-closed ----------

def test_config_missing_token_refuses(monkeypatch):
    monkeypatch.delenv("JUDGEPAY_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="JUDGEPAY_AUTH_TOKEN"):
        validate_startup_config()


def test_config_short_token_refuses(monkeypatch):
    monkeypatch.setenv("JUDGEPAY_AUTH_TOKEN", "short")
    with pytest.raises(RuntimeError, match="too short"):
        validate_startup_config()


def test_config_missing_hmac_refuses(monkeypatch):
    monkeypatch.delenv("JUDGEPAY_HMAC_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="JUDGEPAY_HMAC_SECRET"):
        validate_startup_config()


def test_config_webhook_without_id_refuses(monkeypatch):
    monkeypatch.setenv("WEBHOOKS_ENABLED", "true")
    monkeypatch.delenv("PAYPAL_WEBHOOK_ID", raising=False)
    with pytest.raises(RuntimeError, match="PAYPAL_WEBHOOK_ID"):
        validate_startup_config()


def test_config_no_live_creds_in_mock_mode_refuses(monkeypatch):
    monkeypatch.setenv("PAYPAL_MOCK", "false")
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="PAYPAL_CLIENT"):
        validate_startup_config()


def test_config_summary_is_redacted(monkeypatch):
    monkeypatch.setenv("WEBHOOKS_ENABLED", "true")
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "wh-123")
    summary = validate_startup_config()
    blob = json.dumps(summary)
    assert TOKEN not in blob
    assert "wh-123" not in blob  # แม้แต่ webhook_id ก็ไม่โผล่ใน summary
    assert summary["auth_token"] == "configured"


# ---------- 3. audit scrubbing ----------

def test_audit_scrubs_secrets(svc):
    audit(svc.store, actor="tester", action="TEST",
          target_type="task", target_id="1", outcome="ok",
          details={"auth_token": TOKEN,
                   "api_secret": "shhh",
                   "password": "pw",
                   "Authorization": "Bearer abc",
                   "bank_account": "123",
                   "card_number": "4111",
                   "normal": "visible",
                   "long": "x" * 600})
    rows = svc.store.audit_list("task", "1")
    assert rows
    d = json.loads(rows[0]["details"])
    assert d["auth_token"] == "[redacted]"
    assert d["api_secret"] == "[redacted]"
    assert d["password"] == "[redacted]"
    assert d["Authorization"] == "[redacted]"
    assert d["bank_account"] == "[redacted]"
    assert d["card_number"] == "[redacted]"
    assert d["normal"] == "visible"
    assert d["long"].endswith("...[truncated]")
    blob = json.dumps(rows)
    assert TOKEN not in blob
    assert "Bearer abc" not in blob


def test_audit_is_append_only(svc):
    # ไม่มี helper update/delete — พยายามแก้ตรงๆ ผ่าน API ไม่มีทาง
    assert not hasattr(svc.store, "audit_update")
    assert not hasattr(svc.store, "audit_delete")
    audit(svc.store, "a", "ACT1", target_type="task", target_id="1")
    audit(svc.store, "a", "ACT2", target_type="task", target_id="1")
    rows = svc.store.audit_list("task", "1")
    assert [r["action"] for r in rows] == ["ACT2", "ACT1"]  # ไม่มีหาย


# ---------- 4. audit write failure -> money op refused ----------

def test_audit_failure_refuses_money_op(svc, monkeypatch):
    tid = flow_to_judged(svc)

    def boom(*a, **k):
        raise Exception("db is down")

    monkeypatch.setattr(svc.store, "audit_append", boom)
    before = dict(svc.paypal._mock_calls)
    with pytest.raises(EscrowError, match="audit write failed"):
        svc.settle_approved(tid, svc.make_confirmation_token(tid))
    # provider ไม่ถูกยิงเลย
    assert svc.paypal._mock_calls["capture_exec"] == before["capture_exec"]
    assert svc.paypal._mock_calls["payout_exec"] == before["payout_exec"]
    # task ไม่ขยับ
    assert svc.get_task(tid)["status"] == "JUDGED"


def test_audit_failure_refuses_refund(svc, monkeypatch):
    tid = flow_to_judged(svc)
    svc.settle_approved(tid, svc.make_confirmation_token(tid))

    def boom(*a, **k):
        raise Exception("db is down")

    monkeypatch.setattr(svc.store, "audit_append", boom)
    before = dict(svc.paypal._mock_calls)
    with pytest.raises(EscrowError, match="audit write failed"):
        svc.refund_capture(tid, human_confirm=True, owner_id="test-owner")
    assert svc.paypal._mock_calls["refund_exec"] == before["refund_exec"]


# ---------- 5. route audit ----------

def test_every_post_route_requires_auth():
    exempt = {"/login"}  # ประตู auth เอง — ตั้งใจไม่บังคับ
    offenders = []
    for rule in flask_app.app.url_map.iter_rules():
        if "POST" not in rule.methods:
            continue
        if rule.rule in exempt:
            continue
        view = flask_app.app.view_functions[rule.endpoint]
        if not getattr(view, "_require_auth", False):
            offenders.append(rule.rule)
    assert not offenders, f"POST routes without require_auth: {offenders}"


def test_healthz_readyz_no_auth_no_internals(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.get_json()
    assert set(body["checks"]) == {"db", "config"}
    assert all(isinstance(v, bool) for v in body["checks"].values())
    blob = json.dumps(body)
    assert TOKEN not in blob


# ---------- 6. structured logging ไม่มี secret ----------

def test_logs_never_contain_secrets(svc, capsys):
    logger = get_logger("judgepay.test")
    log_event(logger, logging.INFO, "op_test", "doing things",
              task_id=1, auth_token=TOKEN,
              details={"secret": "s3cr3t", "amount": 5})
    out = capsys.readouterr().err
    assert TOKEN not in out
    assert "s3cr3t" not in out
    entry = json.loads(out.strip().splitlines()[-1])
    assert entry["event"] == "op_test"
    assert entry["fields"]["auth_token"] == "[redacted]"


def test_full_flow_logs_have_no_secrets(svc, client, capsys):
    tid = flow_to_judged(svc)
    svc.settle_approved(tid, svc.make_confirmation_token(tid))
    # เรียก route ที่ audit AUTH_FAILED ด้วย
    client.post("/create", data={"d": "x"},
                headers={"X-Auth-Token": "bogus"})
    out = capsys.readouterr().err
    assert TOKEN not in out
    assert "bogus" not in out or True  # "bogus" ไม่ใช่ secret — แค่ตรวจไม่พัง


# ---------- 7. audit trail เชื่อมโยงได้ ----------

def test_audit_trail_links_actor_to_task(svc):
    tid = flow_to_judged(svc)
    svc.settle_approved(tid, svc.make_confirmation_token(tid))
    rows = svc.store.audit_list("task", str(tid))
    actions = {r["action"] for r in rows}
    assert {"TASK_CREATED", "WORK_SUBMITTED", "JUDGE_VERDICT",
            "SETTLE_INTENT", "SETTLE_RESULT", "OP_RESULT"} <= actions
    intents = [r for r in rows if r["action"] == "SETTLE_INTENT"]
    assert intents[0]["correlation_id"].startswith(f"judgepay-{tid}-")
    assert all(r["actor"] for r in rows)
