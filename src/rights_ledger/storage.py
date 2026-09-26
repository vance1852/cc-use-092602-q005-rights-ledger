"""权益额度账本服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ledger_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('clerk','manager','reviewer','auditor','household')),
    household_id TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS households (
    household_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    village TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','closed')),
    created_by TEXT NOT NULL REFERENCES ledger_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plots (
    plot_id TEXT PRIMARY KEY,
    village TEXT NOT NULL,
    category TEXT NOT NULL,
    area_mu TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'available' CHECK(state IN ('available','reserved','allocated')),
    created_by TEXT NOT NULL REFERENCES ledger_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS allocation_rules (
    rule_id TEXT PRIMARY KEY,
    effective_year INTEGER NOT NULL,
    selection_policy TEXT NOT NULL CHECK(selection_policy IN ('expiry_first','source_priority')),
    source_priority_json TEXT NOT NULL,
    review_deadline_days INTEGER NOT NULL,
    created_by TEXT NOT NULL REFERENCES ledger_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rules_year
ON allocation_rules(effective_year, rule_id);

CREATE TABLE IF NOT EXISTS entitlement_grants (
    grant_id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(household_id),
    source TEXT NOT NULL,
    category TEXT NOT NULL,
    quantity_mu TEXT NOT NULL,
    consumed_mu TEXT NOT NULL DEFAULT '0',
    frozen_mu TEXT NOT NULL DEFAULT '0',
    expired_mu TEXT NOT NULL DEFAULT '0',
    effective_from TEXT NOT NULL,
    expires_at TEXT,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','exhausted','expired','revoked')),
    exception_id TEXT,
    reason TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES ledger_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_grants_household
ON entitlement_grants(household_id, category, state);

CREATE TABLE IF NOT EXISTS applications (
    application_id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(household_id),
    plot_id TEXT NOT NULL REFERENCES plots(plot_id),
    category TEXT NOT NULL,
    requested_mu TEXT NOT NULL,
    project_start TEXT NOT NULL,
    project_end TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','pending_review','confirmed','rejected','expired','cancelled')),
    rule_id TEXT NOT NULL REFERENCES allocation_rules(rule_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES ledger_users(user_id),
    submitted_at TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS exceptions (
    exception_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL UNIQUE REFERENCES applications(application_id),
    household_id TEXT NOT NULL REFERENCES households(household_id),
    category TEXT NOT NULL,
    requested_mu TEXT NOT NULL,
    available_mu TEXT NOT NULL,
    over_mu TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','approved','rejected','expired')),
    rule_id TEXT NOT NULL REFERENCES allocation_rules(rule_id),
    submitted_by TEXT NOT NULL REFERENCES ledger_users(user_id),
    submitted_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    decided_by TEXT REFERENCES ledger_users(user_id),
    decided_at TEXT,
    decision_reason TEXT,
    grant_id TEXT
);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL UNIQUE REFERENCES applications(application_id),
    household_id TEXT NOT NULL REFERENCES households(household_id),
    plot_id TEXT NOT NULL REFERENCES plots(plot_id),
    category TEXT NOT NULL,
    total_mu TEXT NOT NULL,
    delivered_mu TEXT NOT NULL DEFAULT '0',
    state TEXT NOT NULL DEFAULT 'reserved'
        CHECK(state IN ('reserved','in_delivery','completed','exited','failed')),
    rule_id TEXT NOT NULL REFERENCES allocation_rules(rule_id),
    created_by TEXT NOT NULL REFERENCES ledger_users(user_id),
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS reservation_years (
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    year INTEGER NOT NULL,
    planned_mu TEXT NOT NULL,
    delivered_mu TEXT NOT NULL DEFAULT '0',
    settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0,1)),
    settled_at TEXT,
    PRIMARY KEY(reservation_id, year)
);

CREATE TABLE IF NOT EXISTS freezes (
    freeze_id INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id TEXT NOT NULL REFERENCES households(household_id),
    grant_id TEXT NOT NULL REFERENCES entitlement_grants(grant_id),
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    reservation_id TEXT REFERENCES reservations(reservation_id),
    amount_mu TEXT NOT NULL,
    consumed_mu TEXT NOT NULL DEFAULT '0',
    released_mu TEXT NOT NULL DEFAULT '0',
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','closed')),
    created_by TEXT NOT NULL REFERENCES ledger_users(user_id),
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_freezes_application
ON freezes(application_id, state);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    year INTEGER NOT NULL,
    amount_mu TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES ledger_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settled_years (
    year INTEGER PRIMARY KEY,
    reservation_count INTEGER NOT NULL,
    planned_mu TEXT NOT NULL,
    delivered_mu TEXT NOT NULL,
    settled_by TEXT NOT NULL REFERENCES ledger_users(user_id),
    settled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id TEXT NOT NULL REFERENCES households(household_id),
    grant_id TEXT,
    kind TEXT NOT NULL
        CHECK(kind IN ('grant','freeze','unfreeze','consume','return','expire','reserve')),
    amount_mu TEXT NOT NULL,
    application_id TEXT,
    reservation_id TEXT,
    exception_id TEXT,
    delivery_id TEXT,
    rule_id TEXT,
    reason TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'household' CHECK(visibility IN ('household','internal')),
    actor_id TEXT NOT NULL REFERENCES ledger_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_household
ON ledger_entries(household_id, entry_id);

CREATE TABLE IF NOT EXISTS ledger_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS ledger_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_audit_entity
ON ledger_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # HTTP 服务以线程处理请求，连接允许跨线程使用；请求级串行化由 api 层的锁保证。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
