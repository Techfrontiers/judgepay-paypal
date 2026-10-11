"""B5 webhook security tests — mock only (NOT real PayPal behavior).

ครอบคลุม: signature valid/invalid/missing, headers ขาด, dedup,
concurrent delivery, out-of-order, unknown event, malformed,
crash recovery, verify-before-dedup (กัน squat), และกฎเหล็ก
"webhook ห้ามสั่ง money movement ใหม่".
"""

import json
import threading

import pytest

from judgepay_paypal.escrow import EscrowService, EscrowError
from judgepay_paypal.paypal_client import PayPalClient, PayPalError
from judgepay_paypal.store import TaskStore
from judgepay_paypal.judge import AIJudge
from judgepay_paypal import webhooks
from tests.conftest import TOKEN, flow_to_judged, GOOD_OUTPUT


def _tx_headers(eid="EVT-1"):
    return {
        "PAYPAL-TRANSMISSION-ID": f"tx-{eid}",
        "PAYPAL-TRANSMISSION-TIME": "2026-10-11T10:00:00Z",
        "PAYPAL-TRANSMISSION-SIG": "sig",
        "PAYPAL-CERT-URL": "https://api.paypal.com/cert",
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    }


def _body(event_id, event_type, resource):
    return json.dumps({
        "id": event_id, "event_type": event_type, "resource": resource,
    }).encode()


def _wh_svc(svc):
    """(paypal, store) สำหรับ process_webhook."""
    return svc.paypal, svc.store


def _settle(svc, tid):
    token = svc.make_confirmation_token(tid)
    return svc.settle_approved(tid, token)


# ---------- 1. verify_webhook_signature ----------

def test_verify_valid(svc):
    ok = svc.paypal.verify_webhook_signature(
        auth_algo="a", cert_url="c", transmission_id="t",
        transmission_sig="s", transmission_time="tt",
        webhook_id="wh", webhook_event={})
    assert ok is True


def test_verify_invalid(monkeypatch, svc):
    monkeypatch.setenv("PAYPAL_MOCK_WEBHOOK_VERIFY", "invalid")
    ok = svc.paypal.verify_webhook_signature(
        auth_algo="a", cert_url="c", transmission_id="t",
        transmission_sig="s", transmission_time="tt",
        webhook_id="wh", webhook_event={})
    assert ok is False


def test_verify_missing_raises(monkeypatch, svc):
    monkeypatch.setenv("PAYPAL_MOCK_WEBHOOK_VERIFY", "missing")
    with pytest.raises(PayPalError):
        svc.paypal.verify_webhook_signature(
            auth_algo="a", cert_url="c", transmission_id="t",
            transmission_sig="s", transmission_time="tt",
            webhook_id="wh", webhook_event={})


def test_verify_real_mode_without_webhook_id_raises():
    # real path (mock=False) แต่ webhook_id ว่าง -> fail-closed ก่อนยิง network
    c = PayPalClient(client_id="x", client_secret="y", mock=False)
    assert c.mock is False
    with pytest.raises(PayPalError):
        c.verify_webhook_signature(
            auth_algo="a", cert_url="c", transmission_id="t",
            transmission_sig="s", transmission_time="tt",
            webhook_id="", webhook_event={})


# ---------- 2. headers / malformed ----------

def test_missing_headers_rejected(svc):
    paypal, store = _wh_svc(svc)
    r = webhooks.process_webhook(paypal, store, _body("E1", "X", {}), {},
                                 escrow=svc)
    assert r["http_status"] == 400
    assert r["result"] == "headers-missing"
    assert store.webhook_event_get("E1") is None  # ไม่บันทึกอะไรเลย


def test_malformed_json_rejected(svc):
    paypal, store = _wh_svc(svc)
    r = webhooks.process_webhook(paypal, store, b"not-json{{{",
                                 _tx_headers(), escrow=svc)
    assert r["http_status"] == 400
    assert r["result"] == "malformed"


def test_missing_event_id_rejected(svc):
    paypal, store = _wh_svc(svc)
    raw = json.dumps({"event_type": "X", "resource": {}}).encode()
    r = webhooks.process_webhook(paypal, store, raw, _tx_headers(),
                                 escrow=svc)
    assert r["http_status"] == 400
    assert r["result"] == "missing-event-id"


# ---------- 3. invalid signature -> ไม่แตะ state ----------

def test_invalid_signature_no_state_change(monkeypatch, svc):
    monkeypatch.setenv("PAYPAL_MOCK_WEBHOOK_VERIFY", "invalid")
    tid = flow_to_judged(svc)
    _settle(svc, tid)
    task_before = svc.get_task(tid)
    ledger_before = svc.store.ledger_list(tid)

    paypal, store = _wh_svc(svc)
    r = webhooks.process_webhook(
        paypal, store,
        _body("E-BAD", "PAYMENT.CAPTURE.REFUNDED",
              {"id": task_before["capture_id"]}),
        _tx_headers("E-BAD"), escrow=svc)
    assert r["http_status"] == 400
    assert r["result"] == "sig-failed"
    assert store.webhook_event_get("E-BAD") is None
    assert svc.get_task(tid)["status"] == task_before["status"]
    assert svc.store.ledger_list(tid) == ledger_before
    # audit บันทึกความพยายามไว้ (แต่ไม่มี state เปลี่ยน)
    acts = [a["action"] for a in store.audit_list("webhook_event", "E-BAD")]
    assert "WEBHOOK_SIG_FAILED" in acts


def test_verify_error_is_transient_500(monkeypatch, svc):
    monkeypatch.setenv("PAYPAL_MOCK_WEBHOOK_VERIFY", "missing")
    paypal, store = _wh_svc(svc)
    r = webhooks.process_webhook(paypal, store, _body("E2", "X", {}),
                                 _tx_headers("E2"), escrow=svc)
    assert r["http_status"] == 500
    assert r["result"] == "verify-error"
    assert store.webhook_event_get("E2") is None  # ยังไม่ dedup — ให้ retry ได้


def test_no_squat_before_verify(monkeypatch, svc):
    """sig ไม่ผ่านต้องไม่จอง event_id — event จริงมาทีหลังต้องประมวลได้."""
    paypal, store = _wh_svc(svc)
    monkeypatch.setenv("PAYPAL_MOCK_WEBHOOK_VERIFY", "invalid")
    r1 = webhooks.process_webhook(paypal, store, _body("E-SQ", "X", {}),
                                  _tx_headers("E-SQ"), escrow=svc)
    assert r1["http_status"] == 400
    monkeypatch.setenv("PAYPAL_MOCK_WEBHOOK_VERIFY", "valid")
    r2 = webhooks.process_webhook(paypal, store, _body("E-SQ", "X", {}),
                                  _tx_headers("E-SQ"), escrow=svc)
    assert r2["http_status"] == 200  # ไม่โดน event ปลอม squat
    assert r2["result"] == "unknown-event"


# ---------- 4. valid + dedup + concurrent ----------

def test_unknown_event_ok_no_state_change(svc):
    paypal, store = _wh_svc(svc)
    r = webhooks.process_webhook(paypal, store, _body("E3", "NOPE.NOTHING", {}),
                                 _tx_headers("E3"), escrow=svc)
    assert r["http_status"] == 200
    assert r["result"] == "unknown-event"
    ev = store.webhook_event_get("E3")
    assert ev["status"] == "PROCESSED"
    acts = [a["action"] for a in store.audit_list("webhook_event", "E3")]
    assert "WEBHOOK_PROCESSED" in acts


def test_duplicate_event_single_effect(svc):
    tid = flow_to_judged(svc)
    _settle(svc, tid)
    capture_id = svc.get_task(tid)["capture_id"]
    paypal, store = _wh_svc(svc)
    raw = _body("E-DUP", "PAYMENT.CAPTURE.REVERSED", {"id": capture_id})
    h = _tx_headers("E-DUP")
    r1 = webhooks.process_webhook(paypal, store, raw, h, escrow=svc)
    r2 = webhooks.process_webhook(paypal, store, raw, h, escrow=svc)
    assert r1["http_status"] == 200
    assert r2["http_status"] == 200
    assert r2["result"] == "duplicate-ignored"
    notes = [l for l in store.ledger_list(tid) if l["event"] == "WEBHOOK_NOTE"]
    assert len(notes) == 1  # ประมวลครั้งเดียวเท่านั้น


def test_concurrent_delivery_single_processing(svc):
    tid = flow_to_judged(svc)
    _settle(svc, tid)
    capture_id = svc.get_task(tid)["capture_id"]
    paypal, store = _wh_svc(svc)
    raw = _body("E-CONC", "PAYMENT.CAPTURE.REVERSED", {"id": capture_id})
    h = _tx_headers("E-CONC")
    results = []

    def hit():
        results.append(webhooks.process_webhook(paypal, store, raw, h,
                                                escrow=svc))

    ts = [threading.Thread(target=hit) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(r["http_status"] == 200 for r in results)
    notes = [l for l in store.ledger_list(tid) if l["event"] == "WEBHOOK_NOTE"]
    assert len(notes) == 1  # เอฟเฟกต์ครั้งเดียว


def test_crash_mid_processing_redelivery_safe(svc):
    """crash หลัง claim แต่ก่อน mark -> redeliver ประมวลได้ ไม่ซ้ำ."""
    paypal, store = _wh_svc(svc)
    # จำลอง: verify ผ่าน + insert RECEIVED แล้ว crash (ไม่ claim/mark)
    assert store.webhook_event_insert_or_ignore(
        "E-CRASH", "NOPE.NOTHING", body_hash="abc") is True
    raw = _body("E-CRASH", "NOPE.NOTHING", {})
    r = webhooks.process_webhook(paypal, store, raw, _tx_headers("E-CRASH"),
                                 escrow=svc)
    assert r["http_status"] == 200
    assert r["result"] == "unknown-event"
    ev = store.webhook_event_get("E-CRASH")
    assert ev["status"] == "PROCESSED"
    # ส่งซ้ำอีก -> duplicate-ignored
    r2 = webhooks.process_webhook(paypal, store, raw, _tx_headers("E-CRASH"),
                                  escrow=svc)
    assert r2["result"] == "duplicate-ignored"


# ---------- 5. routing ----------

def test_refunded_unknown_capture_no_state_change(svc):
    paypal, store = _wh_svc(svc)
    r = webhooks.process_webhook(
        paypal, store,
        _body("E-U1", "PAYMENT.CAPTURE.REFUNDED", {"id": "NOPE123"}),
        _tx_headers("E-U1"), escrow=svc)
    assert r["http_status"] == 200
    assert r["result"] == "unmapped-resource"
    assert store.webhook_event_get("E-U1")["status"] == "PROCESSED"


def test_refunded_known_capture_syncs_once(svc, monkeypatch):
    """refund สำเร็จแล้ว (B4) + webhook REFUNDED -> sync ครั้งเดียว ไม่ซ้ำ."""
    tid = flow_to_judged(svc)
    _settle(svc, tid)
    capture_id = svc.get_task(tid)["capture_id"]
    monkeypatch.setenv("PAYPAL_MOCK_REFUND_STATUS", "COMPLETED")
    svc.refund_capture(tid, amount=25.00, reason="t", human_confirm=True,
                       owner_id="test-owner")
    n_ledger = len(svc.store.ledger_list(tid))

    paypal, store = _wh_svc(svc)
    raw = _body("E-R1", "PAYMENT.CAPTURE.REFUNDED", {"id": capture_id})
    r = webhooks.process_webhook(paypal, store, raw, _tx_headers("E-R1"),
                                 escrow=svc)
    assert r["http_status"] == 200
    assert r["result"] in ("refund-already-synced", "refund-synced:0")
    assert len(svc.store.ledger_list(tid)) == n_ledger  # ไม่เพิ่ม
    # reconcile ซ้ำก็ไม่เพิ่ม
    svc.reconcile(tid)
    assert len(svc.store.ledger_list(tid)) == n_ledger


def test_refund_failed_marks_op(svc):
    tid = flow_to_judged(svc)
    _settle(svc, tid)
    # สร้าง refund op ค้างแบบ manual (SENT + paypal_ref) — จำลอง crash
    key = f"judgepay-{tid}-refund-9"
    op = svc.store.record_intent(tid, "refund", key, "hash",
                                 amount=5.0, currency="USD", recipient="")
    svc.store.transition_operation(key, "SENT", paypal_ref="RF-FAIL-1")
    paypal, store = _wh_svc(svc)
    r = webhooks.process_webhook(
        paypal, store,
        _body("E-RF", "PAYMENT.REFUND.FAILED", {"id": "RF-FAIL-1"}),
        _tx_headers("E-RF"), escrow=svc)
    assert r["http_status"] == 200
    assert r["result"] == "refund-marked-failed"
    assert svc.store.get_operation(key)["status"] == "FAILED"
    assert svc.store.ledger_has(tid, "REFUND_FAILED", "RF-FAIL-1")
    # ส่งซ้ำ -> idempotent
    r2 = webhooks.process_webhook(
        paypal, store,
        _body("E-RF2", "PAYMENT.REFUND.FAILED", {"id": "RF-FAIL-1"}),
        _tx_headers("E-RF2"), escrow=svc)
    assert r2["result"] == "already-failed"
    assert len([l for l in store.ledger_list(tid)
                if l["event"] == "REFUND_FAILED"]) == 1


def test_payout_item_event_syncs_status(svc):
    tid = flow_to_judged(svc)
    _settle(svc, tid)
    items = svc.store.list_payout_items(tid)
    assert items, "settle ต้องสร้าง payout items"
    item_id = items[0]["payout_item_id"]
    paypal, store = _wh_svc(svc)
    r = webhooks.process_webhook(
        paypal, store,
        _body("E-PI", "PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
              {"payout_item_id": item_id, "transaction_status": "SUCCESS"}),
        _tx_headers("E-PI"), escrow=svc)
    assert r["http_status"] == 200
    assert r["result"] == "item-status-synced:PAID"
    rows = svc.store.list_payout_items(tid)
    assert rows[0]["item_status"] == "PAID"


def test_customer_dispute_created_auto_opens(svc):
    tid = flow_to_judged(svc)
    paypal, store = _wh_svc(svc)
    r = webhooks.process_webhook(
        paypal, store,
        _body("E-CD", "CUSTOMER.DISPUTE.CREATED",
              {"invoice_id": f"judgepay-{tid}-order"}),
        _tx_headers("E-CD"), escrow=svc)
    assert r["http_status"] == 200
    assert r["result"] == "dispute-auto-opened"
    assert svc.get_task(tid)["status"] == "DISPUTED"
    d = svc.store.get_open_dispute(tid)
    assert d and d["opened_by"] == "paypal-webhook"
    # ซ้ำ -> ไม่เปิดซ้อน
    r2 = webhooks.process_webhook(
        paypal, store,
        _body("E-CD2", "CUSTOMER.DISPUTE.CREATED",
              {"invoice_id": f"judgepay-{tid}-order"}),
        _tx_headers("E-CD2"), escrow=svc)
    assert r2["result"] == "dispute-already-open"


def test_customer_dispute_resolved_never_auto_resolves(svc):
    tid = flow_to_judged(svc)
    svc.open_dispute(tid, "tester", "manual", owner_id="test-owner")
    paypal, store = _wh_svc(svc)
    r = webhooks.process_webhook(
        paypal, store,
        _body("E-CR", "CUSTOMER.DISPUTE.RESOLVED",
              {"invoice_id": f"judgepay-{tid}-order"}),
        _tx_headers("E-CR"), escrow=svc)
    assert r["http_status"] == 200
    assert r["result"] == "noted-no-auto-resolve"
    assert svc.store.get_open_dispute(tid) is not None  # ยังเปิดอยู่
    assert svc.get_task(tid)["status"] == "DISPUTED"


def test_reversed_flags_needs_reconciliation(svc):
    tid = flow_to_judged(svc)
    _settle(svc, tid)
    capture_id = svc.get_task(tid)["capture_id"]
    paypal, store = _wh_svc(svc)
    r = webhooks.process_webhook(
        paypal, store,
        _body("E-REV", "PAYMENT.CAPTURE.REVERSED", {"id": capture_id}),
        _tx_headers("E-REV"), escrow=svc)
    assert r["http_status"] == 200
    # COMPLETED เป็น terminal -> บันทึก note อย่างเดียว ไม่เขียนทับ
    assert r["result"] == "noted-terminal-untouched"
    assert svc.get_task(tid)["status"] == "COMPLETED"


# ---------- 6. กฎเหล็ก: webhook ห้ามสั่ง money movement ----------

def test_webhook_never_moves_money(svc):
    tid = flow_to_judged(svc)
    _settle(svc, tid)
    task = svc.get_task(tid)
    items = svc.store.list_payout_items(tid)
    paypal, store = _wh_svc(svc)
    before = dict(paypal._mock_calls)
    events = [
        ("W1", "PAYMENT.CAPTURE.REFUNDED", {"id": task["capture_id"]}),
        ("W2", "PAYMENT.CAPTURE.REVERSED", {"id": task["capture_id"]}),
        ("W3", "PAYMENT.AUTHORIZATION.VOIDED",
         {"id": task["authorization_id"]}),
        ("W4", "PAYMENT.REFUND.FAILED", {"id": "RF-X"}),
        ("W5", "PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
         {"payout_item_id": items[0]["payout_item_id"],
          "transaction_status": "SUCCESS"}),
        ("W6", "PAYMENT.PAYOUTS-ITEM.DENIED",
         {"payout_item_id": items[0]["payout_item_id"],
          "transaction_status": "DENIED"}),
        ("W7", "CUSTOMER.DISPUTE.CREATED",
         {"invoice_id": f"judgepay-{tid}-order"}),
        ("W8", "CUSTOMER.DISPUTE.RESOLVED",
         {"invoice_id": f"judgepay-{tid}-order"}),
        ("W9", "SOME.UNKNOWN.EVENT", {}),
    ]
    for eid, etype, res in events:
        r = webhooks.process_webhook(paypal, store, _body(eid, etype, res),
                                     _tx_headers(eid), escrow=svc)
        assert r["http_status"] == 200, (eid, r)
    for k in ("capture_exec", "payout_exec", "void_exec", "refund_exec"):
        assert paypal._mock_calls[k] == before[k], k
