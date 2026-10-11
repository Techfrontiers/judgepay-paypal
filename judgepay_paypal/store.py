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

# B1: บันทึกทุก PayPal operation แบบ idempotent — durable source of truth
# ว่าธุรกรรมไหน "ตั้งใจจะยิง / ยิงแล้ว / สำเร็จแล้ว" (กัน crash แล้วไม่รู้ว่าเงินขยับไหม)
OPERATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    op_type TEXT NOT NULL,              -- capture | payout | void
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,         -- sha256(op_type|amount|currency|recipient)
    amount REAL,
    currency TEXT,
    recipient TEXT,
    status TEXT NOT NULL DEFAULT 'INTENDED',
    paypal_ref TEXT,                    -- capture_id / payout batch_id
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operations_task ON operations(task_id);
"""

# สถานะ operation: INTENDED (บันทึก intent แล้ว ยังไม่ยิง) -> SENT (ยิงแล้ว รอผล)
#   -> SUCCEEDED | FAILED (ล้มเหลวชัดเจน) | NEEDS_RECONCILIATION (ผลคลุมเครือ)
# กฎ: timeout/5xx ห้าม mark FAILED เด็ดขาด — ต้อง NEEDS_RECONCILIATION เท่านั้น
OP_STATUSES = ("INTENDED", "SENT", "SUCCEEDED", "FAILED", "NEEDS_RECONCILIATION")

# transition ที่ถูกต้องเท่านั้น — นอกนั้นคือ bug ห้ามเงียบ
# (SUCCEEDED -> SUCCEEDED อนุญาตเป็น no-op: สอง thread replay ด้วย key เดิม
#  ได้ผลเดียวกันจาก PayPal อยู่แล้ว)
_OP_TRANSITIONS = {
    "INTENDED": ("SENT", "FAILED"),
    "SENT": ("SENT", "SUCCEEDED", "FAILED", "NEEDS_RECONCILIATION"),
    "NEEDS_RECONCILIATION": ("SENT", "SUCCEEDED", "FAILED"),
    "FAILED": ("SENT",),
    "SUCCEEDED": ("SUCCEEDED",),
}

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
            c.executescript(OPERATIONS_SCHEMA)
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

    # ---------- B1: idempotent operations log ----------

    def record_intent(self, task_id, op_type, idempotency_key, request_hash,
                      amount=None, currency="", recipient=""):
        """บันทึก intent ก่อนยิง PayPal — crash-safe.

        INSERT อย่างเดียว: ถ้า key ซ้ำ (UNIQUE conflict) คืน row เดิม
        โดยไม่เขียนทับ — สอง process/thread ที่ชนกันจะเห็น op เดียวกัน
        """
        now = utcnow()
        with self._conn() as c:
            try:
                c.execute(
                    """INSERT INTO operations
                       (task_id, op_type, idempotency_key, request_hash,
                        amount, currency, recipient, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'INTENDED', ?, ?)""",
                    (task_id, op_type, idempotency_key, request_hash,
                     amount, currency, recipient, now, now),
                )
            except sqlite3.IntegrityError:
                pass  # key นี้มีแล้ว — ใช้ row เดิม (idempotent)
        return self.get_operation(idempotency_key)

    def get_operation(self, idempotency_key):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return dict(row) if row else None

    def list_operations(self, task_id):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM operations WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_open_operations(self):
        """op ที่ยังไม่จบ (INTENDED/SENT/NEEDS_RECONCILIATION) — ไว้ reconcile --all."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM operations WHERE status IN "
                "('INTENDED', 'SENT', 'NEEDS_RECONCILIATION') ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def transition_operation(self, idempotency_key, to_status, paypal_ref=None):
        """เปลี่ยนสถานะ op — อนุญาตเฉพาะ transition ที่ถูกต้องเท่านั้น.

        คืน True ถ้าสำเร็จ; raise ValueError ถ้า transition ผิดกฎ (bug)
        หรือหา op ไม่เจอ.
        """
        if to_status not in OP_STATUSES:
            raise ValueError(f"unknown operation status: {to_status}")
        with self._conn() as c:
            row = c.execute(
                "SELECT status FROM operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if not row:
                raise ValueError(f"operation not found: {idempotency_key}")
            from_status = row["status"]
            if to_status not in _OP_TRANSITIONS[from_status]:
                raise ValueError(
                    f"illegal operation transition {from_status} -> {to_status} "
                    f"for {idempotency_key}"
                )
            if paypal_ref is not None:
                c.execute(
                    "UPDATE operations SET status = ?, paypal_ref = ?, updated_at = ? "
                    "WHERE idempotency_key = ?",
                    (to_status, paypal_ref, utcnow(), idempotency_key),
                )
            else:
                c.execute(
                    "UPDATE operations SET status = ?, updated_at = ? "
                    "WHERE idempotency_key = ?",
                    (to_status, utcnow(), idempotency_key),
                )
            return True
