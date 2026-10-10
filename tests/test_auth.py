"""Regression: authentication, ownership, CSRF, rate limit."""

import json

import pytest

from judgepay_paypal import auth as auth_mod
from tests.conftest import TOKEN, auth_headers, login_client


def _create_via_api(client):
    r = client.post("/create", data={
        "description": "auth test task", "amount": "10.00",
        "currency": "USD", "deadline": "24", "requester_email": "",
        "criteria": "",
    }, headers=auth_headers())
    assert r.status_code == 302, r.status_code
    tid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    return tid


def test_create_without_token_rejected(client):
    r = client.post("/create", data={"description": "x", "amount": "1"})
    assert r.status_code == 403
    assert r.get_json()["error"] == "forbidden"


def test_create_with_wrong_token_rejected(client):
    r = client.post("/create", data={"description": "x", "amount": "1"},
                    headers={"X-Auth-Token": "wrong"})
    assert r.status_code == 403


def test_mutating_routes_require_auth(client):
    # confirm / submit / judge / settle — ไม่มี token -> 403 ทั้งหมด
    for method, url, data in [
        ("post", "/task/1/confirm", {}),
        ("post", "/task/1/submit", {"output": "x"}),
        ("post", "/task/1/judge", {}),
        ("post", "/task/1/settle", {"sig": "x"}),
    ]:
        r = getattr(client, method)(url, data=data)
        assert r.status_code == 403, url


def test_cross_owner_task_rejected(client, svc):
    # สร้าง task ของ owner อื่นตรงๆ ผ่าน store
    t = svc.store.create_task("other owner task", 5.0, "USD", None, "",
                              owner_id="someone-else", acceptance_criteria="[]")
    tid = t["id"]
    r = client.post(f"/task/{tid}/confirm", headers=auth_headers())
    assert r.status_code == 403
    assert r.get_json()["error"] == "forbidden: not your task"


def test_review_requires_auth(client, svc):
    r = client.get("/task/1/review")
    assert r.status_code == 403


def test_csrf_enforced_for_session_auth(client):
    tid = _create_via_api(client)
    csrf = login_client(client)  # ตอนนี้ใช้ session auth
    # ไม่มี csrf -> 403
    r = client.post(f"/task/{tid}/confirm", data={})
    assert r.status_code == 403
    assert "csrf" in r.get_json()["error"]
    # มี csrf ถูกต้อง -> ผ่าน (302 redirect)
    r = client.post(f"/task/{tid}/confirm", data={"csrf_token": csrf})
    assert r.status_code == 302


def test_header_auth_skips_csrf(client):
    tid = _create_via_api(client)
    r = client.post(f"/task/{tid}/confirm", headers=auth_headers(), data={})
    assert r.status_code == 302


def test_rate_limit(client):
    auth_mod._RATE_BUCKETS.clear()
    statuses = set()
    for _ in range(31):
        r = client.post("/task/99999/confirm", headers=auth_headers(), data={})
        statuses.add(r.status_code)
    assert 429 in statuses  # ครั้งที่ 31 โดน 429
    auth_mod._RATE_BUCKETS.clear()


def test_login_wrong_token(client):
    r = client.post("/login", data={"token": "nope"})
    assert r.status_code == 200
    assert b"Invalid token" in r.data
