#!/usr/bin/env python3
"""Resolve persisted collaboration-shadow decisions against Reaction Memory."""

from __future__ import annotations

import argparse
from typing import Any

from technoscout.common import utc_now
from technoscout.db import (
    collaboration_shadow_evaluation_counts,
    collaboration_shadow_evaluation_rows,
    collaboration_shadow_rows,
    connect,
    upsert_collaboration_shadow_evaluation,
)
from technoscout_cli import database_path, load_config


def evaluate_shadow_row(con: Any, row: Any) -> dict[str, Any]:
    draft = con.execute(
        """
        SELECT id,status,target_agent
        FROM reply_drafts
        WHERE room=? AND through_seq=?
        """,
        (str(row["room"]), int(row["through_seq"])),
    ).fetchone()

    if draft is None:
        return {
            "state": "UNRESOLVED",
            "send_attempt_id": None,
            "classification": "",
            "coverage": "",
            "responder_did": "",
            "actual_target_replied": False,
        }

    sent = con.execute(
        """
        SELECT id
        FROM send_attempts
        WHERE draft_id=? AND status='sent'
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(draft["id"]),),
    ).fetchone()

    if sent is None:
        return {
            "state": "UNRESOLVED",
            "send_attempt_id": None,
            "classification": "",
            "coverage": "",
            "responder_did": "",
            "actual_target_replied": False,
        }

    memory = con.execute(
        """
        SELECT classification,coverage,responder_did
        FROM reaction_memory
        WHERE send_attempt_id=?
        """,
        (int(sent["id"]),),
    ).fetchone()

    if memory is None:
        return {
            "state": "UNRESOLVED",
            "send_attempt_id": int(sent["id"]),
            "classification": "",
            "coverage": "",
            "responder_did": "",
            "actual_target_replied": False,
        }

    classification = str(memory["classification"])
    coverage = str(memory["coverage"])
    responder_did = str(memory["responder_did"] or "")

    if (
        coverage != "OBSERVED"
        or classification in {"WINDOW_TRUNCATED", "READ_ERROR"}
    ):
        return {
            "state": "UNRESOLVED",
            "send_attempt_id": int(sent["id"]),
            "classification": classification,
            "coverage": coverage,
            "responder_did": responder_did,
            "actual_target_replied": False,
        }

    actual_target_replied = bool(
        classification in {"DIRECT_REPLY", "LIKELY_REACTION"}
        and responder_did == str(row["actual_agent"])
    )
    return {
        "state": (
            "ACTUAL_REPLIED"
            if actual_target_replied
            else "ACTUAL_NO_REPLY"
        ),
        "send_attempt_id": int(sent["id"]),
        "classification": classification,
        "coverage": coverage,
        "responder_did": responder_did,
        "actual_target_replied": actual_target_replied,
    }


def sync_shadow_evaluations(
    con: Any,
    *,
    limit: int = 100,
    verbose: bool = False,
) -> dict[str, int]:
    stats = {
        "checked": 0,
        "changed": 0,
        "resolved": 0,
        "unresolved": 0,
        "actual_replied": 0,
        "actual_no_reply": 0,
    }
    for row in collaboration_shadow_rows(con, limit):
        result = evaluate_shadow_row(con, row)
        existing = con.execute(
            """
            SELECT state,send_attempt_id,classification,coverage,
                   responder_did,actual_target_replied
            FROM collaboration_shadow_evaluations
            WHERE decision_id=?
            """,
            (int(row["id"]),),
        ).fetchone()
        new_signature = (
            str(result["state"]),
            result["send_attempt_id"],
            str(result["classification"]),
            str(result["coverage"]),
            str(result["responder_did"]),
            1 if bool(result["actual_target_replied"]) else 0,
        )
        old_signature = (
            (
                str(existing["state"]),
                existing["send_attempt_id"],
                str(existing["classification"]),
                str(existing["coverage"]),
                str(existing["responder_did"]),
                int(existing["actual_target_replied"]),
            )
            if existing is not None
            else None
        )
        if old_signature != new_signature:
            stats["changed"] += 1

        upsert_collaboration_shadow_evaluation(
            con,
            decision_id=int(row["id"]),
            evaluated_at=utc_now(),
            state=str(result["state"]),
            send_attempt_id=result["send_attempt_id"],
            classification=str(result["classification"]),
            coverage=str(result["coverage"]),
            responder_did=str(result["responder_did"]),
            actual_target_replied=bool(result["actual_target_replied"]),
        )
        stats["checked"] += 1
        if result["state"] == "UNRESOLVED":
            stats["unresolved"] += 1
        else:
            stats["resolved"] += 1
            if result["state"] == "ACTUAL_REPLIED":
                stats["actual_replied"] += 1
            else:
                stats["actual_no_reply"] += 1

        if verbose:
            print(
                f"[shadow-eval] decision=#{row['id']} marker={row['marker']} "
                f"room={row['room']} state={result['state']} "
                f"reaction={result['classification'] or '-'} "
                f"coverage={result['coverage'] or '-'}",
                flush=True,
            )
    con.commit()
    return stats


def report_shadow_evaluations(con: Any, limit: int = 50) -> None:
    counts = collaboration_shadow_evaluation_counts(con)
    rows = collaboration_shadow_evaluation_rows(con, limit)

    same_resolved = 0
    same_replied = 0
    prefer_resolved = 0
    prefer_actual_no_reply = 0

    for row in rows:
        if str(row["state"]) == "UNRESOLVED":
            continue
        if str(row["marker"]) == "SAME":
            same_resolved += 1
            if str(row["state"]) == "ACTUAL_REPLIED":
                same_replied += 1
        elif str(row["marker"]) == "WOULD_PREFER":
            prefer_resolved += 1
            if str(row["state"]) == "ACTUAL_NO_REPLY":
                prefer_actual_no_reply += 1

    print(
        "Collaboration Shadow Evaluation | "
        f"total={sum(counts.values())} "
        f"resolved={counts.get('ACTUAL_REPLIED',0)+counts.get('ACTUAL_NO_REPLY',0)} "
        f"unresolved={counts.get('UNRESOLVED',0)} "
        f"actual_replied={counts.get('ACTUAL_REPLIED',0)} "
        f"actual_no_reply={counts.get('ACTUAL_NO_REPLY',0)}"
    )
    print(
        "  SAME | "
        f"resolved={same_resolved} actual_replied={same_replied}"
    )
    print(
        "  WOULD_PREFER | "
        f"resolved={prefer_resolved} "
        f"actual_no_reply={prefer_actual_no_reply} "
        "shadow_target_outcome=NOT_TESTED"
    )

    for row in rows:
        print(
            f"  decision=#{row['decision_id']} {row['marker']} "
            f"state={row['state']} room={row['room']} "
            f"reaction={row['classification'] or '-'} "
            f"coverage={row['coverage'] or '-'}"
        )

    print(
        "Interpretation: ACTUAL_NO_REPLY only says the relationship-only target "
        "did not produce the qualifying observed reaction. For WOULD_PREFER, "
        "the alternative shadow target was never messaged, so its outcome "
        "remains counterfactual and is not inferred."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate collaboration-shadow decisions against Reaction Memory"
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("sync", "status"),
        default="status",
    )
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    cfg = load_config(args.config)
    con = connect(database_path(cfg))
    try:
        if args.command == "sync":
            stats = sync_shadow_evaluations(
                con,
                limit=max(1, int(args.limit)),
                verbose=True,
            )
            print(
                "Shadow Evaluation Sync | "
                + " ".join(f"{key}={value}" for key, value in stats.items())
            )
        report_shadow_evaluations(con, min(100, max(1, int(args.limit))))
    finally:
        con.close()


if __name__ == "__main__":
    main()
