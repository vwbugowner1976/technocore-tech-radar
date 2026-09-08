#!/usr/bin/env python3
"""SQLite persistence for TechnoScout v0.2."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable


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

CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    encounter_count INTEGER NOT NULL DEFAULT 0,
    useful_signal_count INTEGER NOT NULL DEFAULT 0,
    followup_count INTEGER NOT NULL DEFAULT 0,
    last_room TEXT NOT NULL DEFAULT '',
    last_summary TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_agents_useful
    ON agents(useful_signal_count DESC, encounter_count DESC, last_seen DESC);

CREATE TABLE IF NOT EXISTS agent_rooms (
    agent_id TEXT NOT NULL,
    room TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    encounter_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(agent_id, room)
);
CREATE INDEX IF NOT EXISTS idx_agent_rooms_room
    ON agent_rooms(room, encounter_count DESC);

CREATE TABLE IF NOT EXISTS agent_topics (
    agent_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(agent_id, topic)
);
CREATE INDEX IF NOT EXISTS idx_agent_topics_agent
    ON agent_topics(agent_id, hit_count DESC);
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


def record_agent_encounter(
    con: sqlite3.Connection,
    agent_id: str,
    room: str,
    seen_at: str,
    count: int = 1,
) -> None:
    if not agent_id or count <= 0:
        return
    agent_id = agent_id[:240]
    room = room[:80]
    con.execute(
        """
        INSERT INTO agents(agent_id,first_seen,last_seen,encounter_count,last_room)
        VALUES(?,?,?,?,?)
        ON CONFLICT(agent_id) DO UPDATE SET
          last_seen=excluded.last_seen,
          encounter_count=agents.encounter_count + excluded.encounter_count,
          last_room=excluded.last_room
        """,
        (agent_id, seen_at, seen_at, count, room),
    )
    con.execute(
        """
        INSERT INTO agent_rooms(agent_id,room,first_seen,last_seen,encounter_count)
        VALUES(?,?,?,?,?)
        ON CONFLICT(agent_id,room) DO UPDATE SET
          last_seen=excluded.last_seen,
          encounter_count=agent_rooms.encounter_count + excluded.encounter_count
        """,
        (agent_id, room, seen_at, seen_at, count),
    )


def record_agent_signal(
    con: sqlite3.Connection,
    agent_ids: Iterable[str],
    room: str,
    seen_at: str,
    tags: Iterable[str],
    summary: str,
    follow_up: bool,
) -> None:
    unique_agents = sorted({str(x)[:240] for x in agent_ids if str(x).strip()})
    clean_tags = sorted({str(x).strip().lower()[:80] for x in tags if str(x).strip()})
    for agent_id in unique_agents:
        # Signal evidence can mention an agent that was not previously counted.
        con.execute(
            """
            INSERT INTO agents(
              agent_id,first_seen,last_seen,encounter_count,useful_signal_count,
              followup_count,last_room,last_summary
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(agent_id) DO UPDATE SET
              last_seen=excluded.last_seen,
              useful_signal_count=agents.useful_signal_count + 1,
              followup_count=agents.followup_count + excluded.followup_count,
              last_room=excluded.last_room,
              last_summary=excluded.last_summary
            """,
            (
                agent_id,
                seen_at,
                seen_at,
                0,
                1,
                1 if follow_up else 0,
                room[:80],
                summary[:1000],
            ),
        )
        for tag in clean_tags:
            con.execute(
                """
                INSERT INTO agent_topics(agent_id,topic,first_seen,last_seen,hit_count)
                VALUES(?,?,?,?,1)
                ON CONFLICT(agent_id,topic) DO UPDATE SET
                  last_seen=excluded.last_seen,
                  hit_count=agent_topics.hit_count + 1
                """,
                (agent_id, tag, seen_at, seen_at),
            )


def top_agents(con: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT
          a.agent_id,
          a.encounter_count,
          a.useful_signal_count,
          a.followup_count,
          a.last_room,
          a.last_seen,
          COALESCE((
            SELECT GROUP_CONCAT(topic, ', ')
            FROM (
              SELECT topic
              FROM agent_topics t
              WHERE t.agent_id=a.agent_id
              ORDER BY hit_count DESC, last_seen DESC
              LIMIT 4
            )
          ), '') AS topics
        FROM agents a
        ORDER BY
          a.useful_signal_count DESC,
          a.followup_count DESC,
          a.encounter_count DESC,
          a.last_seen DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
