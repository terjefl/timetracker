"""SQLite-lagring. Én fil, skjema versjonert med PRAGMA user_version."""

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("TIMETRACKER_DB", "/data/timetracker.db")

SCHEMA_V1 = """
CREATE TABLE customers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL DEFAULT '',
    email       TEXT NOT NULL DEFAULT '',
    address     TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Timepristyper per kunde, f.eks. «Standard» og «Helg».
CREATE TABLE rate_types (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    hourly_rate  REAL NOT NULL DEFAULT 0,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE invoices (
    id           INTEGER PRIMARY KEY,
    number       INTEGER NOT NULL UNIQUE,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    status       TEXT NOT NULL DEFAULT 'kladd',   -- kladd | sendt | betalt
    invoice_date TEXT NOT NULL,
    due_date     TEXT NOT NULL,
    period_from  TEXT,
    period_to    TEXT,
    note         TEXT NOT NULL DEFAULT '',
    sent_at      TEXT,
    paid_at      TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Timepris og typenavn kopieres inn ved registrering, slik at senere
-- prisendringer ikke endrer historikk eller sendte fakturaer.
CREATE TABLE entries (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    rate_type_id INTEGER REFERENCES rate_types(id) ON DELETE SET NULL,
    rate_name    TEXT NOT NULL DEFAULT '',
    hourly_rate  REAL NOT NULL DEFAULT 0,
    work_date    TEXT NOT NULL,
    hours        REAL NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    invoice_id   INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX entries_customer_date ON entries(customer_id, work_date);
CREATE INDEX entries_invoice ON entries(invoice_id);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MIGRATIONS = [SCHEMA_V1]

DEFAULT_SETTINGS = {
    "sender_name": "",
    "sender_address": "",
    "sender_phone": "",
    "sender_email": "",
    "account_number": "",
    "payment_days": "14",
    "invoice_footer": "Takk for oppdraget!",
    "next_invoice_number": "1",
}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {i}")
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
            )


def get_settings(conn) -> dict:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
