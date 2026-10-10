"""SQLite task store — เก็บสถานะ escrow ทั้งหมดไว้ในเครื่องเดียว."""

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL DEFAULT 'AWAITING_APPROVAL',
    order_id TEXT,
    authorization_id TEXT,
    capture_id TEXT,
    payout_batch_id TEXT,
    worker_output TEXT,
    judge_score REAL,
    judge_verdict TEXT,
    judge_reasoning TEXT,
    requester_email TEXT,
    worker_email TEXT,
    created_at TEXT NOT NULL,
    deadline_at TEXT
);
"""

# สถานะ: AWAITING_APPROVAL -> OPEN -> SUBMITTED -> COMPLETED | REFUNDED | EXPIRED
STATUSES = ("AWAITING_APPROVAL", "OPEN", "SUBMITTED", "COMPLETED", "REFUNDED", "EXPIRED")


def _default_db_path():
    # <project_root>/data/judgepay.db โดยค่าเริ่มต้น
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "judgepay.db")


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    def __init__(self, db_path=None):
        self.db_path = db_path or os.getenv("JUDGEPAY_DB") or _default_db_path()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        with self._conn() as c:
            c.executescript(SCHEMA)

    def create_task(self, description, amount, currency, deadline_at, requester_email=""):
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO tasks (description, amount, currency, status,
                                      requester_email, created_at, deadline_at)
                   VALUES (?, ?, ?, 'AWAITING_APPROVAL', ?, ?, ?)""",
                (description, amount, currency, requester_email, utcnow(), deadline_at),
            )
            task_id = cur.lastrowid
        return self.get_task(task_id)

    def get_task(self, task_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def update_task(self, task_id, **fields):
        """อัปเดตฟิลด์ใดๆ ของ task."""
        if not fields:
            return self.get_task(task_id)
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*fields.values(), task_id))
        return self.get_task(task_id)

    def list_tasks(self, status=None, limit=100):
        with self._conn() as c:
            if status:
                rows = c.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
