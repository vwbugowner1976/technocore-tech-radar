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

CREATE TABLE IF NOT EXISTS reply_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    room TEXT NOT NULL,
    through_seq INTEGER NOT NULL,
    target_agent TEXT NOT NULL DEFAULT '',
    relationship_score INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    draft_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    UNIQUE(room, through_seq)
);
CREATE INDEX IF NOT EXISTS idx_reply_drafts_status
    ON reply_drafts(status, created_at DESC);
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



def agent_context(
    con: sqlite3.Connection,
    agent_ids: Iterable[str],
    limit: int = 4,
) -> list[dict[str, Any]]:
    ids = sorted({str(x)[:240] for x in agent_ids if str(x).strip()})
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = con.execute(
        f"""
        SELECT agent_id, encounter_count, useful_signal_count, followup_count, last_room
        FROM agents
        WHERE agent_id IN ({placeholders})
        ORDER BY useful_signal_count DESC, followup_count DESC, encounter_count DESC
        LIMIT ?
        """,
        (*ids, max(1, int(limit))),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        topics = [
            str(item["topic"])
            for item in con.execute(
                """
                SELECT topic FROM agent_topics
                WHERE agent_id=?
                ORDER BY hit_count DESC, last_seen DESC
                LIMIT 3
                """,
                (row["agent_id"],),
            ).fetchall()
        ]
        result.append(
            {
                "id": str(row["agent_id"])[:120],
                "encounters": int(row["encounter_count"]),
                "signals": int(row["useful_signal_count"]),
                "followups": int(row["followup_count"]),
                "last_room": str(row["last_room"])[:60],
                "topics": topics,
            }
        )
    return result



def relationship_score(
    encounter_count: int,
    useful_signal_count: int,
    followup_count: int,
) -> int:
    score = (
        min(20, max(0, int(encounter_count)) * 2)
        + min(50, max(0, int(useful_signal_count)) * 20)
        + min(30, max(0, int(followup_count)) * 15)
    )
    return max(0, min(100, score))


def agent_relationship(con: sqlite3.Connection, agent_id: str) -> dict[str, Any]:
    row = con.execute(
        """
        SELECT agent_id, encounter_count, useful_signal_count, followup_count,
               last_room, last_summary
        FROM agents WHERE agent_id=?
        """,
        (agent_id[:240],),
    ).fetchone()
    if not row:
        return {
            "agent_id": agent_id[:240],
            "score": 0,
            "encounters": 0,
            "signals": 0,
            "followups": 0,
            "last_room": "",
            "last_summary": "",
        }
    return {
        "agent_id": str(row["agent_id"]),
        "score": relationship_score(
            row["encounter_count"],
            row["useful_signal_count"],
            row["followup_count"],
        ),
        "encounters": int(row["encounter_count"]),
        "signals": int(row["useful_signal_count"]),
        "followups": int(row["followup_count"]),
        "last_room": str(row["last_room"]),
        "last_summary": str(row["last_summary"]),
    }


def create_reply_draft(
    con: sqlite3.Connection,
    created_at: str,
    room: str,
    through_seq: int,
    target_agent: str,
    relationship: int,
    reason: str,
    draft_text: str,
) -> bool:
    cur = con.execute(
        """
        INSERT OR IGNORE INTO reply_drafts(
          created_at,room,through_seq,target_agent,relationship_score,reason,draft_text,status
        ) VALUES(?,?,?,?,?,?,?,'pending')
        """,
        (
            created_at,
            room[:80],
            int(through_seq),
            target_agent[:240],
            max(0, min(100, int(relationship))),
            reason[:1000],
            draft_text[:2000],
        ),
    )
    return cur.rowcount > 0


def pending_reply_drafts(con: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT id, created_at, room, through_seq, target_agent,
               relationship_score, reason, draft_text, status
        FROM reply_drafts
        WHERE status='pending'
        ORDER BY id DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
