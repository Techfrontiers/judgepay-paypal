"""Structured JSON logging to stderr — hackathon demo/prototype.

log ทุกบรรทัดเป็น JSON เดียว ไม่มี secret/token/header รั่ว:
fields ผ่าน scrub_value() เดียวกับ audit เสมอ.
"""

import json
import logging
import sys
from datetime import datetime, timezone

# import แบบ deferred กัน circular import (audit.py import logutil)
_scrub = None


def _scrub_value(value):
    global _scrub
    if _scrub is None:
        from judgepay_paypal.audit import scrub_value as sv
        _scrub = sv
    return _scrub(value)


def get_logger(name="judgepay"):
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def log_event(logger, level, event, msg="", task_id=None,
              correlation_id=None, **fields):
    """log หนึ่ง event เป็น JSON บรรทัดเดียว (fields ถูก scrub)."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": logging.getLevelName(level),
        "event": event,
        "msg": msg,
    }
    if task_id is not None:
        entry["task_id"] = task_id
    if correlation_id:
        entry["correlation_id"] = correlation_id
    if fields:
        entry["fields"] = _scrub_value(fields)
    try:
        logger.log(level, json.dumps(entry, ensure_ascii=False, default=str))
    except Exception:
        logger.log(level, json.dumps({"ts": entry["ts"], "level": entry["level"],
                                      "event": "log_encode_failed"}))
