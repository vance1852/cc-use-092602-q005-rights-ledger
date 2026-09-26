"""家庭土地权益额度账本的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS households (
    household_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    village TEXT NOT NULL,
    member_count INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('clerk','manager','reviewer','auditor','household')),
    household_id TEXT REFERENCES households(household_id),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_versions (
    rule_version INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_year INTEGER NOT NULL UNIQUE,
    selection_strategy TEXT NOT NULL
        CHECK(selection_strategy IN ('earliest_expiry_first','source_priority')),
    review_window_hours INTEGER NOT NULL,
    max_overshoot_percent TEXT NOT NULL,
    source_priority_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settled_years (
    year INTEGER PRIMARY KEY,
    settled_by TEXT NOT NULL REFERENCES users(user_id),
    settled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entitlements (
    entitlement_id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(household_id),
    source TEXT NOT NULL
        CHECK(source IN ('contract-merge','homestead-eligibility','relocation-reward',
                         'policy-adjustment','exception-grant')),
    land_categories_json TEXT NOT NULL,
    granted_mu TEXT NOT NULL,
    frozen_mu TEXT NOT NULL DEFAULT '0',
    consumed_mu TEXT NOT NULL DEFAULT '0',
    expired_mu TEXT NOT NULL DEFAULT '0',
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    rule_version INTEGER NOT NULL REFERENCES rule_versions(rule_version),
    exception_id TEXT,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','exhausted','expired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    CHECK(valid_until >= valid_from)
);

CREATE INDEX IF NOT EXISTS idx_entitlements_household
ON entitlements(household_id, state, valid_until);

CREATE TABLE IF NOT EXISTS plots (
    plot_id TEXT PRIMARY KEY,
    village TEXT NOT NULL,
    land_category TEXT NOT NULL,
    area_mu TEXT NOT NULL,
    reserved_mu TEXT NOT NULL DEFAULT '0',
    state TEXT NOT NULL DEFAULT 'available' CHECK(state IN ('available','closed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    application_id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(household_id),
    land_category TEXT NOT NULL,
    requested_mu TEXT NOT NULL,
    starts_on TEXT NOT NULL,
    ends_on TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'under-review'
        CHECK(state IN ('accepted','under-review','rejected','reserved',
                        'delivering','completed','exited','failed')),
    rule_version INTEGER NOT NULL REFERENCES rule_versions(rule_version),
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES users(user_id),
    submitted_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    CHECK(ends_on >= starts_on)
);

CREATE INDEX IF NOT EXISTS idx_applications_household
ON applications(household_id, state, submitted_at);

CREATE TABLE IF NOT EXISTS application_slices (
    slice_id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    year INTEGER NOT NULL,
    segment_start TEXT NOT NULL,
    segment_end TEXT NOT NULL,
    days INTEGER NOT NULL,
    planned_mu TEXT NOT NULL,
    delivered_mu TEXT NOT NULL DEFAULT '0',
    UNIQUE(application_id, year)
);

CREATE TABLE IF NOT EXISTS exceptions (
    exception_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL UNIQUE REFERENCES applications(application_id),
    household_id TEXT NOT NULL REFERENCES households(household_id),
    overshoot_mu TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','approved','rejected','expired','cancelled')),
    submitted_by TEXT NOT NULL REFERENCES users(user_id),
    submitted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_by TEXT REFERENCES users(user_id),
    decided_at TEXT,
    decision_reason TEXT,
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_exceptions_queue
ON exceptions(state, expires_at);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL UNIQUE REFERENCES applications(application_id),
    plot_id TEXT NOT NULL REFERENCES plots(plot_id),
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','closed')),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservation_lines (
    line_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
    year INTEGER NOT NULL,
    amount_mu TEXT NOT NULL,
    consumed_mu TEXT NOT NULL DEFAULT '0'
);

CREATE INDEX IF NOT EXISTS idx_reservation_lines
ON reservation_lines(reservation_id, year);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    year INTEGER NOT NULL,
    amount_mu TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id TEXT NOT NULL REFERENCES households(household_id),
    entitlement_id TEXT NOT NULL REFERENCES entitlements(entitlement_id),
    kind TEXT NOT NULL CHECK(kind IN ('grant','freeze','consume','return','expire')),
    amount_mu TEXT NOT NULL,
    year INTEGER NOT NULL,
    balance_after_mu TEXT NOT NULL,
    application_id TEXT REFERENCES applications(application_id),
    reservation_id TEXT REFERENCES reservations(reservation_id),
    exception_id TEXT REFERENCES exceptions(exception_id),
    rule_version INTEGER NOT NULL REFERENCES rule_versions(rule_version),
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_entries_household
ON ledger_entries(household_id, entry_id);

CREATE INDEX IF NOT EXISTS idx_ledger_entries_entitlement
ON ledger_entries(entitlement_id, entry_id);

CREATE INDEX IF NOT EXISTS idx_ledger_entries_application
ON ledger_entries(application_id, entry_id);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS audit_events (
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

CREATE INDEX IF NOT EXISTS idx_audit_events_entity
ON audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
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


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
