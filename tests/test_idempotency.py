"""B1 regression: idempotency & transaction recovery (mock mode + failure injection)."""

import os
import subprocess
import sys
import threading

import pytest

from judgepay_paypal.escrow import (
    EscrowError, EscrowService, _op_key, _request_hash,
)
from judgepay_paypal.paypal_client import PayPalClient
from judgepay_paypal.store import TaskStore
from judgepay_paypal.judge import AIJudge
from tests.conftest import flow_to_judged


def _settling_task(svc):
    """task พร้อมยิงเงิน: claim แล้ว (เหมือน settle_approved ก่อน _settle_approve)."""
    tid = flow_to_judged(svc)
    assert svc.store.claim_for_settlement(tid, ("JUDGED", "NEEDS_REVIEW"))
    return tid


def _op(svc, tid, op_type):
    return svc.store.get_operation(_op_key(tid, op_type))


# --- 1. ยิง capture ซ้ำด้วย key เดิม -> side effect ครั้งเดียว ---

def test_same_capture_twice_single_side_effect(svc):
    tid = _settling_task(svc)
    task = svc.get_task(tid)
    r1 = svc._execute_op(task, "capture")
    r2 = svc._execute_op(task, "capture")
    assert r1["capture_id"] == r2["capture_id"]
    assert r2.get("replayed") is True
    assert svc.paypal._mock_calls["capture_exec"] == 1
    assert len(svc.paypal._captures) == 1
    op = _op(svc, tid, "capture")
    assert op["status"] == "SUCCEEDED"
    assert op["paypal_ref"] == r1["capture_id"]
    assert op["idempotency_key"] == f"judgepay-{tid}-capture"  # stable, ไม่สุ่ม


# --- 2. สอง thread ชนกันที่ op เดียวกัน -> ยิงจริงครั้งเดียว ---

def test_concurrent_same_op_single_execution(svc):
    tid = _settling_task(svc)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def run():
        barrier.wait()
        try:
            results.append(svc._execute_op(svc.get_task(tid), "capture")["capture_id"])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=run) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    assert len(results) == 2 and results[0] == results[1]
    assert svc.paypal._mock_calls["capture_exec"] == 1  # ยิงจริงครั้งเดียว
    assert len(svc.paypal._captures) == 1


# --- 3. timeout หลัง PayPal สำเร็จ -> NEEDS_RECONCILIATION -> reconcile หาย ---

def test_timeout_after_capture_reconciles_without_duplicate(svc, monkeypatch):
    monkeypatch.setenv("PAYPAL_MOCK_FAIL_MODE", "timeout_after_capture")
    tid = _settling_task(svc)
    with pytest.raises(EscrowError, match="NEEDS_RECONCILIATION"):
        svc._execute_op(svc.get_task(tid), "capture")
    op = _op(svc, tid, "capture")
    assert op["status"] == "NEEDS_RECONCILIATION"
    assert op["status"] != "FAILED"  # timeout ห้าม mark FAILED เด็ดขาด
    assert svc.paypal._mock_calls["capture_exec"] == 1  # server ทำสำเร็จแล้ว
    # network หาย -> reconcile ต้อง adopt ของเดิม ไม่ยิงซ้ำ
    monkeypatch.delenv("PAYPAL_MOCK_FAIL_MODE")
    svc._reconcile_op(svc.get_task(tid), op)
    op2 = _op(svc, tid, "capture")
    assert op2["status"] == "SUCCEEDED"
    assert op2["paypal_ref"]
    assert svc.paypal._mock_calls["capture_exec"] == 1


# --- 4. crash หลัง INTENDED ก่อนยิง PayPal -> reconcile ส่งครั้งเดียว ---

def test_crash_after_intent_reconcile_sends_once(svc):
    tid = _settling_task(svc)
    task = svc.get_task(tid)
    key = _op_key(tid, "capture")
    # จำลอง crash: intent ถูกบันทึกแล้ว process ตายก่อนยิง PayPal
    # (hash ต้องตรงกับที่ _execute_op คำนวณจาก task จริง)
    svc.store.record_intent(
        tid, "capture", key,
        _request_hash("capture", task["amount"], task["currency"],
                      task.get("worker_email") or ""),
        amount=task["amount"], currency=task["currency"],
        recipient=task.get("worker_email") or "")
    assert _op(svc, tid, "capture")["status"] == "INTENDED"
    # "process ใหม่" — service ใหม่บน DB + mock file เดิม
    svc2 = EscrowService(paypal=PayPalClient(), store=TaskStore(),
                         judge=AIJudge(backend="mock"))
    r = svc2.reconcile(tid)
    assert r["success"]
    assert _op(svc2, tid, "capture")["status"] == "SUCCEEDED"
    assert svc2.paypal._mock_calls["capture_exec"] == 1


# --- 5. crash หลัง PayPal สำเร็จ ก่อนบันทึกผล -> reconcile adopt ---

def test_crash_after_paypal_success_before_db_update(svc):
    tid = _settling_task(svc)
    task = svc.get_task(tid)
    key = _op_key(tid, "capture")
    # PayPal ทำสำเร็จแล้ว แต่ app crash ก่อน mark SUCCEEDED (op ค้าง SENT)
    cap = svc.paypal.capture_authorization(task["authorization_id"], idempotency_key=key)
    svc.store.record_intent(
        tid, "capture", key,
        _request_hash("capture", task["amount"], task["currency"],
                      task.get("worker_email") or ""),
        amount=task["amount"], currency=task["currency"],
        recipient=task.get("worker_email") or "")
    svc.store.transition_operation(key, "SENT")
    before = svc.paypal._mock_calls["capture_exec"]
    svc._reconcile_op(svc.get_task(tid), svc.store.get_operation(key))
    assert svc.paypal._mock_calls["capture_exec"] == before  # ไม่ยิงใหม่
    op = _op(svc, tid, "capture")
    assert op["status"] == "SUCCEEDED"
    assert op["paypal_ref"] == cap["capture_id"]


# --- 6. retry ตอน NEEDS_RECONCILIATION ต้องถูกปฏิเสธ ---

def test_retry_while_needs_reconciliation_refused(svc, monkeypatch):
    monkeypatch.setenv("PAYPAL_MOCK_FAIL_MODE", "timeout_after_capture")
    tid = _settling_task(svc)
    with pytest.raises(EscrowError, match="NEEDS_RECONCILIATION"):
        svc._execute_op(svc.get_task(tid), "capture")
    # ยิงซ้ำตรงๆ (ไม่ใช่ reconcile) -> ปฏิเสธ
    with pytest.raises(EscrowError, match="ต้อง reconcile ก่อน retry"):
        svc._execute_op(svc.get_task(tid), "capture")
    assert svc.paypal._mock_calls["capture_exec"] == 1


# --- 7. key เดิมแต่ธุรกรรมเปลี่ยน -> ปฏิเสธเสียงดัง ---

def test_same_key_different_transaction_refused(svc):
    tid = _settling_task(svc)
    task = svc.get_task(tid)
    key = _op_key(tid, "capture")
    # แอบบันทึก intent ด้วยธุรกรรมอื่น (ยอดไม่ตรง)
    svc.store.record_intent(tid, "capture", key, "deadbeef" * 8,
                            amount=999.99, currency="USD", recipient="")
    with pytest.raises(EscrowError, match="ธุรกรรมอื่น"):
        svc._execute_op(task, "capture")
    assert svc.paypal._mock_calls["capture_exec"] == 0  # ไม่ยิงเลย


# --- 8. capture สำเร็จ + payout ล้มเหลว -> state อิสระ ไม่ COMPLETED ---

def test_capture_succeeded_payout_failed_independent(svc, monkeypatch):
    tid = _settling_task(svc)
    task = svc.get_task(tid)
    svc._execute_op(task, "capture")
    assert _op(svc, tid, "capture")["status"] == "SUCCEEDED"
    monkeypatch.setenv("PAYPAL_MOCK_FAIL_MODE", "error_422_payout")
    with pytest.raises(EscrowError, match="FAILED"):
        svc._execute_op(task, "payout")
    assert _op(svc, tid, "payout")["status"] == "FAILED"
    # capture ยัง SUCCEEDED — อิสระจาก payout
    assert _op(svc, tid, "capture")["status"] == "SUCCEEDED"
    assert svc.get_task(tid)["status"] != "COMPLETED"


# --- 9. reconcile เจอ PayPal สำเร็จแล้ว -> adopt ไม่ยิงใหม่ (นับ call) ---

def test_reconcile_adopts_paypal_truth_without_new_call(svc, monkeypatch):
    monkeypatch.setenv("PAYPAL_MOCK_FAIL_MODE", "timeout_after_capture")
    tid = _settling_task(svc)
    with pytest.raises(EscrowError, match="NEEDS_RECONCILIATION"):
        svc._execute_op(svc.get_task(tid), "capture")
    monkeypatch.delenv("PAYPAL_MOCK_FAIL_MODE")
    calls_before = svc.paypal._mock_calls["capture_exec"]
    r = svc.reconcile(tid)  # reconcile เต็ม (รวม op scan)
    assert r["success"]
    assert any("capture" in a for a in r["actions"])
    assert svc.paypal._mock_calls["capture_exec"] == calls_before
    assert _op(svc, tid, "capture")["status"] == "SUCCEEDED"


# --- 10. 422 ชัดเจน -> FAILED + retry ได้; timeout/5xx -> NEEDS_RECONCILIATION ---

def test_definite_422_failed_then_retryable(svc, monkeypatch):
    monkeypatch.setenv("PAYPAL_MOCK_FAIL_MODE", "error_422_capture")
    tid = _settling_task(svc)
    with pytest.raises(EscrowError, match="FAILED"):
        svc._execute_op(svc.get_task(tid), "capture")
    assert _op(svc, tid, "capture")["status"] == "FAILED"
    # definite failure -> retry ด้วย key เดิมได้
    monkeypatch.delenv("PAYPAL_MOCK_FAIL_MODE")
    r = svc._execute_op(svc.get_task(tid), "capture")
    assert r["capture_id"]
    assert _op(svc, tid, "capture")["status"] == "SUCCEEDED"


def test_500_is_ambiguous_not_failed(svc, monkeypatch):
    monkeypatch.setenv("PAYPAL_MOCK_FAIL_MODE", "error_500_capture")
    tid = _settling_task(svc)
    with pytest.raises(EscrowError, match="NEEDS_RECONCILIATION"):
        svc._execute_op(svc.get_task(tid), "capture")
    op = _op(svc, tid, "capture")
    assert op["status"] == "NEEDS_RECONCILIATION"
    assert op["status"] != "FAILED"


# --- 11. duplicate sender_batch_id -> ตาม link ไม่สร้าง batch ใหม่ ---

def test_duplicate_sender_batch_id_follows_link(svc, monkeypatch):
    from judgepay_paypal.paypal_client import _parse_duplicate_payout_link
    tid = _settling_task(svc)
    key = f"judgepay-{tid}-payout"
    p1 = svc.paypal.payout_to_worker("worker@example.com", 25.00, "USD",
                                     sender_batch_id=key,
                                     sender_item_id=f"{key}-item1")
    n_batches = len(svc.paypal._payouts)
    # ส่งซ้ำด้วย sender_batch_id เดิม -> PayPal reject + link -> ตามไปเอาของเดิม
    monkeypatch.setenv("PAYPAL_MOCK_FAIL_MODE", "duplicate_batch")
    p2 = svc.paypal.payout_to_worker("worker@example.com", 25.00, "USD",
                                     sender_batch_id=key,
                                     sender_item_id=f"{key}-item1")
    assert p2["batch_id"] == p1["batch_id"]
    assert len(svc.paypal._payouts) == n_batches  # ไม่มี batch ใหม่
    assert svc.paypal._mock_calls["payout_exec"] == 1
    # parser เจอ link จริง
    err = ('POST /v1/payments/payouts -> 400: DUPLICATE_SENDER_BATCH_ID ... '
           '"href": "https://api-m.sandbox.paypal.com/v1/payments/payouts/AbC123xYz"')
    assert _parse_duplicate_payout_link(err) == "AbC123xYz"
    assert _parse_duplicate_payout_link("POST /x -> 500: boom") is None


# --- 12. intent รอดข้าม process restart (SQLite file จริง) ---

def test_intents_survive_process_restart(tmp_path):
    db = str(tmp_path / "persist.db")
    s1 = TaskStore(db_path=db)
    s1.record_intent(7, "capture", "judgepay-7-capture", "abc123",
                     amount=10.0, currency="USD", recipient="")
    del s1
    s2 = TaskStore(db_path=db)  # "process ใหม่" เปิดไฟล์เดิม
    op = s2.get_operation("judgepay-7-capture")
    assert op is not None
    assert op["status"] == "INTENDED"
    assert op["task_id"] == 7
    assert op["amount"] == 10.0


# --- process restart จริง: process 1 ตายหลัง INTENDED, process 2 reconcile --all ---

def test_reconcile_all_across_real_process_restart(tmp_path):
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db = str(tmp_path / "restart.db")
    mock_file = str(tmp_path / "restart_mock.json")
    env = {**os.environ,
           "PAYPAL_MOCK": "true", "PAYPAL_MODE": "sandbox",
           "JUDGEPAY_DB": db, "PAYPAL_MOCK_FILE": mock_file,
           "JUDGEPAY_AUTH_TOKEN": "test-operator-token",
           "JUDGEPAY_HMAC_SECRET": "test-hmac-secret"}
    setup_code = (
        "from judgepay_paypal.escrow import EscrowService, _op_key, _request_hash;"
        "from judgepay_paypal.paypal_client import PayPalClient;"
        "from judgepay_paypal.store import TaskStore;"
        "from judgepay_paypal.judge import AIJudge;"
        "svc = EscrowService(PayPalClient(), TaskStore(), AIJudge(backend='mock'));"
        "r = svc.create_task('restart brief', 5.00, 'USD', 24, '', owner_id='o');"
        "tid = r['task_id']; svc.confirm_authorization(tid);"
        "t = svc.store.get_task(tid); key = _op_key(tid, 'capture');"
        "svc.store.record_intent(tid, 'capture', key,"
        " _request_hash('capture', t['amount'], t['currency'], ''),"
        " amount=t['amount'], currency=t['currency'], recipient='');"
        "print('process1: INTENDED recorded, exiting (simulated crash)')"
    )
    p1 = subprocess.run([sys.executable, "-c", setup_code],
                        cwd=proj, env=env, capture_output=True, text=True, timeout=60)
    assert p1.returncode == 0, p1.stderr[-500:]
    assert "INTENDED recorded" in p1.stdout
    # process 2: reconcile --all ผ่าน CLI จริง
    p2 = subprocess.run([sys.executable, "-m", "judgepay_paypal.cli",
                         "reconcile", "--all"],
                        cwd=proj, env=env, capture_output=True, text=True, timeout=60)
    assert p2.returncode == 0, p2.stderr[-500:]
    # ตรวจจาก DB: op ต้อง SUCCEEDED และยิง capture แค่ครั้งเดียว
    s = TaskStore(db_path=db)
    ops = s.list_operations(1)
    assert len(ops) == 1 and ops[0]["status"] == "SUCCEEDED"
    assert ops[0]["paypal_ref"]
    import json as _json
    mock_state = _json.load(open(mock_file))
    assert len(mock_state["captures"]) == 1


# --- key/message คงที่: settlement เต็ม flow ยังใช้ stable keys ---

def test_settle_uses_stable_keys_end_to_end(svc):
    tid = flow_to_judged(svc)  # ไม่ claim เอง — ให้ settle_approved ทำทั้งโฟลว์
    r = svc.settle_approved(tid, svc.make_confirmation_token(tid))
    assert r["status"] == "COMPLETED"
    cap_op = _op(svc, tid, "capture")
    pay_op = _op(svc, tid, "payout")
    assert cap_op["idempotency_key"] == f"judgepay-{tid}-capture"
    assert pay_op["idempotency_key"] == f"judgepay-{tid}-payout"
    assert cap_op["status"] == "SUCCEEDED" and pay_op["status"] == "SUCCEEDED"
    # mock จำ batch ตาม sender_batch_id ที่เสถียร
    assert f"judgepay-{tid}-payout" in svc.paypal._payout_idem
