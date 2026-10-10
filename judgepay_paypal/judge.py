"""AI judge — กรรมการตัดสินคุณภาพงานก่อนปล่อยเงิน.

โหมด:
  JUDGE_LLM=mock (ค่าเริ่มต้น) — heuristic ตรวจออฟไลน์ได้ ไม่ต้องมี API key
  JUDGE_LLM=openai-compatible  — เรียก OpenAI-compatible endpoint
      (JUDGE_API_URL, JUDGE_API_KEY, JUDGE_MODEL) แล้วบังคับให้ตอบ JSON
"""

import json
import os
import re

import requests

# คำทั่วไปที่ไม่นับเป็น keyword สำคัญ
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "this", "that", "it", "as", "at",
    "by", "from", "please", "write", "create", "make", "give", "need",
}

JUDGE_SYSTEM_PROMPT = """You are an impartial quality judge for freelance work.
Given a TASK BRIEF and a WORKER DELIVERABLE, score how well the deliverable
fulfills the brief.

Respond with ONLY a JSON object (no markdown, no extra text):
{"score": <0-100 integer>, "verdict": "approve"|"reject", "reasoning": "<one or two sentences>"}

Guidelines:
- 90-100: excellent, fully meets the brief
- 70-89: good, minor gaps
- 50-69: partial, significant gaps -> reject
- 0-49: poor or irrelevant -> reject
Verdict "approve" only when score >= 70."""


def _keywords(text):
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 3 and w not in STOPWORDS}


def mock_evaluate(description, output):
    """Heuristic แบบ deterministic: keyword overlap + ความยาวขั้นต่ำ."""
    brief_kw = _keywords(description)
    out_kw = _keywords(output or "")
    if not brief_kw:
        coverage = 0.0
    else:
        coverage = len(brief_kw & out_kw) / len(brief_kw)

    out_len = len((output or "").strip())
    # ความยาว: อย่างน้อย 50 ตัวอักษรถึงจะนับว่า "ส่งงานจริง"
    length_score = min(1.0, out_len / 200.0)

    score = round(100 * (0.7 * coverage + 0.3 * length_score))
    verdict = "approve" if score >= 60 else "reject"
    reasoning = (
        f"mock judge: keyword coverage {coverage:.0%} ({len(brief_kw & out_kw)}/{len(brief_kw)}), "
        f"output length {out_len} chars -> score {score}"
    )
    return {"score": score, "verdict": verdict, "reasoning": reasoning}


def llm_evaluate(description, output, api_url, api_key, model):
    """เรียก OpenAI-compatible chat completions แล้ว parse JSON."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"TASK BRIEF:\n{description}\n\nWORKER DELIVERABLE:\n{output}",
            },
        ],
        "temperature": 0.2,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    r = requests.post(f"{api_url.rstrip('/')}/chat/completions", json=payload,
                      headers=headers, timeout=60)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"].strip()
    # ตัด markdown fence ถ้ามี
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    data = json.loads(text)
    score = int(data.get("score", 0))
    verdict = data.get("verdict", "reject")
    if verdict not in ("approve", "reject"):
        verdict = "approve" if score >= 70 else "reject"
    return {"score": score, "verdict": verdict,
            "reasoning": str(data.get("reasoning", ""))[:500]}


class AIJudge:
    def __init__(self, backend=None):
        self.backend = (backend or os.getenv("JUDGE_LLM", "mock")).lower()
        self.api_url = os.getenv("JUDGE_API_URL", "")
        self.api_key = os.getenv("JUDGE_API_KEY", "")
        self.model = os.getenv("JUDGE_MODEL", "")

    def evaluate(self, description, output):
        """คืน {score, verdict, reasoning}."""
        if self.backend == "mock" or not self.api_url:
            return mock_evaluate(description, output)
        return llm_evaluate(description, output, self.api_url, self.api_key, self.model)
