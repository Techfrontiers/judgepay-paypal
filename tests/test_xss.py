"""Regression: HTML/JS injection safety in the Flask UI (app.py).

app.py builds pages with f-strings (not Jinja {{ }}), so Jinja autoescape does
NOT apply — every untrusted value must pass through esc(). These tests prove
stored + reflected XSS payloads come out escaped, the dashboard JSON cannot
break out of <script>, and the review page tells the truth for reject verdicts.
"""

import json
import re

import pytest

from judgepay_paypal.auth import owner_id_for
from tests.conftest import TOKEN, auth_headers

XSS = "<script>alert('xss')</script>"
XSS_ATTR = '"><img src=x onerror=alert(1)>'
OWNER = owner_id_for(TOKEN)


def _create(client, description):
    r = client.post("/create", data={
        "description": description, "amount": "10.00",
        "currency": "USD", "deadline": "24", "requester_email": "",
        "criteria": "",
    }, headers=auth_headers())
    assert r.status_code == 302, r.status_code
    return int(r.headers["Location"].rstrip("/").split("/")[-1])


def test_description_xss_escaped_on_detail(client):
    tid = _create(client, f"brief {XSS}")
    r = client.get(f"/task/{tid}")
    body = r.get_data(as_text=True)
    assert XSS not in body
    assert "&lt;script&gt;" in body


def test_worker_output_xss_escaped_on_detail(client, svc):
    # setup via svc fixture (shares DB with the app); render via HTTP client
    r = svc.create_task(f"output {XSS_ATTR} brief", 10.0, "USD", 24, "",
                        owner_id=OWNER)
    tid = r["task_id"]
    svc.confirm_authorization(tid)
    svc.submit_work(tid, f"output {XSS_ATTR} done", "worker@example.com")
    r = client.get(f"/task/{tid}")
    body = r.get_data(as_text=True)
    assert XSS_ATTR not in body
    assert "&lt;img" in body


def test_criteria_xss_escaped_on_detail(client):
    r = client.post("/create", data={
        "description": "xss criteria test", "amount": "10.00",
        "currency": "USD", "deadline": "24", "requester_email": "",
        "criteria": f"!must not {XSS}",
    }, headers=auth_headers())
    tid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    r = client.get(f"/task/{tid}")
    body = r.get_data(as_text=True)
    assert XSS not in body
    assert "&lt;script&gt;" in body


def test_reflected_msg_xss_escaped(client):
    tid = _create(client, "reflected test")
    r = client.get(f"/task/{tid}?msg={XSS}")
    body = r.get_data(as_text=True)
    assert XSS not in body
    assert "&lt;script&gt;" in body


def test_dashboard_json_cannot_break_script(client):
    _create(client, f"dash </script><script>alert('xss')</script>")
    r = client.get("/dashboard")
    body = r.get_data(as_text=True)
    assert "</script><script>" not in body
    # the payload must still be present as inert JSON ('<' unicode-escaped;
    # '>' needs no escaping to stay inside <script>)
    assert "\\u003c/script>" in body


def test_review_page_reject_verdict_shows_void_not_capture(client, svc, monkeypatch):
    """Review page must tell the truth: reject verdict -> void, never capture/payout."""
    from tests.conftest import flow_to_judged, login_client
    tid = flow_to_judged(svc)
    # task owner ต้องตรงกับ session login
    svc.store.update_task(tid, owner_id=OWNER)
    monkeypatch.setattr(svc.judge, "evaluate",
                        lambda d, o, **kw: {"score": 65, "verdict": "reject",
                                            "reasoning": "mediocre"})
    svc.store.update_task(tid, status="SUBMITTED")  # judge ใหม่
    jr = svc.judge_only(tid)
    assert jr["status"] == "NEEDS_REVIEW"
    assert jr["verdict"] == "reject"
    # login via session to reach the review page (requires auth)
    login_client(client)
    r = client.get(f"/task/{tid}/review")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "void" in body.lower()
    assert "capture &amp; payout" not in body
    assert "Confirm — void" in body


def test_review_page_approve_verdict_shows_capture(client, svc):
    from tests.conftest import flow_to_judged, login_client
    tid = flow_to_judged(svc)
    svc.store.update_task(tid, owner_id=OWNER)
    login_client(client)
    r = client.get(f"/task/{tid}/review")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "capture" in body.lower()
