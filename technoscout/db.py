#!/usr/bin/env python3
"""SQLite persistence for TechnoScout v0.8."""

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

CREATE TABLE IF NOT EXISTS send_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    attempted_at TEXT NOT NULL,
    did TEXT NOT NULL,
    room TEXT NOT NULL,
    nonce TEXT NOT NULL,
    sig TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_send_attempts_draft
    ON send_attempts(draft_id, id DESC);

CREATE TABLE IF NOT EXISTS send_permits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    did TEXT NOT NULL,
    room TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'armed',
    consumed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_send_permits_draft
    ON send_permits(draft_id, id DESC);

CREATE TABLE IF NOT EXISTS translations (
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    language TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(source_type, source_key, language)
);
CREATE INDEX IF NOT EXISTS idx_translations_language
    ON translations(language, source_type);

CREATE TABLE IF NOT EXISTS autonomy_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    decided_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    reason TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'none'
);
CREATE INDEX IF NOT EXISTS idx_autonomy_draft
    ON autonomy_decisions(draft_id, id DESC);

CREATE TABLE IF NOT EXISTS reaction_memory (
    send_attempt_id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL,
    first_checked_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    check_count INTEGER NOT NULL DEFAULT 1,
    room TEXT NOT NULL,
    our_seq INTEGER NOT NULL,
    target_agent TEXT NOT NULL DEFAULT '',
    classification TEXT NOT NULL,
    coverage TEXT NOT NULL,
    responder_did TEXT NOT NULL DEFAULT '',
    responder_seq INTEGER,
    overlap INTEGER NOT NULL DEFAULT 0,
    foreign_posts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reaction_memory_class
    ON reaction_memory(classification, last_checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_reaction_memory_responder
    ON reaction_memory(responder_did, classification);
CREATE INDEX IF NOT EXISTS idx_reaction_memory_target
    ON reaction_memory(target_agent, classification);

CREATE TABLE IF NOT EXISTS collaboration_shadow_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    room TEXT NOT NULL,
    through_seq INTEGER NOT NULL,
    marker TEXT NOT NULL,
    actual_agent TEXT NOT NULL,
    shadow_agent TEXT NOT NULL,
    actual_relationship INTEGER NOT NULL DEFAULT 0,
    shadow_relationship INTEGER NOT NULL DEFAULT 0,
    shadow_collaboration INTEGER NOT NULL DEFAULT 0,
    shadow_combined INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    responder_direct INTEGER NOT NULL DEFAULT 0,
    responder_likely INTEGER NOT NULL DEFAULT 0,
    target_direct INTEGER NOT NULL DEFAULT 0,
    target_likely INTEGER NOT NULL DEFAULT 0,
    UNIQUE(room, through_seq)
);
CREATE INDEX IF NOT EXISTS idx_collab_shadow_marker
    ON collaboration_shadow_decisions(marker, observed_at DESC);
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



def reply_drafts_by_status(
    con: sqlite3.Connection,
    status: str,
    limit: int = 20,
) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT id, created_at, room, through_seq, target_agent,
               relationship_score, reason, draft_text, status
        FROM reply_drafts
        WHERE status=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (str(status), max(1, int(limit))),
    ).fetchall()


def mark_pending_draft_status(
    con: sqlite3.Connection,
    draft_id: int,
    status: str,
) -> bool:
    normalized = str(status).strip().lower()
    if normalized not in {"autonomy_blocked", "superseded"}:
        raise ValueError("unsupported pending draft status")
    cur = con.execute(
        "UPDATE reply_drafts SET status=? WHERE id=? AND status='pending'",
        (normalized, int(draft_id)),
    )
    return cur.rowcount > 0


def supersede_older_pending_drafts(
    con: sqlite3.Connection,
    room: str,
    target_agent: str,
    keep_draft_id: int,
) -> int:
    cur = con.execute(
        """
        UPDATE reply_drafts
        SET status='superseded'
        WHERE status='pending'
          AND room=?
          AND target_agent=?
          AND id<?
        """,
        (str(room), str(target_agent), int(keep_draft_id)),
    )
    return int(cur.rowcount)


def get_reply_draft(con: sqlite3.Connection, draft_id: int) -> sqlite3.Row | None:
    return con.execute(
        """
        SELECT id, created_at, room, through_seq, target_agent,
               relationship_score, reason, draft_text, status
        FROM reply_drafts
        WHERE id=?
        """,
        (int(draft_id),),
    ).fetchone()


def review_reply_draft(
    con: sqlite3.Connection,
    draft_id: int,
    status: str,
) -> bool:
    normalized = str(status).strip().lower()
    if normalized not in {"approved", "rejected"}:
        raise ValueError("draft status must be approved or rejected")
    cur = con.execute(
        """
        UPDATE reply_drafts
        SET status=?
        WHERE id=? AND status='pending'
        """,
        (normalized, int(draft_id)),
    )
    return cur.rowcount > 0


def reply_draft_counts(con: sqlite3.Connection) -> dict[str, int]:
    rows = con.execute(
        "SELECT status, COUNT(*) n FROM reply_drafts GROUP BY status"
    ).fetchall()
    result = {
        "pending": 0,
        "approved": 0,
        "rejected": 0,
        "sent": 0,
        "send_uncertain": 0,
        "send_blocked": 0,
        "autonomy_blocked": 0,
        "superseded": 0,
    }
    for row in rows:
        result[str(row["status"])] = int(row["n"])
    return result



def set_draft_status(
    con: sqlite3.Connection,
    draft_id: int,
    status: str,
) -> None:
    cur = con.execute(
        "UPDATE reply_drafts SET status=? WHERE id=?",
        (str(status), int(draft_id)),
    )
    if cur.rowcount != 1:
        raise ValueError(f"draft #{draft_id} not found")


def reserve_send_nonce(
    con: sqlite3.Connection,
    did: str,
    room: str,
    server_nonce: int = 0,
    floor: int = 0,
) -> int:
    key = f"send_nonce:{did}:{room}"
    local = int(get_meta(con, key, "0") or 0)
    value = max(
        int(floor),
        local + 1,
        max(0, int(server_nonce)) + 1,
        1,
    )
    if value >= 10**19:
        raise ValueError("nonce exceeds Technocore 19-digit limit")
    set_meta(con, key, value)
    return value


def create_send_attempt(
    con: sqlite3.Connection,
    draft_id: int,
    attempted_at: str,
    did: str,
    room: str,
    nonce: int,
    signature: str,
    text: str,
) -> int:
    cur = con.execute(
        """
        INSERT INTO send_attempts(
          draft_id,attempted_at,did,room,nonce,sig,text,status
        ) VALUES(?,?,?,?,?,?,?,'reserved')
        """,
        (
            int(draft_id),
            attempted_at,
            did[:240],
            room[:80],
            str(int(nonce)),
            signature[:120],
            text[:4096],
        ),
    )
    return int(cur.lastrowid)


def finish_send_attempt(
    con: sqlite3.Connection,
    attempt_id: int,
    status: str,
    http_status: int | None,
    detail: str,
) -> None:
    cur = con.execute(
        """
        UPDATE send_attempts
        SET status=?, http_status=?, detail=?
        WHERE id=?
        """,
        (
            str(status),
            int(http_status) if http_status is not None else None,
            str(detail)[:2000],
            int(attempt_id),
        ),
    )
    if cur.rowcount != 1:
        raise ValueError(f"send attempt #{attempt_id} not found")


def get_last_send_attempt(
    con: sqlite3.Connection,
    draft_id: int,
) -> sqlite3.Row | None:
    return con.execute(
        """
        SELECT id,draft_id,attempted_at,did,room,nonce,sig,text,status,
               http_status,detail
        FROM send_attempts
        WHERE draft_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(draft_id),),
    ).fetchone()


def send_attempts_for_draft(
    con: sqlite3.Connection,
    draft_id: int,
    limit: int = 10,
) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT id,draft_id,attempted_at,did,room,nonce,sig,text,status,
               http_status,detail
        FROM send_attempts
        WHERE draft_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(draft_id), max(1, int(limit))),
    ).fetchall()



def create_send_permit(
    con: sqlite3.Connection,
    draft_id: int,
    created_at: float,
    expires_at: float,
    token_hash: str,
    did: str,
    room: str,
    text_hash: str,
) -> int:
    # Arming a new permit invalidates any older still-armed permit for the draft.
    con.execute(
        """
        UPDATE send_permits
        SET status='superseded'
        WHERE draft_id=? AND status='armed'
        """,
        (int(draft_id),),
    )
    cur = con.execute(
        """
        INSERT INTO send_permits(
          draft_id,created_at,expires_at,token_hash,did,room,text_hash,status
        ) VALUES(?,?,?,?,?,?,?,'armed')
        """,
        (
            int(draft_id),
            float(created_at),
            float(expires_at),
            str(token_hash),
            str(did)[:240],
            str(room)[:80],
            str(text_hash),
        ),
    )
    return int(cur.lastrowid)


def consume_send_permit(
    con: sqlite3.Connection,
    draft_id: int,
    token_hash: str,
    did: str,
    room: str,
    text_hash: str,
    now: float,
) -> bool:
    cur = con.execute(
        """
        UPDATE send_permits
        SET status='consumed', consumed_at=?
        WHERE draft_id=?
          AND token_hash=?
          AND did=?
          AND room=?
          AND text_hash=?
          AND status='armed'
          AND expires_at>=?
        """,
        (
            float(now),
            int(draft_id),
            str(token_hash),
            str(did)[:240],
            str(room)[:80],
            str(text_hash),
            float(now),
        ),
    )
    return cur.rowcount == 1


def expire_send_permits(con: sqlite3.Connection, now: float) -> int:
    cur = con.execute(
        """
        UPDATE send_permits
        SET status='expired'
        WHERE status='armed' AND expires_at<?
        """,
        (float(now),),
    )
    return int(cur.rowcount)


def send_permits_for_draft(
    con: sqlite3.Connection,
    draft_id: int,
    limit: int = 10,
) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT id,draft_id,created_at,expires_at,did,room,text_hash,status,consumed_at
        FROM send_permits
        WHERE draft_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(draft_id), max(1, int(limit))),
    ).fetchall()



def revoke_send_permits(
    con: sqlite3.Connection,
    draft_id: int,
) -> int:
    cur = con.execute(
        """
        UPDATE send_permits
        SET status='revoked'
        WHERE draft_id=? AND status='armed'
        """,
        (int(draft_id),),
    )
    return int(cur.rowcount)



def get_translation(
    con: sqlite3.Connection,
    source_type: str,
    source_key: str,
    language: str,
    source_hash: str,
) -> str | None:
    row = con.execute(
        """
        SELECT translated_text, source_hash
        FROM translations
        WHERE source_type=? AND source_key=? AND language=?
        """,
        (str(source_type), str(source_key), str(language)),
    ).fetchone()
    if not row or str(row["source_hash"]) != str(source_hash):
        return None
    return str(row["translated_text"])


def store_translation(
    con: sqlite3.Connection,
    source_type: str,
    source_key: str,
    language: str,
    source_hash: str,
    translated_text: str,
    created_at: str,
) -> None:
    con.execute(
        """
        INSERT INTO translations(
          source_type,source_key,language,source_hash,translated_text,created_at
        ) VALUES(?,?,?,?,?,?)
        ON CONFLICT(source_type,source_key,language) DO UPDATE SET
          source_hash=excluded.source_hash,
          translated_text=excluded.translated_text,
          created_at=excluded.created_at
        """,
        (
            str(source_type),
            str(source_key),
            str(language),
            str(source_hash),
            str(translated_text)[:6000],
            str(created_at),
        ),
    )


def record_autonomy_decision(
    con: sqlite3.Connection,
    draft_id: int,
    decided_at: str,
    mode: str,
    allowed: bool,
    reason: str,
    outcome: str = "none",
) -> int:
    cur = con.execute(
        """
        INSERT INTO autonomy_decisions(
          draft_id,decided_at,mode,allowed,reason,outcome
        ) VALUES(?,?,?,?,?,?)
        """,
        (
            int(draft_id),
            str(decided_at),
            str(mode),
            1 if allowed else 0,
            str(reason)[:1000],
            str(outcome)[:80],
        ),
    )
    return int(cur.lastrowid)


def update_autonomy_outcome(
    con: sqlite3.Connection,
    decision_id: int,
    outcome: str,
) -> None:
    con.execute(
        "UPDATE autonomy_decisions SET outcome=? WHERE id=?",
        (str(outcome)[:80], int(decision_id)),
    )


def autonomy_decisions_for_draft(
    con: sqlite3.Connection,
    draft_id: int,
    limit: int = 10,
) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT id,draft_id,decided_at,mode,allowed,reason,outcome
        FROM autonomy_decisions
        WHERE draft_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(draft_id), max(1, int(limit))),
    ).fetchall()


def recent_sent_count(
    con: sqlite3.Connection,
    since_iso: str,
) -> int:
    row = con.execute(
        """
        SELECT COUNT(*) n
        FROM send_attempts
        WHERE status='sent' AND attempted_at>=?
        """,
        (str(since_iso),),
    ).fetchone()
    return int(row["n"] if row else 0)


def last_sent_at_for_room(
    con: sqlite3.Connection,
    room: str,
) -> str:
    row = con.execute(
        """
        SELECT attempted_at
        FROM send_attempts
        WHERE status='sent' AND room=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(room),),
    ).fetchone()
    return str(row["attempted_at"]) if row else ""


def last_sent_at_for_agent(
    con: sqlite3.Connection,
    agent_id: str,
) -> str:
    row = con.execute(
        """
        SELECT s.attempted_at
        FROM send_attempts s
        JOIN reply_drafts d ON d.id=s.draft_id
        WHERE s.status='sent' AND d.target_agent=?
        ORDER BY s.id DESC
        LIMIT 1
        """,
        (str(agent_id),),
    ).fetchone()
    return str(row["attempted_at"]) if row else ""


def get_reply_draft_by_room_seq(
    con: sqlite3.Connection,
    room: str,
    through_seq: int,
) -> sqlite3.Row | None:
    return con.execute(
        """
        SELECT id, created_at, room, through_seq, target_agent,
               relationship_score, reason, draft_text, status
        FROM reply_drafts
        WHERE room=? AND through_seq=?
        """,
        (str(room), int(through_seq)),
    ).fetchone()


def recent_observations(
    con: sqlite3.Connection,
    limit: int = 10,
) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT id,observed_at,room,relevance,technical,people,action,summary,tags_json
        FROM observations
        ORDER BY id DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()



def get_autonomy_halt(con: sqlite3.Connection) -> str:
    return get_meta(con, "autonomy_halt", "")


def set_autonomy_halt(con: sqlite3.Connection, reason: str) -> None:
    set_meta(con, "autonomy_halt", str(reason)[:2000])


def clear_autonomy_halt(con: sqlite3.Connection) -> None:
    con.execute("DELETE FROM meta WHERE key='autonomy_halt'")


REACTION_CLASS_RANK = {
    "READ_ERROR": 0,
    "WINDOW_TRUNCATED": 1,
    "NO_REACTION": 2,
    "ROOM_ACTIVITY": 3,
    "LIKELY_REACTION": 4,
    "DIRECT_REPLY": 5,
}


def get_reaction_memory(
    con: sqlite3.Connection,
    send_attempt_id: int,
) -> sqlite3.Row | None:
    return con.execute(
        """
        SELECT send_attempt_id,draft_id,first_checked_at,last_checked_at,
               check_count,room,our_seq,target_agent,classification,coverage,
               responder_did,responder_seq,overlap,foreign_posts
        FROM reaction_memory
        WHERE send_attempt_id=?
        """,
        (int(send_attempt_id),),
    ).fetchone()


def upsert_reaction_memory(
    con: sqlite3.Connection,
    *,
    send_attempt_id: int,
    draft_id: int,
    checked_at: str,
    room: str,
    our_seq: int,
    target_agent: str,
    classification: str,
    coverage: str,
    responder_did: str = "",
    responder_seq: int | None = None,
    overlap: int = 0,
    foreign_posts: int = 0,
) -> str:
    """Persist canonical reaction evidence without storing raw message text."""
    classification = str(classification).upper()
    if classification not in REACTION_CLASS_RANK:
        raise ValueError(f"invalid reaction classification: {classification}")
    coverage = str(coverage).upper()
    if coverage not in {"OBSERVED", "PARTIAL", "ERROR"}:
        raise ValueError(f"invalid reaction coverage: {coverage}")

    existing = get_reaction_memory(con, send_attempt_id)
    if existing is None:
        con.execute(
            """
            INSERT INTO reaction_memory(
              send_attempt_id,draft_id,first_checked_at,last_checked_at,
              check_count,room,our_seq,target_agent,classification,coverage,
              responder_did,responder_seq,overlap,foreign_posts
            ) VALUES(?,?,?,?,1,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(send_attempt_id),
                int(draft_id),
                str(checked_at),
                str(checked_at),
                str(room)[:80],
                int(our_seq),
                str(target_agent)[:240],
                classification,
                coverage,
                str(responder_did)[:240],
                int(responder_seq) if responder_seq is not None else None,
                max(0, int(overlap)),
                max(0, int(foreign_posts)),
            ),
        )
        return "inserted"

    old_class = str(existing["classification"])
    old_coverage = str(existing["coverage"])
    preserve = False

    # Never lose a previously observed stronger reaction merely because a busy
    # room later becomes truncated or the old reply leaves the fetch window.
    if old_coverage == "OBSERVED" and coverage != "OBSERVED":
        preserve = True
    elif REACTION_CLASS_RANK.get(old_class, 0) > REACTION_CLASS_RANK[classification]:
        preserve = True

    if preserve:
        con.execute(
            """
            UPDATE reaction_memory
            SET last_checked_at=?, check_count=check_count+1,
                foreign_posts=MAX(foreign_posts, ?)
            WHERE send_attempt_id=?
            """,
            (
                str(checked_at),
                max(0, int(foreign_posts)),
                int(send_attempt_id),
            ),
        )
        return "preserved"

    con.execute(
        """
        UPDATE reaction_memory
        SET last_checked_at=?, check_count=check_count+1,
            draft_id=?,room=?,our_seq=?,target_agent=?,
            classification=?,coverage=?,responder_did=?,responder_seq=?,
            overlap=?,foreign_posts=?
        WHERE send_attempt_id=?
        """,
        (
            str(checked_at),
            int(draft_id),
            str(room)[:80],
            int(our_seq),
            str(target_agent)[:240],
            classification,
            coverage,
            str(responder_did)[:240],
            int(responder_seq) if responder_seq is not None else None,
            max(0, int(overlap)),
            max(0, int(foreign_posts)),
            int(send_attempt_id),
        ),
    )
    return "updated"


def reaction_memory_rows(
    con: sqlite3.Connection,
    limit: int = 100,
) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT send_attempt_id,draft_id,first_checked_at,last_checked_at,
               check_count,room,our_seq,target_agent,classification,coverage,
               responder_did,responder_seq,overlap,foreign_posts
        FROM reaction_memory
        ORDER BY send_attempt_id DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()


def reaction_memory_counts(con: sqlite3.Connection) -> dict[str, int]:
    result = {
        "DIRECT_REPLY": 0,
        "LIKELY_REACTION": 0,
        "ROOM_ACTIVITY": 0,
        "WINDOW_TRUNCATED": 0,
        "NO_REACTION": 0,
        "READ_ERROR": 0,
    }
    for row in con.execute(
        "SELECT classification,COUNT(*) AS n FROM reaction_memory GROUP BY classification"
    ).fetchall():
        result[str(row["classification"])] = int(row["n"])
    return result


def record_collaboration_shadow_decision(
    con: sqlite3.Connection,
    *,
    observed_at: str,
    room: str,
    through_seq: int,
    marker: str,
    actual_agent: str,
    shadow_agent: str,
    actual_relationship: int,
    shadow_relationship: int,
    shadow_collaboration: int,
    shadow_combined: int,
    candidate_count: int,
    responder_direct: int,
    responder_likely: int,
    target_direct: int,
    target_likely: int,
) -> bool:
    marker = str(marker).upper()
    if marker not in {"SAME", "WOULD_PREFER"}:
        raise ValueError(f"invalid collaboration shadow marker: {marker}")
    cur = con.execute(
        """
        INSERT OR IGNORE INTO collaboration_shadow_decisions(
          observed_at,room,through_seq,marker,actual_agent,shadow_agent,
          actual_relationship,shadow_relationship,shadow_collaboration,
          shadow_combined,candidate_count,responder_direct,responder_likely,
          target_direct,target_likely
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(observed_at),
            str(room)[:80],
            int(through_seq),
            marker,
            str(actual_agent)[:240],
            str(shadow_agent)[:240],
            max(0, min(100, int(actual_relationship))),
            max(0, min(100, int(shadow_relationship))),
            max(0, min(100, int(shadow_collaboration))),
            max(0, min(100, int(shadow_combined))),
            max(0, int(candidate_count)),
            max(0, int(responder_direct)),
            max(0, int(responder_likely)),
            max(0, int(target_direct)),
            max(0, int(target_likely)),
        ),
    )
    return cur.rowcount > 0


def collaboration_shadow_counts(con: sqlite3.Connection) -> dict[str, int]:
    result = {"SAME": 0, "WOULD_PREFER": 0}
    for row in con.execute(
        """
        SELECT marker,COUNT(*) AS n
        FROM collaboration_shadow_decisions
        GROUP BY marker
        """
    ).fetchall():
        result[str(row["marker"])] = int(row["n"])
    return result


def collaboration_shadow_rows(
    con: sqlite3.Connection,
    limit: int = 50,
) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT
          id,observed_at,room,through_seq,marker,actual_agent,shadow_agent,
          actual_relationship,shadow_relationship,shadow_collaboration,
          shadow_combined,candidate_count,responder_direct,responder_likely,
          target_direct,target_likely
        FROM collaboration_shadow_decisions
        ORDER BY id DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
