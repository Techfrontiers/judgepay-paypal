"""Regression: settlement — atomic claim, invariants, HMAC, fail-safe."""

import threading
import time

import pytest

from judgepay_paypal.escrow import EscrowError, _hmac_key
from tests.conftest import DESC, GOOD_OUTPUT, CRITERIA, flow_to_judged


def test_happy_path_completes(svc):
    tid = flow_to_judged(svc)
    token = svc.make_confirmation_token(tid)
    r = svc.settle_approved(tid, token)
    assert r["status"] == "COMPLETED"
    assert r["capture_id"]
    assert r["payout"]["status"] == "SUCCESS"
    t = svc.get_task(tid)
    assert t["status"] == "COMPLETED"
    assert t["payout_batch_id"]


def test_settle_without_token_rejected(svc):
    tid = flow_to_judged(svc)
    with pytest.raises(EscrowError, match="confirmation token"):
        svc.settle_approved(tid, "")
    with pytest.raises(EscrowError, match="confirmation token"):
        svc.settle_approved(tid, "bogus:token")


def test_settle_twice_second_fails(svc):
    tid = flow_to_judged(svc)
    svc.settle_approved(tid, svc.make_confirmation_token(tid))
    # ออก token ใหม่ไม่ได้แล้ว (สถานะไม่เอื้อ) หรือ settle ถูกปฏิเสธ
    with pytest.raises(EscrowError, match="cannot (settle|confirm)"):
        svc.settle_approved(tid, svc.make_confirmation_token(tid))


def test_hmac_tampered_amount_rejected(svc):
    tid = flow_to_judged(svc)
    token = svc.make_confirmation_token(tid)
    # แก้ยอดใน DB หลังออก token -> token ต้องใช้ไม่ได้
    svc.store.update_task(tid, amount=999.99)
    assert not svc.verify_confirmation_token(tid, token)
    with pytest.raises(EscrowError, match="confirmation token"):
        svc.settle_approved(tid, token)


def test_hmac_expired_rejected(svc):
    import hashlib
    import hmac as hmac_mod
    tid = flow_to_judged(svc)
    task = svc.get_task(tid)
    old_ts = str(int(time.time()) - 4000)  # เกิน TTL 1800
    msg = svc._confirmation_message(task) + "|" + old_ts
    sig = hmac_mod.new(_hmac_key(), msg.encode(), hashlib.sha256).hexdigest()
    assert not svc.verify_confirmation_token(tid, f"{old_ts}:{sig}")


def test_amount_mismatch_blocks_capture(svc):
    tid = flow_to_judged(svc)
    # แก้ยอด task ให้ไม่ตรง authorization (แต่ token ออกหลังแก้ -> HMAC ผ่าน)
    svc.store.update_task(tid, amount=50.00)
    token = svc.make_confirmation_token(tid)
    with pytest.raises(EscrowError, match="refusing to capture"):
        svc.settle_approved(tid, token)
    assert len(svc.paypal._captures) == 0  # ไม่มีการยิง capture เลย
    assert svc.get_task(tid)["status"] == "JUDGED"  # สถานะไม่ขยับ


def test_recipient_tampering_rejected(svc):
    r = svc.create_task(DESC, 25.00, "USD", 24, "", owner_id="o")
    tid = r["task_id"]
    svc.confirm_authorization(tid)
    svc.submit_work(tid, GOOD_OUTPUT, "worker@example.com")
    # ล็อกแล้ว — ส่งอีเมลอื่นต้องถูกปฏิเสธ (จำลองว่ายัง OPEN อยู่)
    svc.store.update_task(tid, status="OPEN")
    with pytest.raises(EscrowError, match="already locked"):
        svc.submit_work(tid, GOOD_OUTPUT, "evil@example.com")
    assert svc.get_task(tid)["worker_email"] == "worker@example.com"


def test_no_recipient_no_settle(svc):
    tid = flow_to_judged(svc, email="")  # submit โดยไม่ให้อีเมล
    token = svc.make_confirmation_token(tid)
    with pytest.raises(EscrowError, match="no locked payout recipient"):
        svc.settle_approved(tid, token)


def test_concurrent_settle_only_one_wins(svc):
    tid = flow_to_judged(svc)
    token = svc.make_confirmation_token(tid)
    barrier = threading.Barrier(2)
    results = []

    def run():
        barrier.wait()
        try:
            svc.settle_approved(tid, token)
            results.append("ok")
        except EscrowError:
            results.append("rejected")

    ts = [threading.Thread(target=run) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(results) == ["ok", "rejected"]
    assert len(svc.paypal._captures) == 1  # capture เกิดครั้งเดียว
    assert svc.get_task(tid)["status"] == "COMPLETED"


def test_capture_ok_db_fail_needs_reconciliation(svc):
    tid = flow_to_judged(svc)
    token = svc.make_confirmation_token(tid)
    orig_update = svc.store.update_task
    state = {"crashed": False}

    def flaky(task_id, **fields):
        if "capture_id" in fields and not state["crashed"]:
            state["crashed"] = True
            raise RuntimeError("simulated DB crash after capture")
        return orig_update(task_id, **fields)

    svc.store.update_task = flaky
    try:
        with pytest.raises(EscrowError, match="NEEDS_RECONCILIATION"):
            svc.settle_approved(tid, token)
    finally:
        svc.store.update_task = orig_update
    t = svc.get_task(tid)
    assert t["status"] == "NEEDS_RECONCILIATION"
    assert t["last_paypal_op"].startswith("capture:")
    assert t["capture_id"]  # capture_id ถูกบันทึกไว้ก่อน mark
    assert len(svc.paypal._captures) == 1  # ไม่มีการยิง capture ซ้ำ
    # reconcile: captured แล้วแต่ยังไม่มี payout -> คง NEEDS_RECONCILIATION
    r = svc.reconcile(tid)
    assert r["status"] == "NEEDS_RECONCILIATION"
    assert len(svc.paypal._captures) == 1  # reconcile ไม่ยิง capture ซ้ำ


def test_payout_pending_not_completed(svc, monkeypatch):
    monkeypatch.setenv("PAYPAL_MOCK_PAYOUT_STATUS", "PENDING")
    tid = flow_to_judged(svc)
    r = svc.settle_approved(tid, svc.make_confirmation_token(tid))
    assert r["status"] == "NEEDS_RECONCILIATION"
    assert r["payout"]["status"] == "PENDING"
    t = svc.get_task(tid)
    assert t["status"] == "NEEDS_RECONCILIATION"
    assert t["payout_batch_id"]
    # reconcile: batch ยัง PENDING -> คงสถานะไว้
    r2 = svc.reconcile(tid)
    assert r2["status"] == "NEEDS_RECONCILIATION"


def test_needs_review_requires_human(svc, monkeypatch):
    tid = flow_to_judged(svc)
    # บังคับ score 70 -> NEEDS_REVIEW
    monkeypatch.setattr(svc.judge, "evaluate",
                        lambda d, o: {"score": 70, "verdict": "approve", "reasoning": "borderline"})
    svc.store.update_task(tid, status="SUBMITTED")  # judge ใหม่
    jr = svc.judge_only(tid)
    assert jr["status"] == "NEEDS_REVIEW"
    token = svc.make_confirmation_token(tid)
    with pytest.raises(EscrowError, match="human review"):
        svc.settle_approved(tid, token)
    r = svc.settle_approved(tid, token, human_approve=True)
    assert r["status"] == "COMPLETED"


def test_judge_reject_voids(svc):
    r = svc.create_task(DESC, 25.00, "USD", 24, "", owner_id="o")
    tid = r["task_id"]
    svc.confirm_authorization(tid)
    svc.submit_work(tid, "too short", "worker@example.com")
    jr = svc.judge_only(tid)
    assert jr["status"] == "REFUNDED"
    auth = svc.paypal.get_authorization(svc.get_task(tid)["authorization_id"])
    assert auth["status"] == "VOIDED"


def test_void_handles_http_204_success(monkeypatch):
    """PayPal void ตอบ 204 No Content = สำเร็จ — ห้าม raise (bug เจอจาก sandbox E2E จริง)."""
    from judgepay_paypal import paypal_client as pc_mod
    from judgepay_paypal.paypal_client import PayPalClient, PayPalError

    class Resp204:
        status_code = 204
        text = ""

    monkeypatch.setattr(pc_mod.requests, "post", lambda *a, **k: Resp204())
    monkeypatch.setattr(PayPalClient, "_headers", lambda self, extra=None: {})
    c = PayPalClient(client_id="x", client_secret="y", mode="sandbox", mock=False)
    assert not c.mock
    r = c.void_authorization("AUTH123")
    assert r == {"authorization_id": "AUTH123", "status": "VOIDED"}


def test_void_raises_on_real_error(monkeypatch):
    """204 ผ่าน แต่ error จริงต้องยัง raise (patch ต้องไม่กลืน error)."""
    from judgepay_paypal import paypal_client as pc_mod
    from judgepay_paypal.paypal_client import PayPalClient, PayPalError

    class Resp500:
        status_code = 500
        text = "internal error"

    monkeypatch.setattr(pc_mod.requests, "post", lambda *a, **k: Resp500())
    monkeypatch.setattr(PayPalClient, "_headers", lambda self, extra=None: {})
    c = PayPalClient(client_id="x", client_secret="y", mode="sandbox", mock=False)
    with pytest.raises(PayPalError, match="500"):
        c.void_authorization("AUTH123")
