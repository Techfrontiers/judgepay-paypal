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
# B2: สถานะ payout แยก 3 ระดับ — task ไม่สรุปจาก batch อย่างเดียว
#   PAYOUT_PENDING (เงินยังไม่ถึงมือ) | PAYOUT_FAILED (ต้องคนตัดสินใจ ห้ามจ่ายซ้ำอัตโนมัติ)
#   | PAYOUT_RETURNED (เงินตีกลับ)
STATUSES = ("AWAITING_APPROVAL", "OPEN", "SUBMITTED", "JUDGED", "NEEDS_REVIEW",
            "SETTLING", "COMPLETED", "REFUNDED", "EXPIRED", "NEEDS_RECONCILIATION",
            "PAYOUT_PENDING", "PAYOUT_FAILED", "PAYOUT_RETURNED",
            # B4: DISPUTED — มีข้อพิพาทค้างอยู่ ระงับ settlement จนกว่าจะ resolve
            "DISPUTED")

# B2: item-level payout lifecycle — เงินถึงมือผู้รับแต่ละรายจริงไหม
# Rollback: DROP TABLE payout_items (task/operations ไม่กระทบ)
PAYOUT_ITEMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS payout_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    payout_item_id TEXT UNIQUE,       -- PayPal's ID (อาจ NULL จนกว่าจะรู้)
    transaction_id TEXT,
    sender_item_id TEXT,              -- stable key ของเรา (judgepay-{task}-payout-item1)
    receiver TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    item_status TEXT NOT NULL,        -- PAID|PENDING|UNCLAIMED|FAILED|RETURNED|CANCELED|NEEDS_RECONCILIATION
    paypal_status TEXT NOT NULL,      -- transaction_status ดิบจาก PayPal
    batch_id TEXT,
    batch_status TEXT,                -- batch-level (informational เท่านั้น)
    error_name TEXT,
    error_message TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payout_items_task ON payout_items(task_id);
"""

# B4: dispute lifecycle + ledger — ตรวจสอบย้อนหลังได้ทุกเหตุการณ์เงินขยับ
# Rollback: DROP TABLE disputes / DROP TABLE ledger (task/operations ไม่กระทบ)
# กฎ: ledger เป็น append-only — ห้าม UPDATE/DELETE ประวัติเพื่อ "ให้ยอดตรง"
DISPUTES_SCHEMA = """
CREATE TABLE IF NOT EXISTS disputes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    opened_by TEXT NOT NULL,          -- ใครเปิด (requester/worker/operator)
    reason TEXT NOT NULL,
    status TEXT NOT NULL,             -- OPEN|UNDER_REVIEW|RESOLVED_UPHELD|RESOLVED_REJECTED|CLOSED
    evidence TEXT DEFAULT '[]',       -- JSON list ของ {by, note, at}
    reviewer TEXT DEFAULT '',         -- มนุษย์เท่านั้น — AI judge ตัดสิน dispute ไม่ได้
    decision TEXT DEFAULT '',         -- upheld | rejected
    decided_at TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disputes_task ON disputes(task_id);
"""

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    event TEXT NOT NULL,              -- DISPUTE_OPENED|DISPUTE_CLOSED|REFUND_INTENT|
                                      -- REFUND_SUCCEEDED|REFUND_FAILED|PAYOUT_ITEM_CANCELLED
    amount REAL DEFAULT 0,
    currency TEXT DEFAULT '',
    ref TEXT DEFAULT '',              -- PayPal resource id (refund_id / dispute id / item id)
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    -- กันเขียนซ้ำตอน concurrent/reconcile (เช่น สอง thread บันทึก REFUND_SUCCEEDED
    -- ของ refund_id เดียวกัน) — ซ้ำ -> คืน id เดิม ไม่ error
    UNIQUE(task_id, event, ref)
);
CREATE INDEX IF NOT EXISTS idx_ledger_task ON ledger(task_id);
"""

DISPUTE_STATUSES = ("OPEN", "UNDER_REVIEW", "RESOLVED_UPHELD",
                    "RESOLVED_REJECTED", "CLOSED")
# dispute ที่ยัง "ค้าง" — เปิดใหม่ซ้ำไม่ได้, settlement ถูกระงับ
_OPEN_DISPUTE_STATUSES = ("OPEN", "UNDER_REVIEW")

# B5: webhook event dedup + append-only audit trail
# Rollback: DROP TABLE webhook_events / DROP TABLE audit_log
# (tasks/operations/ledger ไม่กระทบ)
# กฎ: event_id จาก PayPal ห้าม INSERT ก่อน verify ลายเซ็นผ่าน —
# ไม่งั้นผู้โจมตี squat event_id ดัก event จริงได้ (ดู webhooks.py)
WEBHOOK_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY,       -- PayPal event id (UNIQUE = dedup)
    event_type TEXT NOT NULL,
    resource_id TEXT DEFAULT '',
    task_id INTEGER,                 -- task ที่ผูกได้ (อาจ NULL ถ้าผูกไม่ได้)
    status TEXT NOT NULL,            -- RECEIVED|PROCESSING|PROCESSED|FAILED
    result TEXT DEFAULT '',          -- สรุปผลการประมวล (เช่น duplicate-ignored)
    body_hash TEXT NOT NULL,         -- sha256(raw body) — เก็บ hash ไม่เก็บ body
    received_at TEXT NOT NULL,
    processed_at TEXT DEFAULT ''
);
"""

AUDIT_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,             -- ใคร (owner:abcd1234 / paypal-webhook / system)
    action TEXT NOT NULL,            -- เช่น TASK_CREATED / SETTLE_INTENT / WEBHOOK_SIG_FAILED
    target_type TEXT DEFAULT '',     -- task / dispute / op / webhook_event
    target_id TEXT DEFAULT '',
    outcome TEXT NOT NULL,           -- ok / refused / failed / duplicate-ignored ...
    correlation_id TEXT DEFAULT '',  -- idempotency key / event_id / task id
    details TEXT DEFAULT '{}',       -- JSON ที่ scrub แล้ว — ห้ามมี secret
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
"""

# คอลัมน์ที่เพิ่มหลัง schema แรก — migrate ให้ DB เก่าอัตโนมัติ
_MIGRATIONS = (
    ("worker_email_locked", "INTEGER DEFAULT 0"),
    ("owner_id", "TEXT"),
    ("acceptance_criteria", "TEXT"),
    ("last_paypal_op", "TEXT"),
    # B3: audit การตัดสิน — เกณฑ์/หลักฐาน/เวอร์ชัน ตรวจย้อนหลังได้
    ("judge_version", "TEXT"),
    ("criteria_hash", "TEXT"),
    ("criteria_evidence", "TEXT"),
    ("input_hash", "TEXT"),
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
            c.executescript(PAYOUT_ITEMS_SCHEMA)
            c.executescript(DISPUTES_SCHEMA)
            c.executescript(LEDGER_SCHEMA)
            c.executescript(WEBHOOK_EVENTS_SCHEMA)
            c.executescript(AUDIT_LOG_SCHEMA)
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

    # ---------- B2: item-level payout lifecycle ----------

    def upsert_payout_item(self, task_id, payout_item_id=None, transaction_id="",
                           sender_item_id="", receiver="", amount=0.0, currency="",
                           item_status="", paypal_status="", batch_id="",
                           batch_status="", error_name="", error_message=""):
        """insert หรือ update แถว item — จับคู่ด้วย payout_item_id (PayPal's ID).

        ถ้ายังไม่มี payout_item_id (ยังไม่รู้) จับคู่ด้วย (task_id, sender_item_id)
        แถวที่ payout_item_id IS NULL แทน. SQLite อนุญาต UNIQUE หลาย NULL ได้
        เลยต้องจับคู่เองไม่พึ่ง ON CONFLICT.
        """
        now = utcnow()
        with self._conn() as c:
            row = None
            if payout_item_id:
                row = c.execute(
                    "SELECT id FROM payout_items WHERE payout_item_id = ?",
                    (payout_item_id,),
                ).fetchone()
            if row is None and sender_item_id:
                row = c.execute(
                    """SELECT id FROM payout_items
                       WHERE task_id = ? AND sender_item_id = ?
                       AND payout_item_id IS NULL""",
                    (task_id, sender_item_id),
                ).fetchone()
            if row:
                c.execute(
                    """UPDATE payout_items SET
                           payout_item_id = COALESCE(?, payout_item_id),
                           transaction_id = ?, sender_item_id = ?,
                           receiver = ?, amount = ?, currency = ?,
                           item_status = ?, paypal_status = ?,
                           batch_id = ?, batch_status = ?,
                           error_name = ?, error_message = ?, updated_at = ?
                       WHERE id = ?""",
                    (payout_item_id, transaction_id, sender_item_id,
                     receiver, amount, currency, item_status, paypal_status,
                     batch_id, batch_status, error_name, error_message,
                     now, row["id"]),
                )
            else:
                c.execute(
                    """INSERT INTO payout_items
                       (task_id, payout_item_id, transaction_id, sender_item_id,
                        receiver, amount, currency, item_status, paypal_status,
                        batch_id, batch_status, error_name, error_message, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (task_id, payout_item_id, transaction_id, sender_item_id,
                     receiver, amount, currency, item_status, paypal_status,
                     batch_id, batch_status, error_name, error_message, now),
                )
        return self.list_payout_items(task_id)

    def list_payout_items(self, task_id):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM payout_items WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- B4: disputes ----------

    def create_dispute(self, task_id, opened_by, reason):
        """เปิด dispute — สถานะเริ่ม OPEN; ระงับ settlement ที่ชั้น escrow."""
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO disputes
                   (task_id, opened_by, reason, status, evidence, created_at)
                   VALUES (?, ?, ?, 'OPEN', '[]', ?)""",
                (task_id, opened_by, reason, utcnow()),
            )
            did = cur.lastrowid
            row = c.execute("SELECT * FROM disputes WHERE id = ?",
                            (did,)).fetchone()
        return dict(row)

    def get_dispute(self, task_id):
        """dispute ล่าสุดของ task (สถานะใดก็ได้)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM disputes WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_open_dispute(self, task_id):
        """dispute ที่ยังค้าง (OPEN/UNDER_REVIEW) — ถ้ามีคือ settlement ถูกระงับ."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM disputes WHERE task_id = ? AND status IN "
                "('OPEN', 'UNDER_REVIEW') ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_dispute(self, dispute_id, **fields):
        if not fields:
            return
        if "status" in fields and fields["status"] not in DISPUTE_STATUSES:
            raise ValueError(f"unknown dispute status: {fields['status']}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE disputes SET {sets} WHERE id = ?",
                      (*fields.values(), dispute_id))

    def list_disputes(self, task_id):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM disputes WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- B4: ledger (append-only) ----------

    def ledger_append(self, task_id, event, amount=0.0, currency="",
                      ref="", note=""):
        """บันทึกเหตุการณ์เงิน/ข้อพิพาท — append-only ห้ามแก้ประวัติ.

        (task_id, event, ref) ซ้ำ (เช่น concurrent thread) -> คืน id เดิม
        ไม่เขียนซ้ำ ไม่ raise.
        """
        with self._conn() as c:
            try:
                cur = c.execute(
                    """INSERT INTO ledger
                       (task_id, event, amount, currency, ref, note, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (task_id, event, float(amount or 0), currency or "",
                     ref or "", note or "", utcnow()),
                )
                return cur.lastrowid
            except sqlite3.IntegrityError:
                row = c.execute(
                    "SELECT id FROM ledger WHERE task_id = ? AND event = ? "
                    "AND ref = ?",
                    (task_id, event, ref or ""),
                ).fetchone()
                return row["id"] if row else None

    def ledger_list(self, task_id):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM ledger WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def ledger_sum(self, task_id, events):
        """ยอดรวมของ events ที่ระบุ — ไว้คำนวณยอด refundable / กันคืนเกิน."""
        if not events:
            return 0.0
        placeholders = ", ".join("?" for _ in events)
        with self._conn() as c:
            row = c.execute(
                f"SELECT COALESCE(SUM(amount), 0) AS total FROM ledger "
                f"WHERE task_id = ? AND event IN ({placeholders})",
                (task_id, *events),
            ).fetchone()
        return float(row["total"] or 0)

    def ledger_has(self, task_id, event, ref):
        """มี event+ref นี้บันทึกแล้วหรือยัง — กันเขียนซ้ำตอน reconcile."""
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM ledger WHERE task_id = ? AND event = ? AND ref = ? "
                "LIMIT 1",
                (task_id, event, ref),
            ).fetchone()
        return row is not None

    # ---------- B5: webhook event dedup ----------

    def webhook_event_get(self, event_id):
        """ดึง event ตาม PayPal event_id — ไม่มี -> None."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM webhook_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return dict(row) if row else None

    def webhook_event_insert_or_ignore(self, event_id, event_type,
                                       resource_id="", task_id=None,
                                       body_hash=""):
        """INSERT event ใหม่; ถ้า event_id ซ้ำ -> คืน False (dedup).

        เรียกหลัง verify ลายเซ็นผ่านเท่านั้น — ห้าม insert ของที่ยังไม่ verify.
        """
        with self._conn() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO webhook_events
                   (event_id, event_type, resource_id, task_id, status,
                    body_hash, received_at)
                   VALUES (?, ?, ?, ?, 'RECEIVED', ?, ?)""",
                (event_id, event_type, resource_id or "",
                 task_id, body_hash or "", utcnow()),
            )
            return cur.rowcount == 1

    def webhook_event_claim(self, event_id):
        """อ้างสิทธิ์ประมวลผลแบบ atomic — เจ้าของเดียวเท่านั้นได้ไป.

        UPDATE ... WHERE status IN (RECEIVED/FAILED/PROCESSING):
        rowcount == 1 แปลว่าเราได้สิทธิ์; == 0 แปลว่ามีคนประมวลอยู่แล้ว
        หรือประมวลเสร็จแล้ว (caller ต้องเช็ค status เอง).
        """
        with self._conn() as c:
            cur = c.execute(
                """UPDATE webhook_events SET status = 'PROCESSING'
                   WHERE event_id = ?
                   AND status IN ('RECEIVED', 'FAILED', 'PROCESSING')""",
                (event_id,),
            )
            return cur.rowcount == 1

    def webhook_event_mark(self, event_id, status, result=""):
        """จบการประมวล: PROCESSED หรือ FAILED พร้อมสรุปผล."""
        if status not in ("PROCESSED", "FAILED"):
            raise ValueError(f"bad webhook event status: {status}")
        with self._conn() as c:
            c.execute(
                """UPDATE webhook_events
                   SET status = ?, result = ?, processed_at = ?
                   WHERE event_id = ?""",
                (status, (result or "")[:500], utcnow(), event_id),
            )

    def webhook_event_set_task(self, event_id, task_id):
        """ผูก event กับ task หลัง route สำเร็จ (lookup/report เท่านั้น)."""
        with self._conn() as c:
            c.execute(
                "UPDATE webhook_events SET task_id = ? WHERE event_id = ?",
                (task_id, event_id),
            )

    # ---------- B5: append-only audit log ----------

    def audit_append(self, actor, action, target_type="", target_id="",
                     outcome="ok", correlation_id="", details="{}"):
        """เขียน audit หนึ่งแถว — append-only ไม่มี update/delete.

        ล้มเหลว -> exception หลุดออกไป (audit.py แปลงเป็น AuditError).
        """
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO audit_log
                   (actor, action, target_type, target_id, outcome,
                    correlation_id, details, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (actor, action, target_type or "", str(target_id or ""),
                 outcome, correlation_id or "", details or "{}", utcnow()),
            )
            return cur.lastrowid

    def audit_list(self, target_type=None, target_id=None, limit=200):
        """ดึง audit ย้อนหลัง — กรองตาม target ได้."""
        q = "SELECT * FROM audit_log"
        args = []
        conds = []
        if target_type:
            conds.append("target_type = ?")
            args.append(target_type)
        if target_id is not None:
            conds.append("target_id = ?")
            args.append(str(target_id))
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    # ---------- B5: resource -> task lookups (สำหรับ webhook routing) ----------

    def find_task_by_capture_id(self, capture_id):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM tasks WHERE capture_id = ? LIMIT 1",
                (capture_id,),
            ).fetchone()
        return dict(row) if row else None

    def find_task_by_authorization_id(self, authorization_id):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM tasks WHERE authorization_id = ? LIMIT 1",
                (authorization_id,),
            ).fetchone()
        return dict(row) if row else None

    def find_operation_by_paypal_ref(self, paypal_ref):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM operations WHERE paypal_ref = ? "
                "ORDER BY id DESC LIMIT 1",
                (paypal_ref,),
            ).fetchone()
        return dict(row) if row else None

    def find_task_by_payout_item_id(self, payout_item_id):
        with self._conn() as c:
            row = c.execute(
                """SELECT t.* FROM tasks t
                   JOIN payout_items pi ON pi.task_id = t.id
                   WHERE pi.payout_item_id = ? LIMIT 1""",
                (payout_item_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_payout_item_status(self, payout_item_id, item_status,
                                  paypal_status):
        """อัปเดตสถานะ item จาก webhook — ไม่สร้าง payout ใหม่ ไม่แตะเงิน."""
        with self._conn() as c:
            c.execute(
                """UPDATE payout_items
                   SET item_status = ?, paypal_status = ?, updated_at = ?
                   WHERE payout_item_id = ?""",
                (item_status, paypal_status, utcnow(), payout_item_id),
            )
