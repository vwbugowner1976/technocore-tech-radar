#!/usr/bin/env python3
"""Resume one locally blocked JOB after a known quality condition.

This helper exists only for jobs that were already CLAIMed and then blocked by a
known local quality condition that newer code can handle without re-CLAIMing. It
performs no signed write itself. It re-arms only the local post-claim pipeline,
then delegates to job_action.py, which still requires explicit human confirmation
immediately before any DELIVER send.
"""

from __future__ import annotations

import argparse

from job_action import _latest_auto, _latest_status, run_action
from job_candidate_refiner import _runtime_defaults
from technoscout.common import utc_now
from technoscout.db import connect
from technoscout_cli import database_path, load_config


KNOWN_REASONS = (
    "quality repair returned the candidate answer unchanged",
    "final quality adjudicator marked REVISED but again returned the candidate answer unchanged",
    "adjudicator-guided repair returned the candidate answer unchanged",
    "adjudicator-guided repair already attempted: UNCHANGED",
    "adjudicator-guided addition contains no new information",
    (
        "The candidate answer does not provide a concrete failure mode and leading "
        "indicator as requested. It only mentions memory fragmentation and an "
        "out-of-memory (OOM) error as the failure mode, without specifying the exact "
        "signal that shows up before it."
    ),
)

KNOWN_PREFIXES = (
    "quality:",
    "quality-adjudication-repair:",
)


def resume_quality_block(con, cfg, job_id: str, *, room: str = "kibble") -> str:
    tracked = _latest_auto(con, room, job_id)
    if tracked is None:
        print("STOP: this job is not tracked by the safe auto orchestrator")
        return "UNTRACKED"

    if str(tracked["pipeline_state"] or "") != "BLOCKED":
        print(f"STOP: pipeline state is {tracked['pipeline_state']}, not BLOCKED")
        return "NOT_BLOCKED"

    detail = str(tracked["detail"] or "")
    known = (
        any(detail.startswith(prefix) for prefix in KNOWN_PREFIXES)
        and any(reason in detail for reason in KNOWN_REASONS)
    )
    if not known:
        print(f"STOP: block reason is not a known resumable quality condition: {detail}")
        return "BLOCKED_OTHER_REASON"

    claim_state = _latest_status(
        con, "job_claim_trials", "prepared_at", room, job_id
    )
    if claim_state != "SENT":
        print(f"STOP: CLAIM state is {claim_state or 'missing'}, not SENT")
        return "CLAIM_NOT_SENT"

    delivery_state = _latest_status(
        con, "job_delivery_trials", "prepared_at", room, job_id
    )
    if delivery_state in {"SENT", "UNCERTAIN", "RESERVED"}:
        print(f"STOP: delivery is terminal state {delivery_state}; do not retry")
        return delivery_state

    changed = con.execute(
        """
        UPDATE job_auto_orchestrator
        SET pipeline_state='WAITING_POSTCLAIM',
            detail='human-invoked retry after known quality block',
            updated_at=?
        WHERE room=? AND job_id=? AND content_hash=? AND pipeline_state='BLOCKED'
        """,
        (
            utc_now(),
            str(tracked["room"]),
            str(tracked["job_id"]),
            str(tracked["content_hash"]),
        ),
    )
    if changed.rowcount != 1:
        con.rollback()
        print("STOP: blocked row changed before retry; nothing was modified")
        return "STATE_CHANGED"
    con.commit()

    print("=== QUALITY BLOCK RESUME ===")
    print("CLAIM is already SENT; no CLAIM will be resent.")
    print("Re-running local DRAFT -> REVIEW -> QUALITY -> SUCCESS -> DELIVERY PREPARE.")
    print("A signed DELIVER still requires the normal explicit human confirmation.")
    return run_action(con, cfg, job_id, room=room)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resume one known resumable local quality block"
    )
    parser.add_argument("job_id")
    parser.add_argument("--room", default="kibble")
    parser.add_argument("--config", default="technoscout.config.json")
    args = parser.parse_args()

    cfg = _runtime_defaults(load_config(args.config))
    con = connect(database_path(cfg))
    try:
        state = resume_quality_block(con, cfg, args.job_id, room=args.room)
    finally:
        con.close()
    print(f"Job Quality Resume | final_state={state} job={args.job_id}")


if __name__ == "__main__":
    main()
