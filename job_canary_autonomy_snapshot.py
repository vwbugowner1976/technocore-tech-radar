#!/usr/bin/env python3
"""Immutable local JOB snapshots captured during Shadow CANARY autonomy review."""

from __future__ import annotations

import hashlib
from typing import Any

from technoscout.common import utc_now


SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_canary_autonomy_snapshots (
    room TEXT NOT NULL,
    job_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    job_type TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    snapshot_hash TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY(room,job_id,content_hash)
);
CREATE INDEX IF NOT EXISTS idx_job_canary_autonomy_snapshots_captured
    ON job_canary_autonomy_snapshots(captured_at DESC);
"""


def ensure_autonomy_snapshot_schema(con: Any) -> None:
    con.executescript(SNAPSHOT_SCHEMA)


def _clean(value: Any, maximum: int) -> str:
    return str(value or "")[:maximum]


def _snapshot_hash(job_type: str, title: str, body: str) -> str:
    payload = "\0".join((job_type, title, body)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def store_autonomy_snapshot(
    con: Any,
    candidate: dict[str, Any],
    job: dict[str, Any],
) -> dict[str, Any]:
    """Insert once; never overwrite different content for the same content binding."""
    ensure_autonomy_snapshot_schema(con)
    room = str(candidate.get("room", "kibble"))
    job_id = str(candidate.get("job_id", ""))
    content_hash = str(candidate.get("content_hash", ""))
    job_type = _clean(job.get("job_type", job.get("type", candidate.get("job_type", ""))), 80)
    title = _clean(job.get("title", ""), 1200)
    body = _clean(job.get("body", ""), 5000)
    digest = _snapshot_hash(job_type, title, body)

    existing = con.execute(
        """
        SELECT snapshot_hash FROM job_canary_autonomy_snapshots
        WHERE room=? AND job_id=? AND content_hash=?
        """,
        (room, job_id, content_hash),
    ).fetchone()
    if existing is not None:
        if str(existing["snapshot_hash"]) != digest:
            return {
                "state": "SNAPSHOT_CONFLICT",
                "reason": "existing autonomy JOB snapshot differs for the same content binding",
            }
        return {"state": "SNAPSHOT_VERIFIED", "snapshot_hash": digest}

    con.execute(
        """
        INSERT INTO job_canary_autonomy_snapshots(
          room,job_id,content_hash,job_type,title,body,snapshot_hash,captured_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (room, job_id, content_hash, job_type, title, body, digest, utc_now()),
    )
    con.commit()
    return {"state": "SNAPSHOT_STORED", "snapshot_hash": digest}


def load_autonomy_snapshot(
    con: Any,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    ensure_autonomy_snapshot_schema(con)
    row = con.execute(
        """
        SELECT room,job_id,content_hash,job_type,title,body,snapshot_hash,captured_at
        FROM job_canary_autonomy_snapshots
        WHERE room=? AND job_id=? AND content_hash=?
        """,
        (
            str(candidate.get("room", "kibble")),
            str(candidate.get("job_id", "")),
            str(candidate.get("content_hash", "")),
        ),
    ).fetchone()
    if row is None:
        return {"state": "SNAPSHOT_NOT_FOUND"}

    digest = _snapshot_hash(str(row["job_type"]), str(row["title"]), str(row["body"]))
    if digest != str(row["snapshot_hash"]):
        return {"state": "SNAPSHOT_INVALID", "reason": "stored autonomy JOB snapshot hash mismatch"}
    return {
        "state": "EXACT",
        "source": "autonomy-snapshot",
        "captured_at": str(row["captured_at"]),
        "job": {
            "job_type": str(row["job_type"]),
            "title": str(row["title"]),
            "body": str(row["body"]),
        },
    }
