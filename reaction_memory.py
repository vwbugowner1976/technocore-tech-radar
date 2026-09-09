#!/usr/bin/env python3
"""Persist conservative reaction classifications without storing raw reply text."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any, Callable

from reaction_tracker import (
    classify_reaction,
    fetch_room_after,
    known_self_dids,
    reaction_window_truncated,
    sender_of,
    sent_seq,
)
from technoscout.common import seq_of, utc_now
from technoscout.db import (
    connect,
    reaction_memory_counts,
    reaction_memory_rows,
    upsert_reaction_memory,
)
from technoscout_cli import database_path, load_config


def sent_rows(con: Any, limit: int) -> list[Any]:
    return con.execute(
        """
        SELECT
          s.id AS attempt_id,
          s.draft_id,
          s.room,
          s.did,
          s.text,
          s.detail,
          s.attempted_at,
          d.target_agent
        FROM send_attempts s
        JOIN reply_drafts d ON d.id=s.draft_id
        WHERE s.status='sent'
        ORDER BY s.id DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def reaction_check_due(
    *,
    attempted_at: str,
    last_checked_at: str,
    classification: str,
    now: datetime,
    max_age_seconds: int = 86400,
) -> bool:
    """Return whether a verified send should be rechecked automatically."""
    attempted = _parse_iso(attempted_at)
    checked = _parse_iso(last_checked_at)
    now = now.astimezone(timezone.utc)

    # Never auto-recheck an explicit direct reply: it is terminal evidence.
    if str(classification).upper() == "DIRECT_REPLY":
        return False

    # Unsynced sends get one initial check even if they predate the normal
    # tracking horizon. This makes upgrades safe for existing databases.
    if not str(last_checked_at or "").strip():
        return True

    if attempted is None or checked is None:
        return True

    send_age = max(0.0, (now - attempted).total_seconds())
    if send_age > max(60, int(max_age_seconds)):
        return False

    checked_age = max(0.0, (now - checked).total_seconds())

    # Follow fresh posts closely so busy rooms do not push an immediate reply
    # out of the 200-message window. Back off as the post ages.
    if send_age <= 15 * 60:
        interval = 60
    elif send_age <= 2 * 60 * 60:
        interval = 5 * 60
    else:
        interval = 30 * 60

    return checked_age >= interval


def due_sent_rows(
    con: Any,
    *,
    limit: int,
    now: datetime,
    max_age_seconds: int = 86400,
) -> list[Any]:
    scan_limit = max(50, max(1, int(limit)) * 12)
    rows = con.execute(
        """
        SELECT
          s.id AS attempt_id,
          s.draft_id,
          s.room,
          s.did,
          s.text,
          s.detail,
          s.attempted_at,
          d.target_agent,
          rm.classification AS memory_classification,
          rm.last_checked_at AS memory_last_checked_at
        FROM send_attempts s
        JOIN reply_drafts d ON d.id=s.draft_id
        LEFT JOIN reaction_memory rm ON rm.send_attempt_id=s.id
        WHERE s.status='sent'
        ORDER BY s.id DESC
        LIMIT ?
        """,
        (scan_limit,),
    ).fetchall()

    due = []
    for row in rows:
        if reaction_check_due(
            attempted_at=str(row["attempted_at"]),
            last_checked_at=str(row["memory_last_checked_at"] or ""),
            classification=str(row["memory_classification"] or ""),
            now=now,
            max_age_seconds=max_age_seconds,
        ):
            due.append(row)
            if len(due) >= max(1, int(limit)):
                break
    return due


def sync_reaction_memory(
    con: Any,
    cfg: dict[str, Any],
    *,
    limit: int = 50,
    message_limit: int = 200,
    rows: list[Any] | None = None,
    verbose: bool = True,
    fetcher: Callable[[dict[str, Any], str, int, int], list[dict[str, Any]]] | None = None,
) -> dict[str, int]:
    self_dids = known_self_dids(con, cfg)
    stats = {
        "checked": 0,
        "inserted": 0,
        "updated": 0,
        "preserved": 0,
        "no_seq": 0,
        "read_error": 0,
    }

    work_rows = rows if rows is not None else sent_rows(con, limit)
    room_fetcher = fetcher or fetch_room_after

    for row in work_rows:
        our_seq = sent_seq(str(row["detail"]))
        if our_seq is None:
            stats["no_seq"] += 1
            continue

        room = str(row["room"])
        target_agent = str(row["target_agent"] or "")
        checked_at = utc_now()

        try:
            messages = room_fetcher(
                cfg,
                room,
                our_seq,
                message_limit,
            )
            truncated = reaction_window_truncated(
                messages,
                our_seq,
                message_limit,
            )
            classification, candidate, overlap, foreign_posts = classify_reaction(
                room=room,
                our_seq=our_seq,
                our_text=str(row["text"]),
                target_agent=target_agent,
                messages=messages,
                self_dids=self_dids,
                window_truncated=truncated,
            )
            coverage = "PARTIAL" if truncated else "OBSERVED"
            responder_did = sender_of(candidate) if candidate is not None else ""
            responder_seq = seq_of(candidate) if candidate is not None else None
        except Exception:
            classification = "READ_ERROR"
            coverage = "ERROR"
            responder_did = ""
            responder_seq = None
            overlap = 0
            foreign_posts = 0
            stats["read_error"] += 1

        action = upsert_reaction_memory(
            con,
            send_attempt_id=int(row["attempt_id"]),
            draft_id=int(row["draft_id"]),
            checked_at=checked_at,
            room=room,
            our_seq=our_seq,
            target_agent=target_agent,
            classification=classification,
            coverage=coverage,
            responder_did=responder_did,
            responder_seq=responder_seq,
            overlap=int(overlap),
            foreign_posts=int(foreign_posts),
        )
        con.commit()
        stats["checked"] += 1
        stats[action] += 1

        if verbose:
            print(
                f"[reaction-memory] draft=#{row['draft_id']} room={room} "
                f"seq={our_seq} class={classification} coverage={coverage} "
                f"action={action}"
                + (
                    f" responder={responder_did[:30]} responder_seq={responder_seq}"
                    if responder_did
                    else ""
                ),
                flush=True,
            )

    return stats


def auto_sync_reaction_memory(
    con: Any,
    cfg: dict[str, Any],
    *,
    now: datetime | None = None,
    limit: int = 6,
    message_limit: int = 200,
    max_age_seconds: int = 86400,
    verbose: bool = False,
    fetcher: Callable[[dict[str, Any], str, int, int], list[dict[str, Any]]] | None = None,
) -> dict[str, int]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = due_sent_rows(
        con,
        limit=max(1, int(limit)),
        now=current,
        max_age_seconds=max_age_seconds,
    )
    stats = {
        "due": len(rows),
        "checked": 0,
        "inserted": 0,
        "updated": 0,
        "preserved": 0,
        "no_seq": 0,
        "read_error": 0,
    }
    if not rows:
        return stats

    synced = sync_reaction_memory(
        con,
        cfg,
        limit=len(rows),
        message_limit=max(1, min(200, int(message_limit))),
        rows=rows,
        verbose=verbose,
        fetcher=fetcher,
    )
    stats.update(synced)
    stats["due"] = len(rows)
    return stats


def print_status(con: Any, limit: int = 20) -> None:
    counts = reaction_memory_counts(con)
    total = con.execute("SELECT COUNT(*) AS n FROM reaction_memory").fetchone()["n"]
    print(
        "Reaction Memory | "
        f"total={total} "
        f"direct={counts.get('DIRECT_REPLY', 0)} "
        f"likely={counts.get('LIKELY_REACTION', 0)} "
        f"activity={counts.get('ROOM_ACTIVITY', 0)} "
        f"partial={counts.get('WINDOW_TRUNCATED', 0)} "
        f"none={counts.get('NO_REACTION', 0)} "
        f"errors={counts.get('READ_ERROR', 0)}"
    )
    for row in reaction_memory_rows(con, limit):
        responder = str(row["responder_did"] or "")
        print(
            f"  send=#{row['send_attempt_id']} draft=#{row['draft_id']} "
            f"room={row['room']} seq={row['our_seq']} "
            f"class={row['classification']} coverage={row['coverage']} "
            f"checks={row['check_count']}"
            + (
                f" responder={responder[:30]} responder_seq={row['responder_seq']} "
                f"overlap={row['overlap']}"
                if responder
                else ""
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Persist and inspect TechnoScout reaction memory"
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("sync", "status"),
        default="status",
    )
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--message-limit", type=int, default=200)
    args = parser.parse_args()

    cfg = load_config(args.config)
    con = connect(database_path(cfg))
    try:
        if args.command == "sync":
            stats = sync_reaction_memory(
                con,
                cfg,
                limit=max(1, args.limit),
                message_limit=max(1, min(200, args.message_limit)),
            )
            print(
                "Reaction Memory Sync | "
                + " ".join(f"{key}={value}" for key, value in stats.items())
            )
            print_status(con, min(20, max(1, args.limit)))
            return
        print_status(con, min(50, max(1, args.limit)))
    finally:
        con.close()


if __name__ == "__main__":
    main()
