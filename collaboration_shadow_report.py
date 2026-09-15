#!/usr/bin/env python3
"""Report persisted collaboration-shadow decisions and observed real outcomes."""

from __future__ import annotations

import argparse
from typing import Any

from technoscout.db import (
    collaboration_shadow_counts,
    collaboration_shadow_rows,
    connect,
)
from technoscout_cli import database_path, load_config


def short_did(value: str) -> str:
    text = str(value or "")
    if len(text) <= 28:
        return text
    return text[:18] + "…" + text[-8:]


def shadow_outcome(con: Any, row: Any) -> dict[str, Any]:
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
            "draft_id": None,
            "draft_status": "missing",
            "sent": False,
            "classification": "",
            "coverage": "",
            "responder_did": "",
            "actual_target_replied": False,
            "shadow_counterfactual": "not-tested",
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
            "draft_id": int(draft["id"]),
            "draft_status": str(draft["status"]),
            "sent": False,
            "classification": "",
            "coverage": "",
            "responder_did": "",
            "actual_target_replied": False,
            "shadow_counterfactual": "not-tested",
        }

    memory = con.execute(
        """
        SELECT classification,coverage,responder_did
        FROM reaction_memory
        WHERE send_attempt_id=?
        """,
        (int(sent["id"]),),
    ).fetchone()

    classification = str(memory["classification"]) if memory else ""
    coverage = str(memory["coverage"]) if memory else ""
    responder_did = str(memory["responder_did"]) if memory else ""
    actual_target_replied = bool(
        memory
        and classification in {"DIRECT_REPLY", "LIKELY_REACTION"}
        and responder_did == str(row["actual_agent"])
    )

    # When WOULD_PREFER differs from the actual target, the shadow target was
    # never messaged. Its response is a counterfactual and must not be inferred.
    counterfactual = (
        "same-as-actual"
        if str(row["shadow_agent"]) == str(row["actual_agent"])
        else "not-tested"
    )
    return {
        "draft_id": int(draft["id"]),
        "draft_status": str(draft["status"]),
        "sent": True,
        "classification": classification,
        "coverage": coverage,
        "responder_did": responder_did,
        "actual_target_replied": actual_target_replied,
        "shadow_counterfactual": counterfactual,
    }


def report(con: Any, limit: int = 50) -> None:
    counts = collaboration_shadow_counts(con)
    rows = collaboration_shadow_rows(con, limit)

    sent_count = 0
    actual_replied = 0
    outcomes = []
    for row in rows:
        outcome = shadow_outcome(con, row)
        outcomes.append((row, outcome))
        if outcome["sent"]:
            sent_count += 1
        if outcome["actual_target_replied"]:
            actual_replied += 1

    print(
        "Collaboration Shadow Report | "
        f"decisions={sum(counts.values())} "
        f"same={counts.get('SAME',0)} "
        f"would_prefer={counts.get('WOULD_PREFER',0)} "
        f"shown={len(rows)} sent={sent_count} "
        f"actual_target_replied={actual_replied}"
    )

    if not rows:
        print("  no shadow decisions recorded yet")
        return

    for row, outcome in outcomes:
        print()
        print(
            f"#{row['id']} {row['marker']} room={row['room']} "
            f"through_seq={row['through_seq']} candidates={row['candidate_count']}"
        )
        print(
            f"  actual={short_did(row['actual_agent'])} "
            f"relationship={row['actual_relationship']}"
        )
        print(
            f"  shadow={short_did(row['shadow_agent'])} "
            f"relationship={row['shadow_relationship']} "
            f"collaboration={row['shadow_collaboration']} "
            f"combined={row['shadow_combined']}"
        )
        print(
            f"  outcome=draft#{outcome['draft_id']} "
            f"status={outcome['draft_status']} "
            f"sent={'yes' if outcome['sent'] else 'no'} "
            f"reaction={outcome['classification'] or '-'} "
            f"coverage={outcome['coverage'] or '-'} "
            f"actual_target_replied="
            f"{'yes' if outcome['actual_target_replied'] else 'no'}"
        )
        if str(row["marker"]) == "WOULD_PREFER":
            print(
                "  shadow_outcome=NOT_TESTED "
                "(counterfactual; shadow target was not messaged)"
            )

    print()
    print(
        "Interpretation: WOULD_PREFER only records a disagreement with the "
        "relationship-only target. It does not prove the shadow target would "
        "have replied. Real target selection remains unchanged."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect persisted collaboration-shadow decisions"
    )
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    cfg = load_config(args.config)
    con = connect(database_path(cfg))
    try:
        report(con, max(1, args.limit))
    finally:
        con.close()


if __name__ == "__main__":
    main()
