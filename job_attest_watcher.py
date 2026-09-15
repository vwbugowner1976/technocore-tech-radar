#!/usr/bin/env python3
"""Read-only watcher that notifies when our delivered Kibble job is ATTESTed.

This watcher never writes to Technocore. It only observes signed ATTEST records
for jobs that this TechnoScout successfully DELIVERed, verifies that the ATTEST
sender matches the original job issuer, stores structured metadata, and publishes
a minimal local ntfy notification. Raw ATTEST text is never persisted.
"""

from __future__ import annotations

import argparse
import re
from typing import Any, Callable

from job_claim_trial import ensure_claim_schema
from job_delivery_trial import ensure_delivery_schema
from job_shadow import parse_kibble_message, sender_of, signed_did
from technoscout.common import room_messages, seq_of, technocore_json, utc_now
from technoscout.db import connect
from technoscout_notify import notify_job_attest
from technoscout_cli import database_path, load_config


ATTEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_attest_watch_state (
    room TEXT PRIMARY KEY,
    cursor INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS job_attest_notifications (
    room TEXT NOT NULL,
    job_id TEXT NOT NULL,
    attest_seq INTEGER NOT NULL,
    issuer_did TEXT NOT NULL,
    result TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    notify_state TEXT NOT NULL DEFAULT 'PENDING',
    detail TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(room, attest_seq)
);
CREATE INDEX IF NOT EXISTS idx_job_attest_notify_state
    ON job_attest_notifications(notify_state, attest_seq);
"""

_RESULT_CLEAN = re.compile(r"[^a-z0-9_.+-]+")


def ensure_attest_schema(con: Any) -> None:
    con.executescript(ATTEST_SCHEMA)
    ensure_claim_schema(con)
    ensure_delivery_schema(con)


def _result_label(rest: str) -> str:
    first = str(rest or "").split(" | ", 1)[0].strip().lower()
    label = _RESULT_CLEAN.sub("-", first).strip("-")[:32]
    return label or "attested"


def _watch_cursor(con: Any, room: str) -> int:
    row = con.execute(
        "SELECT cursor FROM job_attest_watch_state WHERE room=?",
        (room,),
    ).fetchone()
    return int(row["cursor"]) if row is not None else 0


def _set_watch_cursor(con: Any, room: str, cursor: int) -> None:
    con.execute(
        """
        INSERT INTO job_attest_watch_state(room,cursor) VALUES(?,?)
        ON CONFLICT(room) DO UPDATE SET cursor=excluded.cursor
        """,
        (room, max(0, int(cursor))),
    )


def _eligible_delivery(
    con: Any,
    *,
    room: str,
    job_id: str,
    attest_seq: int,
    attest_sender: str,
) -> bool:
    """Only accept issuer ATTESTs after our successful DELIVER."""
    row = con.execute(
        """
        SELECT d.sent_seq AS deliver_seq, c.issuer_did AS issuer_did
        FROM job_delivery_trials d
        JOIN job_claim_trials c
          ON c.room=d.room
         AND c.job_id=d.job_id
         AND c.content_hash=d.content_hash
        WHERE d.room=? AND d.job_id=? AND d.status='SENT'
        ORDER BY d.prepared_at DESC
        LIMIT 1
        """,
        (room, job_id),
    ).fetchone()
    if row is None or row["deliver_seq"] is None:
        return False
    issuer = str(row["issuer_did"] or "")
    if not signed_did(attest_sender) or attest_sender != issuer:
        return False
    return int(attest_seq) > int(row["deliver_seq"])


def scan_attest_notifications(
    con: Any,
    cfg: dict[str, Any],
    *,
    room: str | None = None,
    fetcher: Callable[[dict[str, Any], str, dict[str, Any]], Any] | None = None,
    notifier: Callable[[dict[str, Any], str, str], dict[str, str]] | None = None,
) -> dict[str, int]:
    """Observe new ATTESTs and publish queued notifications fail-soft."""
    ensure_attest_schema(con)
    target_room = str(room or cfg.get("job_shadow_room", "kibble"))
    limit = max(20, min(200, int(cfg.get("job_attest_fetch_limit", 200))))
    cursor = _watch_cursor(con, target_room)
    query: dict[str, Any] = {"format": "json", "limit": limit}
    if cursor > 0:
        query["since"] = cursor

    read = fetcher or technocore_json
    stats = {
        "messages": 0,
        "observed": 0,
        "published": 0,
        "failed": 0,
        "disabled": 0,
        "skipped": 0,
    }

    payload = read(cfg, f"/r/{target_room}", query)
    messages = sorted(room_messages(payload), key=seq_of)
    stats["messages"] = len(messages)
    highest = cursor

    for message in messages:
        message_seq = seq_of(message)
        highest = max(highest, message_seq)
        parsed = parse_kibble_message(message.get("text", message.get("message", "")))
        if not parsed or parsed.get("verb") != "ATTEST":
            continue
        job_id = str(parsed.get("job_id", ""))
        sender = sender_of(message)
        if not _eligible_delivery(
            con,
            room=target_room,
            job_id=job_id,
            attest_seq=message_seq,
            attest_sender=sender,
        ):
            stats["skipped"] += 1
            continue
        result = _result_label(str(parsed.get("rest", "")))
        before = con.total_changes
        con.execute(
            """
            INSERT OR IGNORE INTO job_attest_notifications(
              room,job_id,attest_seq,issuer_did,result,observed_at,
              attempts,notify_state,detail
            ) VALUES(?,?,?,?,?,?,0,'PENDING','')
            """,
            (target_room, job_id, message_seq, sender[:240], result, utc_now()),
        )
        if con.total_changes > before:
            stats["observed"] += 1

    if highest > cursor:
        _set_watch_cursor(con, target_room, highest)
    con.commit()

    publish = notifier or notify_job_attest
    rows = con.execute(
        """
        SELECT room,job_id,attest_seq,result,attempts
        FROM job_attest_notifications
        WHERE room=? AND notify_state IN ('PENDING','FAILED') AND attempts < 20
        ORDER BY attest_seq ASC
        LIMIT 10
        """,
        (target_room,),
    ).fetchall()
    for row in rows:
        notice = publish(cfg, str(row["job_id"]), str(row["result"]))
        state = str(notice.get("state", "FAILED"))
        detail = str(notice.get("detail", ""))[:300]
        if state == "PUBLISHED_LOCAL":
            con.execute(
                """
                UPDATE job_attest_notifications
                SET notify_state='PUBLISHED',attempts=attempts+1,detail=?
                WHERE room=? AND attest_seq=?
                """,
                (detail, target_room, int(row["attest_seq"])),
            )
            stats["published"] += 1
        elif state == "DISABLED":
            # Keep it pending so enabling ntfy later still delivers the event.
            con.execute(
                "UPDATE job_attest_notifications SET detail=? WHERE room=? AND attest_seq=?",
                (detail, target_room, int(row["attest_seq"])),
            )
            stats["disabled"] += 1
        else:
            con.execute(
                """
                UPDATE job_attest_notifications
                SET notify_state='FAILED',attempts=attempts+1,detail=?
                WHERE room=? AND attest_seq=?
                """,
                (detail, target_room, int(row["attest_seq"])),
            )
            stats["failed"] += 1
    con.commit()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only ATTEST notifier for delivered Kibble jobs")
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--room", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    con = connect(database_path(cfg))
    try:
        stats = scan_attest_notifications(con, cfg, room=args.room)
        print(
            "Job ATTEST Watch | "
            f"messages={stats['messages']} observed={stats['observed']} "
            f"published={stats['published']} failed={stats['failed']} "
            f"disabled={stats['disabled']} skipped={stats['skipped']}"
        )
        print("NOTE: read-only against Technocore; no CLAIM/DELIVER/write was sent.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
