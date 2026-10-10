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
    worker_email_locked INTEGER DEFAULT 0,
    owner_id TEXT,
    acceptance_criteria TEXT,
    last_paypal_op TEXT,
    created_at TEXT NOT NULL,
    deadline_at TEXT
);
"""

# สถานะ: AWAITING_APPROVAL -> OPEN -> SUBMITTED -> JUDGED | NEEDS_REVIEW
#   -> SETTLING -> COMPLETED | NEEDS_RECONCILIATION
#   SUBMITTED -> REFUNDED (reject/void) | EXPIRED
STATUSES = ("AWAITING_APPROVAL", "OPEN", "SUBMITTED", "JUDGED", "NEEDS_REVIEW",
            "SETTLING", "COMPLETED", "REFUNDED", "EXPIRED", "NEEDS_RECONCILIATION")

# คอลัมน์ที่เพิ่มหลัง schema แรก — migrate ให้ DB เก่าอัตโนมัติ
_MIGRATIONS = (
    ("worker_email_locked", "INTEGER DEFAULT 0"),
    ("owner_id", "TEXT"),
    ("acceptance_criteria", "TEXT"),
    ("last_paypal_op", "TEXT"),
)


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
            cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)").fetchall()}
            for name, ddl in _MIGRATIONS:
                if name not in cols:
                    c.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")

    def create_task(self, description, amount, currency, deadline_at,
                    requester_email="", owner_id="", acceptance_criteria=""):
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO tasks (description, amount, currency, status,
                                      requester_email, owner_id,
                                      acceptance_criteria, created_at, deadline_at)
                   VALUES (?, ?, ?, 'AWAITING_APPROVAL', ?, ?, ?, ?, ?)""",
                (description, amount, currency, requester_email, owner_id,
                 acceptance_criteria, utcnow(), deadline_at),
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

    def claim_for_settlement(self, task_id, from_statuses):
        """Atomic claim: เปลี่ยนเป็น SETTLING ได้คนเดียวเท่านั้น.

        ใช้ UPDATE ... WHERE status IN (...) แบบ statement เดียว —
        คืน True ก็ต่อเมื่อ rowcount == 1 (ไม่มี race ระหว่าง check กับ set)
        """
        placeholders = ", ".join("?" for _ in from_statuses)
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE tasks SET status = 'SETTLING' WHERE id = ? "
                f"AND status IN ({placeholders})",
                (task_id, *from_statuses),
            )
            return cur.rowcount == 1
