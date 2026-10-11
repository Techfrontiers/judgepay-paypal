"""B4 regression: dispute / refund lifecycle (mock mode).

กฎที่ทดสอบ:
- dispute เปิด -> settlement ถูกระงับ; resolve โดยมนุษย์เท่านั้น (AI ไม่เกี่ยว)
- refund ต้อง human_confirm ชัดแจ้ง; ตรวจ capture จริงก่อนยิง; ไม่เกินยอดคงเหลือ
- refund ใช้ _execute_op (idempotent) — timeout -> NEEDS_RECONCILIATION ไม่ใช่ FAILED
- ledger append-only ไม่สร้างรายการซ้ำ; dispute/refund/payout/ledger แยกสถานะกัน
- payout item ที่จ่ายถึงมือแล้วเรียกคืนไม่ได้ (ยกเว้น UNCLAIMED -> cancel)
"""

import json
import threading

import pytest
import requests

from judgepay_paypal.escrow import EscrowError, _request_hash
from tests.conftest import flow_to_judged, DESC, CRITERIA


OWNER = "test-owner"


def _refund_key(svc, tid, seq=1):
    """refund key ตาม format ใหม่: ผูก capture_id กันชนข้าม authorization."""
    cap_id = svc.get_task(tid)["capture_id"]
    return f"judgepay-{tid}-{cap_id}-refund-{seq}"


def _capture_task(svc, owner=OWNER, amount=25.00):
    """create -> confirm -> capture (ยังไม่ payout) — มี capture_id พร้อม refund."""
    r = svc.create_task(DESC, amount, "USD", 24, "buyer@example.com",
                        owner_id=owner, acceptance_criteria=CRITERIA)
    tid = r["task_id"]
    svc.confirm_authorization(tid)
    cap = svc._execute_op(svc.get_task(tid), "capture")
    svc.store.update_task(tid, capture_id=cap["capture_id"])
    return tid


def _settle_paid(svc):
    """settle เต็มโฟลว์จน COMPLETED (item SUCCESS)."""
    tid = flow_to_judged(svc)
    r = svc.settle_approved(tid, svc.make_confirmation_token(tid))
    assert r["status"] == "COMPLETED", r
    return tid


# --- 1. unauthorized: เจ้าของไม่ตรง -> ปฏิเสธทุกทาง ---

def test_unauthorized_dispute_refund(svc):
    tid = _capture_task(svc, owner="owner-a")
    with pytest.raises(EscrowError, match="not your task"):
        svc.open_dispute(tid, "requester", "bad work", owner_id="owner-b")
    with pytest.raises(EscrowError, match="not your task"):
        svc.open_dispute(tid, "requester", "bad work", owner_id="")
    # เปิดด้วยเจ้าของถูกก่อน แล้วลอง resolve/refund ด้วยคนอื่น
    svc.open_dispute(tid, "requester", "bad work", owner_id="owner-a")
    with pytest.raises(EscrowError, match="not your task"):
        svc.resolve_dispute(tid, "boss", "upheld", owner_id="owner-b")
    with pytest.raises(EscrowError, match="not your task"):
        svc.refund_capture(tid, human_confirm=True, owner_id="owner-b")
    assert svc.get_task(tid)["status"] == "DISPUTED"  # ไม่ขยับ


# --- 2. dispute ระงับ settlement; resolve rejected -> NEEDS_REVIEW ---

def test_dispute_suspends_settlement(svc):
    tid = flow_to_judged(svc)
    r = svc.open_dispute(tid, "requester", "work does not match brief",
                         owner_id=OWNER)
    assert r["task_status"] == "DISPUTED"
    assert svc.get_task(tid)["status"] == "DISPUTED"
    # settlement ต้องถูกระงับ — แม้มี token ถูกต้อง
    # (make_confirmation_token ก็ปฏิเสธ DISPUTED; ตรวจ guard ใน settle_approved ตรงๆ)
    with pytest.raises(EscrowError, match="cannot confirm"):
        svc.make_confirmation_token(tid)
    with pytest.raises(EscrowError, match="dispute open"):
        svc.settle_approved(tid, "fake:token")
    # เปิดซ้ำไม่ได้
    with pytest.raises(EscrowError, match="already has an open"):
        svc.open_dispute(tid, "requester", "another reason", owner_id=OWNER)
    # resolve แบบ rejected -> NEEDS_REVIEW (ไม่ auto-settle)
    rr = svc.resolve_dispute(tid, "ops-lead", "rejected",
                             reason="brief was met", owner_id=OWNER)
    assert rr["task_status"] == "NEEDS_REVIEW"
    d = svc.store.get_dispute(tid)
    assert d["status"] == "RESOLVED_REJECTED" and d["reviewer"] == "ops-lead"
    # ledger มีเปิด+ปิด
    events = [e["event"] for e in svc.store.ledger_list(tid)]
    assert events == ["DISPUTE_OPENED", "DISPUTE_CLOSED"]
    # หลัง resolve แล้ว settle ได้ตามปกติ (มนุษย์ยืนยัน)
    s = svc.settle_approved(tid, svc.make_confirmation_token(tid),
                            human_approve=True)
    assert s["status"] == "COMPLETED"


def test_dispute_evidence_flow(svc):
    tid = _capture_task(svc)
    svc.open_dispute(tid, "worker", "paid late?", owner_id=OWNER)
    e1 = svc.add_dispute_evidence(tid, "worker", "screenshot attached",
                                  owner_id=OWNER)
    assert e1["dispute_status"] == "UNDER_REVIEW"  # OPEN -> UNDER_REVIEW
    assert e1["evidence_count"] == 1
    d = svc.store.get_dispute(tid)
    ev = json.loads(d["evidence"])
    assert ev[0]["by"] == "worker" and "screenshot" in ev[0]["note"]
    # resolve ต้องระบุ decision ที่ถูกต้อง + reviewer
    with pytest.raises(EscrowError, match="upheld.*rejected"):
        svc.resolve_dispute(tid, "boss", "maybe", owner_id=OWNER)
    with pytest.raises(EscrowError, match="reviewer is required"):
        svc.resolve_dispute(tid, "", "upheld", owner_id=OWNER)


# --- 3. full refund หลัง upheld -> REFUNDED, ledger ตรง ---

def test_full_refund_after_upheld(svc):
    tid = _capture_task(svc)
    svc.open_dispute(tid, "requester", "deliverable unusable", owner_id=OWNER)
    svc.resolve_dispute(tid, "ops-lead", "upheld",
                         reason="confirmed broken", owner_id=OWNER)
    r = svc.refund_capture(tid, reason="dispute upheld",
                           human_confirm=True, dispute_resolution=True,
                           owner_id=OWNER)
    assert r["status"] == "COMPLETED"
    assert r["task_status"] == "REFUNDED"
    assert svc.get_task(tid)["status"] == "REFUNDED"
    assert r["refund_id"]
    assert svc.store.ledger_sum(tid, ["REFUND_SUCCEEDED"]) == pytest.approx(25.00)
    # capture จริง (mock) บอก REFUNDED
    cap = svc.paypal.get_capture(svc.get_task(tid)["capture_id"])
    assert cap["status"] == "REFUNDED"
    # dispute_resolution ต้องมี dispute ที่ upheld จริง
    tid2 = _capture_task(svc)
    with pytest.raises(EscrowError, match="resolved as 'upheld'"):
        svc.refund_capture(tid2, human_confirm=True, dispute_resolution=True,
                           owner_id=OWNER)


def test_refund_requires_explicit_confirm(svc):
    tid = _capture_task(svc)
    with pytest.raises(EscrowError, match="explicit human confirmation"):
        svc.refund_capture(tid, owner_id=OWNER)
    assert svc.paypal._mock_calls["refund_exec"] == 0  # ไม่ยิงเลย


# --- 4. partial refund: คืนบางส่วน -> task ไม่ REFUNDED; คืนครบ -> REFUNDED ---

def test_partial_then_full_refund(svc):
    tid = _capture_task(svc)
    r1 = svc.refund_capture(tid, amount=10.00, reason="partial goodwill",
                            human_confirm=True, owner_id=OWNER)
    assert r1["status"] == "COMPLETED" and r1["amount"] == pytest.approx(10.00)
    assert svc.get_task(tid)["status"] != "REFUNDED"  # ยังคืนไม่ครบ
    assert svc.store.ledger_sum(tid, ["REFUND_SUCCEEDED"]) == pytest.approx(10.00)
    cap = svc.paypal.get_capture(svc.get_task(tid)["capture_id"])
    assert cap["status"] == "PARTIALLY_REFUNDED"
    # คืนส่วนที่เหลือ -> REFUNDED
    r2 = svc.refund_capture(tid, reason="rest", human_confirm=True,
                            owner_id=OWNER)
    assert r2["amount"] == pytest.approx(15.00)
    assert r2["task_status"] == "REFUNDED"
    assert svc.store.ledger_sum(tid, ["REFUND_SUCCEEDED"]) == pytest.approx(25.00)


# --- 5. refund เกินยอด -> ปฏิเสธก่อนยิง provider ---

def test_refund_over_refundable_refused(svc):
    tid = _capture_task(svc)
    with pytest.raises(EscrowError, match="refundable"):
        svc.refund_capture(tid, amount=999.99, human_confirm=True,
                           owner_id=OWNER)
    assert svc.paypal._mock_calls["refund_exec"] == 0
    # คืนไป 5 แล้วขอเกินที่เหลือ -> ปฏิเสธ
    svc.refund_capture(tid, amount=5.00, human_confirm=True, owner_id=OWNER)
    with pytest.raises(EscrowError, match="refundable is 20.00"):
        svc.refund_capture(tid, amount=20.01, human_confirm=True,
                           owner_id=OWNER)
    assert svc.paypal._mock_calls["refund_exec"] == 1


# --- 6. concurrent refunds -> provider ยิงครั้งเดียว ---

def test_concurrent_refunds_single_provider_call(svc):
    tid = _capture_task(svc)
    barrier = threading.Barrier(2)
    results = []

    def _do():
        barrier.wait()
        try:
            results.append(svc.refund_capture(
                tid, human_confirm=True, owner_id=OWNER))
        except Exception as e:  # noqa: BLE001 — เก็บผลทุกแบบ
            results.append(e)

    ts = [threading.Thread(target=_do) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    # provider ถูกยิงครั้งเดียว (idempotency ฝั่ง mock + key เดียวกัน)
    assert svc.paypal._mock_calls["refund_exec"] == 1
    # ledger ไม่เบิ้ล — ยอดคืนรวมต้อง 25.00 เท่านั้น
    assert svc.store.ledger_sum(tid, ["REFUND_SUCCEEDED"]) == pytest.approx(25.00)
    ok = [r for r in results if not isinstance(r, Exception)]
    assert len(ok) >= 1  # อย่างน้อยหนึ่ง thread สำเร็จ


# --- 7. timeout -> NEEDS_RECONCILIATION; reconcile กู้กลับได้ ---

def test_timeout_then_reconcile_recovers(svc, monkeypatch):
    tid = _capture_task(svc)
    monkeypatch.setenv("PAYPAL_MOCK_FAIL_MODE", "timeout_after_refund")
    with pytest.raises(EscrowError, match="NEEDS_RECONCILIATION"):
        svc.refund_capture(tid, human_confirm=True, owner_id=OWNER)
    op = svc.store.get_operation(_refund_key(svc, tid))
    assert op["status"] == "NEEDS_RECONCILIATION"  # ห้าม mark FAILED
    assert svc.get_task(tid)["status"] != "REFUNDED"
    assert svc.paypal._mock_calls["refund_exec"] == 1  # provider ทำไปแล้วครั้งเดียว
    # reconcile: replay ด้วย key เดิม -> ได้ผลเดิม ไม่ยิงซ้ำ
    monkeypatch.delenv("PAYPAL_MOCK_FAIL_MODE")
    r = svc.reconcile(tid)
    assert svc.paypal._mock_calls["refund_exec"] == 1
    assert svc.store.get_operation(_refund_key(svc, tid))["status"] == "SUCCEEDED"
    assert svc.store.ledger_sum(tid, ["REFUND_SUCCEEDED"]) == pytest.approx(25.00)
    assert svc.get_task(tid)["status"] == "REFUNDED"
    assert any("refund" in a for a in r["actions"])


# --- 8. crash หลัง provider สำเร็จ (DB เขียนไม่ทัน) -> reconcile เจอของเดิม ---

def test_crash_after_success_recovers_via_replay(svc):
    tid = _capture_task(svc)
    cap_id = svc.get_task(tid)["capture_id"]
    key = _refund_key(svc, tid)
    # provider ประมวลผลสำเร็จแล้ว (มี refund_id ฝั่ง PayPal)
    pres = svc.paypal.refund_capture(cap_id, amount=25.00, currency="USD",
                                     idempotency_key=key)
    # จำลอง crash: DB มีแค่ intent SENT ไม่มี paypal_ref (mark SUCCEEDED หาย)
    svc.store.record_intent(tid, "refund", key,
                            _request_hash("refund", 25.00, "USD", ""),
                            amount=25.00, currency="USD", recipient="")
    svc.store.transition_operation(key, "SENT")
    # reconcile: replay ด้วย key เดิม -> PayPal คืนผลเดิม ไม่ทำซ้ำ
    r = svc.reconcile(tid)
    assert svc.paypal._mock_calls["refund_exec"] == 1  # ไม่ยิงซ้ำ
    op = svc.store.get_operation(key)
    assert op["status"] == "SUCCEEDED"
    assert op["paypal_ref"] == pres["refund_id"]
    assert svc.store.ledger_sum(tid, ["REFUND_SUCCEEDED"]) == pytest.approx(25.00)
    assert svc.get_task(tid)["status"] == "REFUNDED"
    assert r["success"]


# --- 9. refund ถูกปฏิเสธชัดเจน (422) -> FAILED, ledger ครบ, task ไม่ขยับ ---

def test_refund_definite_failure(svc, monkeypatch):
    tid = _capture_task(svc)
    monkeypatch.setenv("PAYPAL_MOCK_FAIL_MODE", "error_422_refund")
    with pytest.raises(EscrowError, match="FAILED"):
        svc.refund_capture(tid, human_confirm=True, owner_id=OWNER)
    op = svc.store.get_operation(_refund_key(svc, tid))
    assert op["status"] == "FAILED"
    assert svc.get_task(tid)["status"] != "REFUNDED"
    assert svc.store.ledger_has(tid, "REFUND_FAILED",
                                _refund_key(svc, tid))
    assert svc.store.ledger_sum(tid, ["REFUND_SUCCEEDED"]) == 0


# --- 10. response แปลก -> NEEDS_RECONCILIATION (ไม่เดา) ---

def test_unknown_refund_shape_fail_closed(svc, monkeypatch):
    tid = _capture_task(svc)

    def _weird(*a, **k):
        return {"refund_id": "WEIRDID123", "status": "WEIRD"}

    monkeypatch.setattr(svc.paypal, "refund_capture", _weird)
    monkeypatch.setattr(svc.paypal, "get_refund",
                        lambda rid: (_ for _ in ()).throw(
                            Exception("mock: unknown refund")))
    r = svc.refund_capture(tid, human_confirm=True, owner_id=OWNER)
    assert r["status"] == "UNKNOWN"
    assert r["task_status"] == "NEEDS_RECONCILIATION"
    assert svc.get_task(tid)["status"] == "NEEDS_RECONCILIATION"
    assert svc.get_task(tid)["status"] != "REFUNDED"


# --- 11. dispute หลัง payout PAID: refund จาก capture ได้; cancel item ไม่ได้ ---

def test_dispute_after_paid_payout(svc):
    tid = _settle_paid(svc)
    items = svc.store.list_payout_items(tid)
    assert items and items[0]["item_status"] == "PAID"
    svc.open_dispute(tid, "requester", "want money back", owner_id=OWNER)
    svc.resolve_dispute(tid, "ops-lead", "upheld", owner_id=OWNER)
    # refund จาก capture ยังทำได้ (เงิน hold อยู่กับ PayPal)
    r = svc.refund_capture(tid, human_confirm=True, dispute_resolution=True,
                           owner_id=OWNER)
    assert r["task_status"] == "REFUNDED"
    # แต่ cancel payout item ที่ PAID แล้ว -> ปฏิเสธ (เรียกเงินคืนไม่ได้)
    with pytest.raises(EscrowError, match="cannot cancel.*PAID"):
        svc.cancel_payout_item(tid, items[0]["payout_item_id"], owner_id=OWNER)


# --- 12. UNCLAIMED -> cancel ได้; เงินกลับ balance ผู้ส่ง (ไม่ใช่ capture) ---

def test_cancel_unclaimed_item(svc, monkeypatch):
    monkeypatch.setenv("PAYPAL_MOCK_ITEM_STATUS", "UNCLAIMED")
    tid = flow_to_judged(svc)
    r = svc.settle_approved(tid, svc.make_confirmation_token(tid))
    assert r["status"] == "PAYOUT_PENDING"
    items = svc.store.list_payout_items(tid)
    assert items[0]["item_status"] == "UNCLAIMED"
    c = svc.cancel_payout_item(tid, items[0]["payout_item_id"], owner_id=OWNER)
    assert c["item_status"] == "CANCELED"
    assert "sender" in c["note"] and "not to the capture" in c["note"]
    rows = svc.store.list_payout_items(tid)
    assert rows[0]["item_status"] == "CANCELED"
    ev = [e for e in svc.store.ledger_list(tid)
          if e["event"] == "PAYOUT_ITEM_CANCELLED"]
    assert len(ev) == 1 and ev[0]["ref"] == items[0]["payout_item_id"]


# --- 13. FAILED / RETURNED item -> cancel ไม่ได้ ---

def test_cancel_failed_returned_item_refused(svc, monkeypatch):
    for st, task_st in (("FAILED", "PAYOUT_FAILED"),
                        ("RETURNED", "PAYOUT_RETURNED")):
        monkeypatch.setenv("PAYPAL_MOCK_ITEM_STATUS", st)
        tid = flow_to_judged(svc)
        r = svc.settle_approved(tid, svc.make_confirmation_token(tid))
        assert r["status"] == task_st, (st, r)
        item_id = svc.store.list_payout_items(tid)[0]["payout_item_id"]
        with pytest.raises(EscrowError, match="cannot cancel"):
            svc.cancel_payout_item(tid, item_id, owner_id=OWNER)
        monkeypatch.delenv("PAYPAL_MOCK_ITEM_STATUS")


# --- 14. dispute ได้โดยไม่มี capture; แต่ refund ต้องมี capture ---

def test_dispute_without_capture(svc):
    r = svc.create_task(DESC, 25.00, "USD", 24, "buyer@example.com",
                        owner_id=OWNER, acceptance_criteria=CRITERIA)
    tid = r["task_id"]
    svc.confirm_authorization(tid)  # มีแค่ authorization ยังไม่ capture
    d = svc.open_dispute(tid, "requester", "changed my mind", owner_id=OWNER)
    assert d["task_status"] == "DISPUTED"
    with pytest.raises(EscrowError, match="no capture"):
        svc.refund_capture(tid, human_confirm=True, owner_id=OWNER)


# --- 16. refund ซ้ำตอน op ค้าง -> ปฏิเสธ (กันคืนซ้ำ), reconcile ก่อน ---

def test_no_double_refund_while_pending(svc, monkeypatch):
    tid = _capture_task(svc)
    monkeypatch.setenv("PAYPAL_MOCK_FAIL_MODE", "timeout_after_refund")
    with pytest.raises(EscrowError, match="NEEDS_RECONCILIATION"):
        svc.refund_capture(tid, human_confirm=True, owner_id=OWNER)
    # เรียก refund ใหม่ทั้งที่ op เก่ายังคลุมเครือ -> ต้องถูกปฏิเสธ (ไม่ยิงซ้ำ)
    with pytest.raises(EscrowError, match="run reconcile before"):
        svc.refund_capture(tid, human_confirm=True, owner_id=OWNER)
    assert svc.paypal._mock_calls["refund_exec"] == 1  # provider โดนครั้งเดียว
    # reconcile กู้ -> เรียกใหม่ได้ (ยอดหมดแล้ว -> ปฏิเสธแบบสุภาพ ไม่ยิง)
    monkeypatch.delenv("PAYPAL_MOCK_FAIL_MODE")
    svc.reconcile(tid)
    assert svc.get_task(tid)["status"] == "REFUNDED"
    with pytest.raises(EscrowError, match="nothing left to refund"):
        svc.refund_capture(tid, human_confirm=True, owner_id=OWNER)
    assert svc.paypal._mock_calls["refund_exec"] == 1


# --- 15. refund PENDING จาก provider -> NEEDS_RECONCILIATION รอ sync ---

def test_refund_pending_syncs_on_reconcile(svc, monkeypatch):
    tid = _capture_task(svc)
    monkeypatch.setenv("PAYPAL_MOCK_REFUND_STATUS", "PENDING")
    r = svc.refund_capture(tid, human_confirm=True, owner_id=OWNER)
    assert r["status"] == "PENDING"
    assert r["task_status"] == "NEEDS_RECONCILIATION"
    assert svc.store.ledger_has(tid, "REFUND_INTENT", r["refund_id"])
    # PayPal ประมวลผลเสร็จทีหลัง -> reconcile sync เป็น COMPLETED
    svc.paypal._refunds[r["refund_id"]]["status"] = "COMPLETED"
    svc.paypal._save_mock()
    monkeypatch.delenv("PAYPAL_MOCK_REFUND_STATUS")
    rc = svc.reconcile(tid)
    assert svc.store.ledger_sum(tid, ["REFUND_SUCCEEDED"]) == pytest.approx(25.00)
    assert svc.get_task(tid)["status"] == "REFUNDED"
    assert any("refund" in a for a in rc["actions"])
