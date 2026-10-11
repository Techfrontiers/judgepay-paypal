"""Fail-closed judge policy — เกณฑ์ตัดสินแหล่งเดียวของทั้งระบบ.

ลำดับตัดสิน (fail-closed):
  1. deterministic hard-fail (evaluate_policy) — เจองานหลอก -> reject ทันที ไม่เรียก AI
  2. AI/LLM ให้คะแนน -> normalize_verdict บังคับ verdict ให้สอดคล้องกับ score เสมอ
  3. band_for แบ่งชะตา:
       score >= APPROVE_THRESHOLD (80) -> JUDGED      (รอ human confirm)
       REVIEW_MIN (60) <= score < 80    -> NEEDS_REVIEW (คนตัดสินใจเอง)
       score < 60                       -> reject (void คืนเงิน)
"""

import difflib
import hashlib
import json
import re

from .judge import _keywords, POLICY_VERSION  # noqa: F401 (re-export: แหล่งเดียว)

# --- thresholds: แหล่งเดียว ห้าม hardcode ค่าอื่นที่ไหน ---
APPROVE_THRESHOLD = 80
REVIEW_MIN = 60

COPY_BRIEF_RATIO = 0.7    # output คล้าย brief เกินนี้ = ก็อปโจทย์
STUFFING_MIN_WORDS = 20   # คำน้อยกว่านี้ไม่ตรวจ stuffing
STUFFING_UNIQUE_RATIO = 0.35  # unique/total ต่ำกว่านี้ = ยัดคีย์เวิร์ด
MIN_OUTPUT_LEN = 50       # สั้นกว่านี้ = ไม่ได้ส่งงานจริง


def evaluate_policy(description, criteria, output):
    """ตรวจ deterministic ก่อนเรียก AI.

    คืน list ของ check ที่ fail (ชื่อ check); list ว่าง = ผ่าน.
    criteria = list ของ {"text": ..., "required": bool}
    """
    fails = []
    out = output or ""

    # 1. ก็อป brief มาตรงๆ (ทั้งก้อน หรือทั้ง brief ฝังอยู่ใน output)
    if description and out.strip():
        ratio = difflib.SequenceMatcher(None, out.lower(), description.lower()).ratio()
        desc_norm = " ".join(description.lower().split())
        out_norm = " ".join(out.lower().split())
        if ratio > COPY_BRIEF_RATIO or (len(desc_norm) >= 80 and desc_norm in out_norm):
            fails.append("copy_brief")

    # 2. ยัดคีย์เวิร์ดซ้ำๆ ปั่นคะแนน
    words = re.findall(r"[a-z0-9]+", out.lower())
    if len(words) >= STUFFING_MIN_WORDS:
        if len(set(words)) / len(words) < STUFFING_UNIQUE_RATIO:
            fails.append("stuffing")

    # 3. สั้นเกินกว่าจะเป็นงานจริง
    if len(out.strip()) < MIN_OUTPUT_LEN:
        fails.append("min_length")

    # 4. ข้อบังคับใน acceptance criteria ไม่มี keyword ปรากฏในงานเลย
    out_kw = _keywords(out)
    for crit in criteria or []:
        if not crit.get("required"):
            continue
        kws = _keywords(crit.get("text", ""))
        if not kws:
            continue
        if not (kws & out_kw):
            fails.append(f"missing:{crit.get('text', '')[:40]}")

    return fails


def normalize_verdict(score, verdict, reasoning=""):
    """บังคับ verdict ให้สอดคล้องกับ score เสมอ.

    LLM ส่ง verdict ขัดกับ score มา -> override แล้วจดไว้ใน reasoning.
    กฎ: approve ได้ก็ต่อเมื่อ score >= 70 (ตรงกับ JUDGE_SYSTEM_PROMPT)
    """
    derived = "approve" if score >= 70 else "reject"
    if verdict not in ("approve", "reject"):
        verdict = derived
    if verdict != derived:
        reasoning = (
            f"{reasoning} [normalized: model said '{verdict}' "
            f"but score {score} -> '{derived}']"
        ).strip()
        verdict = derived
    return verdict, reasoning


def band_for(score):
    """ชะตาของงานจากคะแนน — fail-closed: ไม่แน่ใจ -> คนตรวจ, ต่ำ -> reject."""
    if score >= APPROVE_THRESHOLD:
        return "JUDGED"
    if score >= REVIEW_MIN:
        return "NEEDS_REVIEW"
    return "REJECT"


def _canonical_criteria(criteria):
    """normalize criteria ให้ deterministic ก่อน hash/เทียบ."""
    norm = []
    for c in criteria or []:
        if not isinstance(c, dict):
            continue
        norm.append({"required": bool(c.get("required")),
                     "text": str(c.get("text", ""))})
    return norm


def hash_criteria(criteria):
    """hash ของ acceptance criteria ที่ freeze — จับได้ถ้าโดนแก้ย้อนหลัง."""
    canon = json.dumps(_canonical_criteria(criteria), sort_keys=True,
                       ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def criteria_evidence(criteria, output):
    """หลักฐานประกอบการตัดสิน: keyword ที่เจอใน output ต่อแต่ละข้อบังคับ.

    คืน {criterion_text: [matched keywords]} — ตรวจย้อนหลังได้ว่าแต่ละข้อ
    ผ่านเพราะหลักฐานอะไร ไม่ใช่แค่ pass/fail ลอยๆ.
    """
    out_kw = _keywords(output or "")
    ev = {}
    for c in _canonical_criteria(criteria):
        if not c["required"] or not c["text"]:
            continue
        kws = _keywords(c["text"])
        ev[c["text"]] = sorted(kws & out_kw)
    return ev
