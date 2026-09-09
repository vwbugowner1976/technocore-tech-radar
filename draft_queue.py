#!/usr/bin/env python3
"""Inspect and compact the TechnoScout reply draft queue."""

from __future__ import annotations

import argparse

from technoscout import database_path, load_config
from technoscout.db import (
    connect,
    mark_pending_draft_status,
    pending_reply_drafts,
    reply_draft_counts,
    reply_drafts_by_status,
)


def archive_decided_blocks(con) -> int:
    cur = con.execute(
        """
        UPDATE reply_drafts
        SET status='autonomy_blocked'
        WHERE status='pending'
          AND EXISTS (
            SELECT 1
            FROM autonomy_decisions a
            WHERE a.draft_id=reply_drafts.id
              AND a.allowed=0
              AND a.outcome='blocked'
          )
        """
    )
    return int(cur.rowcount)


def supersede_stale_pending(con) -> int:
    rows = pending_reply_drafts(con, 100000)
    seen: set[tuple[str, str]] = set()
    changed = 0
    for row in rows:
        key = (str(row["room"]), str(row["target_agent"]))
        if key in seen:
            if mark_pending_draft_status(
                con, int(row["id"]), "superseded"
            ):
                changed += 1
        else:
            seen.add(key)
    return changed


def print_rows(rows, title: str, total: int) -> None:
    print(f"{title} | count={total}")
    for row in rows:
        print(
            f"\n#{row['id']} status={row['status']} room={row['room']} "
            f"target={row['target_agent'][:42]} "
            f"relationship={row['relationship_score']}\n"
            f"reason: {row['reason'][:220]}\n"
            f"draft: {row['draft_text']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="TechnoScout draft queue utility")
    parser.add_argument(
        "command",
        choices=("status", "cleanup", "blocked", "superseded"),
    )
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    cfg = load_config(args.config)
    con = connect(database_path(cfg))
    try:
        if args.command == "cleanup":
            blocked = archive_decided_blocks(con)
            superseded = supersede_stale_pending(con)
            con.commit()
            counts = reply_draft_counts(con)
            print(
                "Draft queue cleanup | "
                f"blocked={blocked} superseded={superseded} "
                f"pending={counts.get('pending',0)} "
                f"sent={counts.get('sent',0)}"
            )
            return

        counts = reply_draft_counts(con)
        if args.command == "status":
            print(
                "Draft queue | "
                f"pending={counts.get('pending',0)} "
                f"sent={counts.get('sent',0)} "
                f"blocked={counts.get('autonomy_blocked',0) + counts.get('send_blocked',0)} "
                f"superseded={counts.get('superseded',0)} "
                f"uncertain={counts.get('send_uncertain',0)}"
            )
            return

        if args.command == "blocked":
            rows = list(reply_drafts_by_status(
                con, "autonomy_blocked", args.limit
            ))
            rows.extend(reply_drafts_by_status(
                con, "send_blocked", args.limit
            ))
            rows = sorted(
                rows, key=lambda row: int(row["id"]), reverse=True
            )[:max(1, args.limit)]
            print_rows(
                rows,
                "Blocked Drafts",
                counts.get("autonomy_blocked",0) + counts.get("send_blocked",0),
            )
            return

        rows = reply_drafts_by_status(
            con, "superseded", max(1, args.limit)
        )
        print_rows(
            rows,
            "Superseded Drafts",
            counts.get("superseded",0),
        )
    finally:
        con.close()


if __name__ == "__main__":
    main()
