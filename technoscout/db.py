#!/usr/bin/env python3
"""SQLite persistence for TechnoScout."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
    room TEXT PRIMARY KEY,
    topic TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'unknown',
    state TEXT NOT NULL DEFAULT 'pending',
    relevance INTEGER,
    novelty INTEGER,
    technical INTEGER,
    people INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    last_seq INTEGER NOT NULL DEFAULT 0,
    triaged_at TEXT,
    watched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rooms_state_seen ON rooms(state, last_seen DESC);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    room TEXT NOT NULL,
    from_seq INTEGER NOT NULL,
    through_seq INTEGER NOT NULL,
    relevance INTEGER,
    novelty INTEGER,
    technical INTEGER,
    people INTEGER,
    action TEXT NOT NULL,
    summary TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_room_time ON observations(room, observed_at DESC);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def get_meta(con: sqlite3.Connection, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_meta(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
