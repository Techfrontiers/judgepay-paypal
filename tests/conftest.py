"""Fixtures ร่วม: แยก DB + mock state ต่อเทส, env ครบก่อน import app."""

import os

os.environ.setdefault("PAYPAL_MOCK", "true")
os.environ.setdefault("PAYPAL_MODE", "sandbox")
# B5: startup config validation ต้องการ secret ยาว >= 32 ตัวอักษร
# (ค่า test เท่านั้น — ไม่ใช่ secret จริง)
os.environ.setdefault("JUDGEPAY_AUTH_TOKEN", "test-operator-token-0123456789abcdef")
os.environ.setdefault("JUDGEPAY_HMAC_SECRET", "test-hmac-secret-0123456789abcdef")
os.environ.setdefault("FLASK_SECRET_KEY", "test-flask-secret-0123456789abcdef")

import pytest  # noqa: E402

from judgepay_paypal.escrow import EscrowService  # noqa: E402
from judgepay_paypal.paypal_client import PayPalClient  # noqa: E402
from judgepay_paypal.store import TaskStore  # noqa: E402
from judgepay_paypal.judge import AIJudge  # noqa: E402

import app as flask_app  # noqa: E402

TOKEN = "test-operator-token-0123456789abcdef"

# brief + งานดี (mock score ~91, ผ่าน policy ทุกข้อ)
DESC = ("Write a product description for a ceramic coffee mug. "
        "Must mention dishwasher-safe, 350ml capacity, blue glaze finish, "
        "and gift packaging.")
GOOD_OUTPUT = (
    "Here is the product description for your ceramic coffee mug. This beautiful "
    "mug features a stunning blue glaze finish that catches the light. With a "
    "generous 350ml capacity, it holds your favorite brew. The mug is "
    "dishwasher-safe for easy cleaning. It arrives in elegant gift packaging, "
    "making it a perfect present. Enjoy every sip from this durable ceramic "
    "coffee companion, designed for daily delight and lasting quality."
)
CRITERIA = [
    {"text": "mention dishwasher-safe", "required": True},
    {"text": "mention 350ml capacity", "required": True},
    {"text": "at least 100 words", "required": False},
]


@pytest.fixture
def svc(tmp_path, monkeypatch):
    """EscrowService แยก DB + mock file ต่อเทส."""
    monkeypatch.setenv("PAYPAL_MOCK", "true")
    monkeypatch.setenv("PAYPAL_MOCK_FILE", str(tmp_path / "mock.json"))
    monkeypatch.setenv("JUDGEPAY_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("JUDGEPAY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("JUDGEPAY_HMAC_SECRET", "test-hmac-secret-0123456789abcdef")
    monkeypatch.delenv("PAYPAL_MOCK_PAYOUT_STATUS", raising=False)
    monkeypatch.delenv("PAYPAL_MOCK_ITEM_STATUS", raising=False)
    monkeypatch.delenv("PAYPAL_MOCK_ITEMS_JSON", raising=False)
    monkeypatch.delenv("PAYPAL_MOCK_REFUND_STATUS", raising=False)
    monkeypatch.delenv("PAYPAL_MOCK_FAIL_MODE", raising=False)
    return EscrowService(paypal=PayPalClient(), store=TaskStore(),
                         judge=AIJudge(backend="mock"))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client — แยก DB + mock file ต่อเทส."""
    monkeypatch.setenv("PAYPAL_MOCK", "true")
    monkeypatch.setenv("PAYPAL_MOCK_FILE", str(tmp_path / "mock.json"))
    monkeypatch.setenv("JUDGEPAY_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("JUDGEPAY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("JUDGEPAY_HMAC_SECRET", "test-hmac-secret-0123456789abcdef")
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def flow_to_judged(svc, email="worker@example.com", criteria=None):
    """รันโฟลว์เต็มถึง JUDGED (score สูง)."""
    r = svc.create_task(DESC, 25.00, "USD", 24, "buyer@example.com",
                        owner_id="test-owner",
                        acceptance_criteria=criteria if criteria is not None else CRITERIA)
    tid = r["task_id"]
    svc.confirm_authorization(tid)
    svc.submit_work(tid, GOOD_OUTPUT, email)
    jr = svc.judge_only(tid)
    assert jr["status"] == "JUDGED", jr
    return tid


def auth_headers():
    return {"X-Auth-Token": TOKEN}


def login_client(client):
    """login ผ่าน form -> session + csrf token."""
    r = client.post("/login", data={"token": TOKEN})
    assert r.status_code == 302
    with client.session_transaction() as s:
        csrf = s["csrf"]
    return csrf
