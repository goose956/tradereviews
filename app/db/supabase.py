"""Local SQLite database layer with a Supabase-compatible query builder.

Drop-in replacement for the Supabase client so the rest of the codebase
needs zero changes.  Swap back to Supabase later by reverting this file.
"""

from __future__ import annotations

import re as _re
import sqlite3
import uuid
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

_volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
if _volume:
    DB_PATH = Path(_volume) / "local.db"
else:
    DB_PATH = Path(__file__).resolve().parent.parent.parent / "local.db"

# ── Schema bootstrap ─────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS businesses (
    id                   TEXT PRIMARY KEY,
    owner_name           TEXT NOT NULL DEFAULT '',
    business_name        TEXT NOT NULL,
    phone_number         TEXT NOT NULL UNIQUE,
    email                TEXT DEFAULT '',
    trade_type           TEXT,
    google_place_id      TEXT,
    google_review_link   TEXT,
    google_refresh_token TEXT,
    google_account_id    TEXT,
    google_location_id   TEXT,
    stripe_customer_id   TEXT,
    stripe_subscription_id TEXT,
    subscription_status  TEXT NOT NULL DEFAULT 'active',
    auto_reply_enabled   INTEGER NOT NULL DEFAULT 1,
    auto_reply_threshold INTEGER NOT NULL DEFAULT 4,
    auto_reply_positive_msg TEXT NOT NULL DEFAULT 'Thank you so much for your kind review, {reviewer_name}! We really appreciate your support and are glad you had a great experience with {business_name}.',
    auto_reply_negative_msg TEXT NOT NULL DEFAULT 'Thank you for your feedback, {reviewer_name}. We''re sorry your experience didn''t meet expectations and will be in touch to address any concerns.',
    active_customer_phone TEXT DEFAULT '',
    business_address     TEXT NOT NULL DEFAULT '',
    business_city        TEXT NOT NULL DEFAULT '',
    business_postcode    TEXT NOT NULL DEFAULT '',
    business_country     TEXT NOT NULL DEFAULT 'GB',
    tax_label            TEXT NOT NULL DEFAULT 'VAT',
    tax_number           TEXT NOT NULL DEFAULT '',
    tax_rate             REAL NOT NULL DEFAULT 20.0,
    default_payment_terms TEXT NOT NULL DEFAULT 'Payment due within 14 days',
    bank_details         TEXT NOT NULL DEFAULT '',
    accepted_payment_methods TEXT NOT NULL DEFAULT 'cash,bank_transfer',
    payment_link         TEXT NOT NULL DEFAULT '',
    currency             TEXT NOT NULL DEFAULT 'GBP',
    confirm_before_send  INTEGER NOT NULL DEFAULT 0,
    vat_registered       INTEGER NOT NULL DEFAULT 0,
    twilio_number        TEXT NOT NULL DEFAULT '',
    twilio_number_sid    TEXT NOT NULL DEFAULT '',
    brand_color          TEXT NOT NULL DEFAULT '#16a34a',
    logo_url             TEXT NOT NULL DEFAULT '',
    followup_enabled     INTEGER NOT NULL DEFAULT 1,
    followup_interval_days INTEGER NOT NULL DEFAULT 3,
    followup_max_count   INTEGER NOT NULL DEFAULT 2,
    followup_message     TEXT NOT NULL DEFAULT 'Hi {first_name}, just a quick reminder — we''d really appreciate your feedback! It only takes a minute. Thank you 😊',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id                  TEXT PRIMARY KEY,
    business_id         TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    phone_number        TEXT NOT NULL,
    email               TEXT NOT NULL DEFAULT '',
    review_requested_at TEXT,
    review_link_sent    INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'request_sent',
    followup_count      INTEGER NOT NULL DEFAULT 0,
    last_followup_at    TEXT,
    whatsapp_opted_in   INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_biz_phone
    ON customers (business_id, phone_number);

CREATE TABLE IF NOT EXISTS admin_campaigns (
    id               TEXT PRIMARY KEY,
    message_body     TEXT NOT NULL,
    filter_status    TEXT NOT NULL DEFAULT 'all',
    filter_trade     TEXT NOT NULL DEFAULT 'all',
    total_recipients INTEGER NOT NULL DEFAULT 0,
    sent_count       INTEGER NOT NULL DEFAULT 0,
    failed_count     INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_drafts (
    id               TEXT PRIMARY KEY,
    business_id      TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    google_review_id TEXT NOT NULL,
    reviewer_name    TEXT,
    review_text      TEXT,
    star_rating      INTEGER,
    ai_draft_reply   TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending_approval',
    sent_to_owner    INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id            TEXT PRIMARY KEY,
    business_id   TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    direction     TEXT NOT NULL DEFAULT 'outbound',
    to_phone      TEXT NOT NULL,
    message_type  TEXT NOT NULL DEFAULT 'text',
    message_body  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'sent',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id              TEXT PRIMARY KEY,
    business_id     TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    customer_id     TEXT REFERENCES customers(id) ON DELETE SET NULL,
    invoice_number  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft',
    subtotal        REAL NOT NULL DEFAULT 0,
    tax_rate        REAL NOT NULL DEFAULT 20.0,
    tax_amount      REAL NOT NULL DEFAULT 0,
    total           REAL NOT NULL DEFAULT 0,
    currency        TEXT NOT NULL DEFAULT 'GBP',
    payment_terms   TEXT NOT NULL DEFAULT 'Payment due within 14 days',
    notes           TEXT NOT NULL DEFAULT '',
    due_date        TEXT,
    sent_at         TEXT,
    payment_method  TEXT NOT NULL DEFAULT '',
    payment_link    TEXT NOT NULL DEFAULT '',
    paid_at         TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quotes (
    id              TEXT PRIMARY KEY,
    business_id     TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    customer_id     TEXT REFERENCES customers(id) ON DELETE SET NULL,
    quote_number    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft',
    subtotal        REAL NOT NULL DEFAULT 0,
    tax_rate        REAL NOT NULL DEFAULT 20.0,
    tax_amount      REAL NOT NULL DEFAULT 0,
    total           REAL NOT NULL DEFAULT 0,
    currency        TEXT NOT NULL DEFAULT 'GBP',
    valid_until     TEXT,
    notes           TEXT NOT NULL DEFAULT '',
    sent_at         TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS line_items (
    id          TEXT PRIMARY KEY,
    parent_id   TEXT NOT NULL,
    parent_type TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    quantity    REAL NOT NULL DEFAULT 1,
    unit_price  REAL NOT NULL DEFAULT 0,
    total       REAL NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS expenses (
    id              TEXT PRIMARY KEY,
    business_id     TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    vendor          TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT 'general',
    date            TEXT NOT NULL DEFAULT '',
    subtotal        REAL NOT NULL DEFAULT 0,
    tax_amount      REAL NOT NULL DEFAULT 0,
    total           REAL NOT NULL DEFAULT 0,
    currency        TEXT NOT NULL DEFAULT 'GBP',
    receipt_data    TEXT NOT NULL DEFAULT '',
    receipt_image   TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id              TEXT PRIMARY KEY,
    business_id     TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    customer_id     TEXT REFERENCES customers(id) ON DELETE SET NULL,
    customer_name   TEXT NOT NULL DEFAULT '',
    customer_phone  TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    date            TEXT NOT NULL DEFAULT '',
    time            TEXT NOT NULL DEFAULT '',
    duration_mins   INTEGER NOT NULL DEFAULT 60,
    notes           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'confirmed',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_codes (
    id          TEXT PRIMARY KEY,
    phone       TEXT NOT NULL,
    code        TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id          TEXT PRIMARY KEY,
    business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    token       TEXT NOT NULL UNIQUE,
    expires_at  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_SQL)
    # ── Migrations for existing databases ──
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that may be missing in older databases."""
    cur = conn.execute("PRAGMA table_info(customers)")
    cols = {row[1] for row in cur.fetchall()}
    if "email" not in cols:
        conn.execute("ALTER TABLE customers ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        conn.commit()

    biz_cur = conn.execute("PRAGMA table_info(businesses)")
    biz_cols = {row[1] for row in biz_cur.fetchall()}
    if "oauth_state" not in biz_cols:
        conn.execute("ALTER TABLE businesses ADD COLUMN oauth_state TEXT")
        conn.commit()

    exp_cur = conn.execute("PRAGMA table_info(expenses)")
    exp_cols = {row[1] for row in exp_cur.fetchall()}
    if "receipt_image" not in exp_cols:
        conn.execute("ALTER TABLE expenses ADD COLUMN receipt_image TEXT NOT NULL DEFAULT ''")
        conn.commit()

    # Follow-up columns on customers
    cust_cur = conn.execute("PRAGMA table_info(customers)")
    cust_cols = {row[1] for row in cust_cur.fetchall()}
    if "whatsapp_opted_in" not in cust_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN whatsapp_opted_in INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        # Re-read after migration
        cust_cur = conn.execute("PRAGMA table_info(customers)")
        cust_cols = {row[1] for row in cust_cur.fetchall()}
    if "followup_count" not in cust_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN followup_count INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE customers ADD COLUMN last_followup_at TEXT")
        conn.commit()

    # Follow-up settings on businesses
    if "followup_enabled" not in biz_cols:
        conn.execute("ALTER TABLE businesses ADD COLUMN followup_enabled INTEGER NOT NULL DEFAULT 1")
        conn.execute("ALTER TABLE businesses ADD COLUMN followup_interval_days INTEGER NOT NULL DEFAULT 3")
        conn.execute("ALTER TABLE businesses ADD COLUMN followup_max_count INTEGER NOT NULL DEFAULT 2")
        conn.execute("ALTER TABLE businesses ADD COLUMN followup_message TEXT NOT NULL DEFAULT 'Hi {first_name}, just a quick reminder — we''d really appreciate your feedback! It only takes a minute. Thank you 😊'")
        conn.commit()

    # Brand customisation
    if "brand_color" not in biz_cols:
        conn.execute("ALTER TABLE businesses ADD COLUMN brand_color TEXT NOT NULL DEFAULT '#16a34a'")
        conn.execute("ALTER TABLE businesses ADD COLUMN logo_url TEXT NOT NULL DEFAULT ''")
        conn.commit()

    # VAT registration flag
    if "vat_registered" not in biz_cols:
        conn.execute("ALTER TABLE businesses ADD COLUMN vat_registered INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # Per-business Twilio number
    if "twilio_number" not in biz_cols:
        conn.execute("ALTER TABLE businesses ADD COLUMN twilio_number TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE businesses ADD COLUMN twilio_number_sid TEXT NOT NULL DEFAULT ''")
        conn.commit()

    # Add sent_at to quotes (was missing)
    quo_cols = {r[1] for r in conn.execute("PRAGMA table_info(quotes)").fetchall()}
    if "sent_at" not in quo_cols:
        conn.execute("ALTER TABLE quotes ADD COLUMN sent_at TEXT")
        conn.commit()


@lru_cache
def _conn() -> sqlite3.Connection:
    return _get_conn()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for k in ("review_link_sent", "sent_to_owner"):
        if k in d:
            d[k] = bool(d[k])
    return d


# ── Supabase-compatible query builder ────────────────────────────

class _Result:
    """Mimics the Supabase response object."""
    def __init__(self, data: Any, count: int | None = None):
        self.data = data
        self.count = count


class _QueryBuilder:
    """Chainable query builder that mirrors the Supabase Python SDK."""

    def __init__(self, table: str):
        self._table = table
        self._op: str = "select"
        self._columns: str = "*"
        self._filters: list[tuple[str, str, Any]] = []
        self._order_col: str | None = None
        self._order_desc: bool = False
        self._limit_val: int | None = None
        self._single: bool = False
        self._count_mode: str | None = None
        self._payload: dict[str, Any] | None = None
        self._on_conflict: str | None = None
        self._join_table: str | None = None

    # ── Operation starters ───────────────────────────

    def select(self, columns: str = "*", *, count: str | None = None) -> "_QueryBuilder":
        self._op = "select"
        self._columns = columns
        self._count_mode = count
        # Detect join syntax like "*, businesses(*)"
        m = _re.search(r"(\w+)\(\*\)", columns)
        if m:
            self._join_table = m.group(1)
            self._columns = _re.sub(r",?\s*\w+\(\*\)", "", columns).strip().rstrip(",") or "*"
        return self

    def insert(self, payload: dict[str, Any]) -> "_QueryBuilder":
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload: dict[str, Any]) -> "_QueryBuilder":
        self._op = "update"
        self._payload = payload
        return self

    def upsert(self, payload: dict[str, Any], *, on_conflict: str | None = None) -> "_QueryBuilder":
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        return self

    def delete(self) -> "_QueryBuilder":
        self._op = "delete"
        return self

    # ── Filters / modifiers ──────────────────────────

    def eq(self, col: str, val: Any) -> "_QueryBuilder":
        self._filters.append(("eq", col, val))
        return self

    def neq(self, col: str, val: Any) -> "_QueryBuilder":
        self._filters.append(("neq", col, val))
        return self

    def lt(self, col: str, val: Any) -> "_QueryBuilder":
        self._filters.append(("lt", col, val))
        return self

    def lte(self, col: str, val: Any) -> "_QueryBuilder":
        self._filters.append(("lte", col, val))
        return self

    def gt(self, col: str, val: Any) -> "_QueryBuilder":
        self._filters.append(("gt", col, val))
        return self

    def gte(self, col: str, val: Any) -> "_QueryBuilder":
        self._filters.append(("gte", col, val))
        return self

    def is_(self, col: str, val: Any) -> "_QueryBuilder":
        self._filters.append(("is", col, val))
        return self

    def not_(self, col: str, op: str, val: Any) -> "_QueryBuilder":
        self._filters.append(("not_" + op, col, val))
        return self

    def order(self, col: str, *, desc: bool = False) -> "_QueryBuilder":
        self._order_col = col
        self._order_desc = desc
        return self

    def limit(self, n: int) -> "_QueryBuilder":
        self._limit_val = n
        return self

    def single(self) -> "_QueryBuilder":
        self._single = True
        self._limit_val = 1
        return self

    # ── Execution ────────────────────────────────────

    def execute(self) -> _Result:
        conn = _conn()
        if self._op == "select":
            return self._exec_select(conn)
        if self._op == "insert":
            return self._exec_insert(conn)
        if self._op == "update":
            return self._exec_update(conn)
        if self._op == "upsert":
            return self._exec_upsert(conn)
        if self._op == "delete":
            return self._exec_delete(conn)
        return _Result([])

    # ── Internals ────────────────────────────────────

    def _where_clause(self) -> tuple[str, list[Any]]:
        parts: list[str] = []
        vals: list[Any] = []
        for op, col, val in self._filters:
            if op == "eq":
                if val is None:
                    parts.append(f"{col} IS NULL")
                else:
                    parts.append(f"{col} = ?")
                    vals.append(val)
            elif op == "neq":
                if val is None:
                    parts.append(f"{col} IS NOT NULL")
                else:
                    parts.append(f"{col} != ?")
                    vals.append(val)
            elif op == "is":
                if val is None:
                    parts.append(f"{col} IS NULL")
                else:
                    parts.append(f"{col} IS ?")
                    vals.append(val)
            elif op == "lt":
                parts.append(f"{col} < ?")
                vals.append(val)
            elif op == "lte":
                parts.append(f"{col} <= ?")
                vals.append(val)
            elif op == "gt":
                parts.append(f"{col} > ?")
                vals.append(val)
            elif op == "gte":
                parts.append(f"{col} >= ?")
                vals.append(val)
            elif op == "not_is":
                if val is None:
                    parts.append(f"{col} IS NOT NULL")
                else:
                    parts.append(f"{col} IS NOT ?")
                    vals.append(val)
        where = " AND ".join(parts)
        return (f" WHERE {where}" if where else ""), vals

    def _order_clause(self) -> str:
        if not self._order_col:
            return ""
        return f" ORDER BY {self._order_col} {'DESC' if self._order_desc else 'ASC'}"

    def _limit_clause(self) -> str:
        return f" LIMIT {self._limit_val}" if self._limit_val else ""

    def _table_columns(self) -> set[str]:
        return {r[1] for r in _conn().execute(f"PRAGMA table_info({self._table})").fetchall()}

    # ── SELECT ───────────────────────────────────────

    def _exec_select(self, conn: sqlite3.Connection) -> _Result:
        sql = f"SELECT * FROM {self._table}"
        where, vals = self._where_clause()
        sql += where + self._order_clause() + self._limit_clause()

        rows = conn.execute(sql, vals).fetchall()
        data = [_row_to_dict(r) for r in rows]

        if self._join_table and data:
            for row in data:
                fk = row.get("business_id")
                if fk:
                    joined = conn.execute(
                        f"SELECT * FROM {self._join_table} WHERE id = ?", (fk,)
                    ).fetchone()
                    row[self._join_table] = _row_to_dict(joined) if joined else None

        count = None
        if self._count_mode == "exact":
            c_sql = f"SELECT COUNT(*) FROM {self._table}" + where
            count = conn.execute(c_sql, vals).fetchone()[0]

        if self._single:
            return _Result(data[0] if data else None, count)
        return _Result(data, count)

    # ── INSERT ───────────────────────────────────────

    def _exec_insert(self, conn: sqlite3.Connection) -> _Result:
        p = dict(self._payload)  # type: ignore[arg-type]
        if "id" not in p:
            p["id"] = str(uuid.uuid4())
        now = _now()
        p.setdefault("created_at", now)
        if "updated_at" in self._table_columns():
            p.setdefault("updated_at", now)
        for k, v in list(p.items()):
            if isinstance(v, bool):
                p[k] = int(v)

        cols = ", ".join(p.keys())
        phs = ", ".join("?" for _ in p)
        conn.execute(f"INSERT INTO {self._table} ({cols}) VALUES ({phs})", list(p.values()))
        conn.commit()
        return _Result([p])

    # ── UPDATE ───────────────────────────────────────

    def _exec_update(self, conn: sqlite3.Connection) -> _Result:
        p = dict(self._payload)  # type: ignore[arg-type]
        if "updated_at" in self._table_columns():
            p["updated_at"] = _now()
        for k, v in list(p.items()):
            if isinstance(v, bool):
                p[k] = int(v)

        set_clause = ", ".join(f"{k} = ?" for k in p)
        where, wvals = self._where_clause()
        conn.execute(f"UPDATE {self._table} SET {set_clause}{where}", list(p.values()) + wvals)
        conn.commit()

        rows = conn.execute(f"SELECT * FROM {self._table}{where}", wvals).fetchall()
        return _Result([_row_to_dict(r) for r in rows])

    # ── UPSERT ───────────────────────────────────────

    def _exec_upsert(self, conn: sqlite3.Connection) -> _Result:
        p = dict(self._payload)  # type: ignore[arg-type]
        if "id" not in p:
            p["id"] = str(uuid.uuid4())
        now = _now()
        p.setdefault("created_at", now)
        if "updated_at" in self._table_columns():
            p.setdefault("updated_at", now)
        for k, v in list(p.items()):
            if isinstance(v, bool):
                p[k] = int(v)
            if v == "now()":
                p[k] = now

        conflict_cols = self._on_conflict or "id"
        update_cols = [k for k in p if k not in conflict_cols.split(",") and k != "id"]
        update_set = ", ".join(f"{k} = excluded.{k}" for k in update_cols)

        cols = ", ".join(p.keys())
        phs = ", ".join("?" for _ in p)
        conn.execute(
            f"INSERT INTO {self._table} ({cols}) VALUES ({phs}) "
            f"ON CONFLICT({conflict_cols}) DO UPDATE SET {update_set}",
            list(p.values()),
        )
        conn.commit()
        return _Result([p])

    # ── DELETE ───────────────────────────────────────

    def _exec_delete(self, conn: sqlite3.Connection) -> _Result:
        where, vals = self._where_clause()
        rows = conn.execute(f"SELECT * FROM {self._table}{where}", vals).fetchall()
        conn.execute(f"DELETE FROM {self._table}{where}", vals)
        conn.commit()
        return _Result([_row_to_dict(r) for r in rows])


# ── Public interface (matches supabase.Client) ───────────────────

class _LocalClient:
    def table(self, name: str) -> _QueryBuilder:
        return _QueryBuilder(name)


@lru_cache
def get_supabase() -> _LocalClient:  # type: ignore[return]
    """Return a local SQLite client with the same API as the Supabase client."""
    return _LocalClient()
