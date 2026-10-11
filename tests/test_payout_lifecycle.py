"""B2 regression: payout lifecycle — item-level truth (mock mode).

กฎที่ทดสอบ: batch status อย่างเดียวห้ามสรุปว่าเงินถึงมือ —
ต้องดู transaction_status ของทุก item. สถานะไม่รู้จัก -> NEEDS_RECONCILIATION.
"""

import json

import pytest
import requests

from judgepay_paypal.escrow import EscrowError
from judgepay_paypal.payout_status import (
    normalize_item_status, normalize_payout_item, aggregate_task_status,
)
from tests.conftest import flow_to_judged


def _settle(svc, monkeypatch=None, item_status=None, items_json=None,
            batch_status=None):
    """settle เต็มโฟลว์พร้อมบังคับ item/batch status ของ mock."""
    if monkeypatch is not None:
        if item_status is not None:
            monkeypatch.setenv("PAYPAL_MOCK_ITEM_STATUS", item_status)
        if items_json is not None:
            monkeypatch.setenv("PAYPAL_MOCK_ITEMS_JSON", items_json)
        if batch_status is not None:
            monkeypatch.setenv("PAYPAL_MOCK_PAYOUT_STATUS", batch_status)
    tid = flow_to_judged(svc)
    r = svc.settle_approved(tid, svc.make_confirmation_token(tid))
    return tid, r


def _items(svc, tid):
    return svc.store.list_payout_items(tid)


# --- 1. batch SUCCESS + item PENDING -> PAYOUT_PENDING (ห้าม COMPLETED) ---

def test_batch_success_item_pending(svc, monkeypatch):
    tid, r = _settle(svc, monkeypatch, item_status="PENDING")
    assert r["status"] == "PAYOUT_PENDING"
    assert svc.get_task(tid)["status"] == "PAYOUT_PENDING"
    rows = _items(svc, tid)
    assert len(rows) == 1 and rows[0]["item_status"] == "PENDING"
    assert rows[0]["paypal_status"] == "PENDING"


# --- 2. batch SUCCESS + item UNCLAIMED -> PAYOUT_PENDING (เคสจริง UW9PA5HUJWAX6) ---

def test_batch_success_item_unclaimed(svc, monkeypatch):
    tid, r = _settle(svc, monkeypatch, item_status="UNCLAIMED")
    assert r["status"] == "PAYOUT_PENDING"
    assert svc.get_task(tid)["status"] == "PAYOUT_PENDING"
    rows = _items(svc, tid)
    assert rows[0]["item_status"] == "UNCLAIMED"


# --- 3. item FAILED -> PAYOUT_FAILED, ห้ามสร้าง payout ใหม่ ---

def test_item_failed_no_auto_repayout(svc, monkeypatch):
    tid, r = _settle(svc, monkeypatch, item_status="FAILED")
    assert r["status"] == "PAYOUT_FAILED"
    assert svc.get_task(tid)["status"] == "PAYOUT_FAILED"
    assert svc.paypal._mock_calls["payout_exec"] == 1  # ยิงครั้งเดียวเท่านั้น
    rows = _items(svc, tid)
    assert rows[0]["item_status"] == "FAILED"
    assert rows[0]["error_name"]  # เก็บ error ไว้ตรวจสอบย้อนหลัง
    # พยายาม settle ซ้ำต้องถูกปฏิเสธ (status ไม่อยู่ในข่าย) — ไม่มี payout ใหม่
    with pytest.raises(EscrowError):
        svc.settle_approved(tid, svc.make_confirmation_token(tid))
    assert svc.paypal._mock_calls["payout_exec"] == 1


# --- 4. CANCELED -> ไม่ COMPLETED; RETURNED -> PAYOUT_RETURNED ---

def test_item_canceled_not_completed(svc, monkeypatch):
    tid, r = _settle(svc, monkeypatch, item_status="CANCELED")
    assert r["status"] != "COMPLETED"
    assert r["status"] == "PAYOUT_FAILED"
    assert _items(svc, tid)[0]["item_status"] == "CANCELED"


def test_item_returned(svc, monkeypatch):
    tid, r = _settle(svc, monkeypatch, item_status="RETURNED")
    assert r["status"] == "PAYOUT_RETURNED"
    assert svc.get_task(tid)["status"] == "PAYOUT_RETURNED"


# --- 5. multi-item: สถานะแย่สุดชนะ ---

def test_multi_item_mixed_statuses(svc, monkeypatch):
    tid, r = _settle(svc, monkeypatch, items_json=json.dumps([
        {"transaction_status": "SUCCESS"},
        {"transaction_status": "PENDING"},
    ]))
    assert r["status"] == "PAYOUT_PENDING"
    assert len(_items(svc, tid)) == 2

    tid2, r2 = _settle(svc, monkeypatch, items_json=json.dumps([
        {"transaction_status": "SUCCESS"},
        {"transaction_status": "FAILED"},
    ]))
    assert r2["status"] == "PAYOUT_FAILED"


# --- 6. reconcile ซ้ำหลายรอบ -> ไม่สร้าง payout ใหม่ ---

def test_reconcile_twice_no_new_payout(svc):
    tid, r = _settle(svc)
    assert r["status"] == "COMPLETED"
    n = svc.paypal._mock_calls["payout_exec"]
    r1 = svc.reconcile(tid)
    r2 = svc.reconcile(tid)
    assert r1["status"] == "COMPLETED" and r2["status"] == "COMPLETED"
    assert svc.paypal._mock_calls["payout_exec"] == n
    assert len(svc.paypal._payouts) == 1  # batch เดียวเท่าเดิม


# --- 7. batch response ไม่มี items -> NEEDS_RECONCILIATION, เก็บ rows เดิม ---

def test_malformed_batch_no_items(svc, monkeypatch):
    tid, r = _settle(svc)
    assert r["status"] == "COMPLETED"
    n_rows = len(_items(svc, tid))
    assert n_rows == 1
    batch_id = svc.get_task(tid)["payout_batch_id"]

    def no_items(bid):
        return {"batch_id": bid, "status": "SUCCESS"}  # ไม่มี items key
    monkeypatch.setattr(svc.paypal, "get_payout_batch", no_items)
    rp = svc.refresh_payout(tid)
    assert rp["task_status"] == "NEEDS_RECONCILIATION"
    assert svc.get_task(tid)["status"] == "NEEDS_RECONCILIATION"
    assert len(_items(svc, tid)) == n_rows  # rows เดิมไม่ถูกลบ


# --- 8. item status ที่ไม่รู้จัก -> NEEDS_RECONCILIATION ห้าม success ---

def test_unknown_item_status(svc, monkeypatch):
    tid, r = _settle(svc, monkeypatch, items_json=json.dumps([
        {"transaction_status": "WEIRD_STATUS"},
    ]))
    assert r["status"] == "NEEDS_RECONCILIATION"
    assert r["status"] != "COMPLETED"
    assert _items(svc, tid)[0]["item_status"] == "NEEDS_RECONCILIATION"
    assert _items(svc, tid)[0]["paypal_status"] == "WEIRD_STATUS"


# --- 9. timeout ตอน refresh -> เก็บของเดิม + NEEDS_RECONCILIATION ---

def test_timeout_during_refresh(svc, monkeypatch):
    tid, r = _settle(svc)
    assert r["status"] == "COMPLETED"
    n_rows = len(_items(svc, tid))

    def boom(bid):
        raise requests.Timeout("mock: timeout on get batch")
    monkeypatch.setattr(svc.paypal, "get_payout_batch", boom)
    rp = svc.refresh_payout(tid)
    assert rp["task_status"] == "NEEDS_RECONCILIATION"
    assert svc.get_task(tid)["status"] == "NEEDS_RECONCILIATION"
    assert len(_items(svc, tid)) == n_rows  # rows เดิมอยู่ครบ
    # refresh รอบหน้าสำเร็จ -> กลับ COMPLETED ได้ (ไม่ติดค้าง)
    monkeypatch.undo()
    rp2 = svc.refresh_payout(tid)
    assert rp2["task_status"] == "COMPLETED"


# --- 10. batch ไม่ override item: DENIED+PENDING -> PAYOUT_PENDING ---

def test_batch_does_not_override_items(svc, monkeypatch):
    tid = flow_to_judged(svc)
    # สร้าง batch เอง: batch DENIED แต่ item PENDING
    monkeypatch.setenv("PAYPAL_MOCK_PAYOUT_STATUS", "DENIED")
    monkeypatch.setenv("PAYPAL_MOCK_ITEM_STATUS", "PENDING")
    r = svc.settle_approved(tid, svc.make_confirmation_token(tid))
    # settle: batch ไม่ SUCCESS -> NEEDS_RECONCILIATION (gate เดิม)
    assert r["status"] == "NEEDS_RECONCILIATION"
    # แต่ refresh_payout ระดับ item: item PENDING ชนะ batch DENIED
    rp = svc.refresh_payout(tid)
    assert rp["task_status"] == "PAYOUT_PENDING"

    tid2 = flow_to_judged(svc)
    monkeypatch.setenv("PAYPAL_MOCK_ITEM_STATUS", "FAILED")
    svc.settle_approved(tid2, svc.make_confirmation_token(tid2))
    rp2 = svc.refresh_payout(tid2)
    assert rp2["task_status"] == "PAYOUT_FAILED"


# --- 11. backfill: มี batch_id แต่ไม่มี item rows -> refresh สร้างให้ ---

def test_backfill_items(svc):
    tid, r = _settle(svc)
    assert r["status"] == "COMPLETED"
    batch_id = svc.get_task(tid)["payout_batch_id"]
    with svc.store._conn() as c:
        c.execute("DELETE FROM payout_items WHERE task_id = ?", (tid,))
    assert _items(svc, tid) == []
    rp = svc.refresh_payout(tid)
    assert rp["task_status"] == "COMPLETED"
    rows = _items(svc, tid)
    assert len(rows) == 1 and rows[0]["batch_id"] == batch_id
    assert rows[0]["item_status"] == "PAID"


# --- 12. B1 ไม่ถอยหลัง: replay ไม่สร้าง batch ใหม่ ---

def test_b1_idempotency_preserved(svc):
    tid, r = _settle(svc)
    assert r["status"] == "COMPLETED"
    task = svc.get_task(tid)
    r2 = svc._execute_op(task, "payout")  # replay ด้วย key เดิม
    assert r2.get("replayed") is True
    assert r2["batch_id"] == svc.get_task(tid)["payout_batch_id"]
    assert svc.paypal._mock_calls["payout_exec"] == 1
    assert len(svc.paypal._payouts) == 1
    # item rows ยังสอดคล้อง (refresh ใน replay อัปเดตให้)
    assert _items(svc, tid)[0]["item_status"] == "PAID"


# --- unit: mapping + aggregate (pure, ไม่ต้องมี DB) ---

def test_status_mapping_unit():
    assert normalize_item_status("SUCCESS") == "PAID"
    assert normalize_item_status("pending") == "PENDING"
    assert normalize_item_status("PROCESSING") == "PENDING"
    assert normalize_item_status("UNCLAIMED") == "UNCLAIMED"
    assert normalize_item_status("FAILED") == "FAILED"
    assert normalize_item_status("DENIED") == "FAILED"
    assert normalize_item_status("RETURNED") == "RETURNED"
    assert normalize_item_status("CANCELED") == "CANCELED"
    assert normalize_item_status("WEIRD") == "NEEDS_RECONCILIATION"
    assert normalize_item_status("") == "NEEDS_RECONCILIATION"
    assert normalize_item_status(None) == "NEEDS_RECONCILIATION"


def test_aggregate_unit():
    assert aggregate_task_status(["PAID"], True) == "COMPLETED"
    assert aggregate_task_status(["PAID"], False) == "NEEDS_RECONCILIATION"
    assert aggregate_task_status(["PAID", "PENDING"], True) == "PAYOUT_PENDING"
    assert aggregate_task_status(["UNCLAIMED"], True) == "PAYOUT_PENDING"
    assert aggregate_task_status(["PAID", "FAILED"], True) == "PAYOUT_FAILED"
    assert aggregate_task_status(["CANCELED"], True) == "PAYOUT_FAILED"
    assert aggregate_task_status(["RETURNED"], True) == "PAYOUT_RETURNED"
    assert aggregate_task_status(["PAID", "NEEDS_RECONCILIATION"], True) == \
        "NEEDS_RECONCILIATION"
    assert aggregate_task_status([], True) == "NEEDS_RECONCILIATION"


def test_normalize_real_item_shape():
    raw = {
        "payout_item_id": "2AZ8F8LKEBAMC",
        "transaction_id": "abc",
        "transaction_status": "UNCLAIMED",
        "payout_batch_id": "UW9PA5HUJWAX6",
        "payout_item": {
            "receiver": "worker@example.com",
            "amount": {"currency_code": "USD", "value": "1.00"},
            "sender_item_id": "judgepay-1-payout-item1",
        },
    }
    n = normalize_payout_item(raw)
    assert n["payout_item_id"] == "2AZ8F8LKEBAMC"
    assert n["item_status"] == "UNCLAIMED"
    assert n["receiver"] == "worker@example.com"
    assert n["amount"] == 1.00 and n["currency"] == "USD"
    assert n["sender_item_id"] == "judgepay-1-payout-item1"
