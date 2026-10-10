"""Server-side auth gate — ประตูชั่วคราวสำหรับเดโม่ Sandbox เท่านั้น.

โมเดล: shared operator token เก็บใน env JUDGEPAY_AUTH_TOKEN
- เทียบด้วย hmac.compare_digest (กัน timing attack)
- รับจาก header X-Auth-Token, form field auth_token, หรือ session (หลัง login)
- ห้าม log token / ห้าม render token ลง HTML/JS เด็ดขาด
- request แบบ session ต้องมี CSRF token ถูกต้องด้วย
"""

import hashlib
import hmac
import os
import secrets
import time
from collections import deque
from functools import wraps

from flask import g, jsonify, request, session

# จำกัด 30 mutating requests / นาที ต่อ token (กันยิงรัว)
RATE_LIMIT = 30
RATE_WINDOW = 60
_RATE_BUCKETS = {}


def _expected_token():
    return os.getenv("JUDGEPAY_AUTH_TOKEN", "")


def owner_id_for(token):
    """owner_id = sha256(token) — ผูก task กับเจ้าของ token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""


def current_owner_id():
    return g.get("owner_id", "")


def _csrf_ok():
    # session-based auth ต้องมี csrf_token ตรงกับใน session;
    # header-based auth ไม่ต้อง (browser ไม่แนบ custom header ให้เอง)
    if not session.get("authed"):
        return True
    provided = request.form.get("csrf_token", "")
    expected = session.get("csrf", "")
    return bool(provided and expected) and hmac.compare_digest(provided, expected)


def require_auth(fn):
    """Decorator: ไม่มี token ที่ถูกต้อง -> 403 JSON."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = _expected_token()
        if not expected:
            return jsonify({"error": "server misconfigured: JUDGEPAY_AUTH_TOKEN not set"}), 500
        provided = (
            request.headers.get("X-Auth-Token")
            or request.form.get("auth_token")
            or ""
        )
        if provided:
            if not hmac.compare_digest(provided, expected):
                return jsonify({"error": "forbidden"}), 403
            g.owner_id = owner_id_for(expected)
        elif session.get("authed") and session.get("owner_id") == owner_id_for(expected):
            g.owner_id = session["owner_id"]
        else:
            return jsonify({"error": "forbidden"}), 403
        if request.method == "POST" and not _csrf_ok():
            return jsonify({"error": "forbidden: csrf check failed"}), 403
        return fn(*args, **kwargs)
    return wrapper


def rate_limited(fn):
    """กันยิงรัว: 30 ครั้ง/นาที ต่อ owner."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        key = current_owner_id() or "anon"
        now = time.time()
        dq = _RATE_BUCKETS.setdefault(key, deque())
        while dq and now - dq[0] > RATE_WINDOW:
            dq.popleft()
        if len(dq) >= RATE_LIMIT:
            return jsonify({"error": "rate limit exceeded"}), 429
        dq.append(now)
        return fn(*args, **kwargs)
    return wrapper


def new_csrf_token():
    tok = secrets.token_hex(16)
    session["csrf"] = tok
    return tok


def login_with_token(token):
    """ตรวจ token ตอน login; ถูก -> เปิด session. คืน True/False."""
    expected = _expected_token()
    if not expected or not token:
        return False
    if not hmac.compare_digest(token, expected):
        return False
    session["authed"] = True
    session["owner_id"] = owner_id_for(expected)
    new_csrf_token()
    return True


def logout():
    session.clear()
