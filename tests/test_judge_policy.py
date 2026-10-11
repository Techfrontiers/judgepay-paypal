"""Regression: fail-closed judge policy."""

import pytest

from judgepay_paypal.policy import (
    APPROVE_THRESHOLD, REVIEW_MIN, band_for, evaluate_policy, normalize_verdict,
)
from tests.conftest import DESC, GOOD_OUTPUT, CRITERIA, flow_to_judged


def test_policy_passes_good_work():
    assert evaluate_policy(DESC, CRITERIA, GOOD_OUTPUT) == []


def test_policy_rejects_copy_brief():
    fails = evaluate_policy(DESC, [], DESC)  # ก็อปโจทย์มาตรงๆ
    assert "copy_brief" in fails


def test_policy_rejects_stuffing():
    stuffed = "excellent quality product " * 25 + "buy now best deal " * 10
    fails = evaluate_policy(DESC, [], stuffed)
    assert "stuffing" in fails


def test_policy_rejects_min_length():
    assert "min_length" in evaluate_policy(DESC, [], "done ok")


def test_policy_rejects_missing_required():
    output = ("This is a long and detailed deliverable about a completely different "
              "topic: gardening tips for indoor plants, watering schedules, sunlight "
              "needs, soil types, fertilizer choices, pruning techniques, pest control "
              "methods, repotting guidance, seasonal care routines, and harvesting herbs.")
    fails = evaluate_policy(DESC, CRITERIA, output)
    assert any(f.startswith("missing:") for f in fails)


def test_policy_optional_criterion_not_required():
    output = GOOD_OUTPUT  # ผ่านข้อบังคับ แต่ขาดข้อ optional บางข้อก็ไม่เป็นไร
    fails = evaluate_policy(DESC, [{"text": "mention xyzzy-plugh", "required": False}],
                            output)
    assert fails == []


def test_normalize_verdict_overrides_contradiction():
    v, r = normalize_verdict(95, "reject", "low quality")
    assert v == "approve"
    assert "normalized" in r
    v, r = normalize_verdict(20, "approve", "great")
    assert v == "reject"
    assert "normalized" in r
    v, r = normalize_verdict(85, "approve", "fine")
    assert v == "approve" and "normalized" not in r


def test_bands_single_source_of_truth():
    assert APPROVE_THRESHOLD == 80 and REVIEW_MIN == 60
    assert band_for(100) == "JUDGED"
    assert band_for(80) == "JUDGED"
    assert band_for(79) == "NEEDS_REVIEW"
    assert band_for(60) == "NEEDS_REVIEW"
    assert band_for(59) == "REJECT"
    assert band_for(0) == "REJECT"


def test_inconsistent_llm_output_normalized(svc, monkeypatch):
    # LLM บอก score 95 แต่ verdict reject -> ต้อง override เป็น approve -> JUDGED
    monkeypatch.setattr(svc.judge, "evaluate",
                        lambda d, o, **kw: {"score": 95, "verdict": "reject",
                                      "reasoning": "contradictory test"})
    r = svc.create_task(DESC, 25.00, "USD", 24, "", owner_id="o")
    tid = r["task_id"]
    svc.confirm_authorization(tid)
    svc.submit_work(tid, GOOD_OUTPUT, "worker@example.com")
    jr = svc.judge_only(tid)
    assert jr["status"] == "JUDGED"
    assert jr["verdict"] == "approve"
    assert "normalized" in jr["reasoning"]


def test_adversarial_stuffing_rejected_no_ai(svc, monkeypatch):
    # งานยัดคีย์เวิร์ดต้องโดน hard-fail ก่อน AI — AI ต้องไม่ถูกเรียก
    calls = []
    orig = svc.judge.evaluate
    monkeypatch.setattr(svc.judge, "evaluate",
                        lambda d, o, **kw: calls.append(1) or orig(d, o))
    r = svc.create_task(DESC, 25.00, "USD", 24, "", owner_id="o")
    tid = r["task_id"]
    svc.confirm_authorization(tid)
    stuffed = "ceramic coffee mug dishwasher-safe 350ml blue glaze gift packaging " * 12
    svc.submit_work(tid, stuffed, "worker@example.com")
    jr = svc.judge_only(tid)
    assert jr["status"] == "REFUNDED"
    assert "stuffing" in jr["reasoning"]
    assert calls == []  # AI ไม่ถูกเรียกเลย


def test_adversarial_copy_rejected(svc):
    r = svc.create_task(DESC, 25.00, "USD", 24, "", owner_id="o")
    tid = r["task_id"]
    svc.confirm_authorization(tid)
    svc.submit_work(tid, DESC + " " + DESC, "worker@example.com")
    jr = svc.judge_only(tid)
    assert jr["status"] == "REFUNDED"
    assert "copy_brief" in jr["reasoning"]
