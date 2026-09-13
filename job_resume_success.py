#!/usr/bin/env python3
"""Human-invoked one-shot local retry for a structured Success-gate block.

This helper performs local verification only. It never claims, delivers, signs,
posts, or changes CLAIM/DELIVER trial status. For one (room, job_id,
content_hash), it may re-arm the local post-claim pipeline once and enable the
existing frozen-contract Success repairer for that invocation.

Version 2 also routes this human retry through the narrow literal named-item
proof supplement. That supplement can only add an exact sentence ID when a
short frozen requirement such as "one leading indicator" is literally expressed
as "the leading indicator is ..." in the answer. Grounding checks remain
untouched. The final frozen Success verifier remains fail-closed.

If the pipeline reaches DELIVERY_READY, use the normal human action command
separately to review the exact delivery preview.
"""

from __future__ import annotations

import argparse
import re
from typing import Any

from job_auto_orchestrator import ensure_auto_schema, process_sent_claims
from job_candidate_refiner import _runtime_defaults
from job_postclaim_pipeline import run_postclaim_pipeline
from job_success_named_proof import validate_success_criterion as validate_success_named
from technoscout.common import utc_now
from technoscout.db import connect
from technoscout_cli import database_path, load_config


_JOB_ID_RE = re.compile(r"^k[0-9a-f]{10}$")

# V2 intentionally uses a new ledger. A prior V1 retry that ran before the
# named-proof fix does not consume the one V2 retry. V2 itself is still one-shot.
_RETRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_success_human_retries_v2 (
    room TEXT NOT NULL,
    job_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    original_detail TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(room, job_id, content_hash)
);
"""


def _latest_auto(con: Any, room: str, job_id: str) -> Any | None:
    ensure_auto_schema(con)
    return con.execute(
        """
        SELECT * FROM job_auto_orchestrator
        WHERE room=? AND job_id=?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (str(room), str(job_id)),
    ).fetchone()


def _latest_status(con: Any, table: str, room: str, job_id: str) -> str:
    if table not in {"job_claim_trials", "job_delivery_trials"}:
        raise ValueError("unexpected table")
    row = con.execute(
        f"SELECT status FROM {table} WHERE room=? AND job_id=? ORDER BY prepared_at DESC LIMIT 1",
        (str(room), str(job_id)),
    ).fetchone()
    return str(row["status"]) if row is not None else ""


def _structured_success_block(detail: str) -> bool:
    text = str(detail or "")
    return (
        text.startswith("success:")
        and "structured exact-quote evidence does not satisfy frozen contract" in text
        and "requirements=[" in text
        and "grounding=[" in text
    )


def _named_success_pipeline(con: Any, cfg: dict[str, Any], job_id: str, **kwargs: Any) -> dict[str, Any]:
    """Run the normal pipeline with only the Success verifier swapped."""
    return run_postclaim_pipeline(
        con,
        cfg,
        job_id,
        success_runner=validate_success_named,
        **kwargs,
    )


def resume_success_block(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    *,
    room: str = "kibble",
) -> str:
    if not _JOB_ID_RE.fullmatch(str(job_id or "")):
        print("STOP: invalid job id")
        return "INVALID_JOB_ID"

    tracked = _latest_auto(con, room, job_id)
    if tracked is None:
        print("STOP: this job is not tracked by the safe auto orchestrator")
        return "UNTRACKED"

    if str(tracked["pipeline_state"] or "") != "BLOCKED":
        print(f"STOP: pipeline state is {tracked['pipeline_state']}, not BLOCKED")
        return "NOT_BLOCKED"

    detail = str(tracked["detail"] or "")
    if not _structured_success_block(detail):
        print(f"STOP: block reason is not a structured Success proof failure: {detail}")
        return "BLOCKED_OTHER_REASON"

    claim_state = _latest_status(con, "job_claim_trials", room, job_id)
    if claim_state != "SENT":
        print(f"STOP: CLAIM state is {claim_state or 'missing'}, not SENT")
        return "CLAIM_NOT_SENT"

    delivery_state = _latest_status(con, "job_delivery_trials", room, job_id)
    if delivery_state in {"SENT", "UNCERTAIN", "RESERVED"}:
        print(f"STOP: delivery is terminal state {delivery_state}; do not retry")
        return delivery_state

    con.executescript(_RETRY_SCHEMA)
    existing = con.execute(
        """
        SELECT attempted_at FROM job_success_human_retries_v2
        WHERE room=? AND job_id=? AND content_hash=?
        """,
        (
            str(tracked["room"]),
            str(tracked["job_id"]),
            str(tracked["content_hash"]),
        ),
    ).fetchone()
    if existing is not None:
        print("STOP: this JOB already used its one V2 local Success repair retry")
        return "RETRY_ALREADY_USED"

    # Consume the one-shot retry before running the pipeline so a crash cannot
    # accidentally create a repair loop.
    con.execute(
        """
        INSERT INTO job_success_human_retries_v2(
          room,job_id,content_hash,attempted_at,original_detail
        ) VALUES(?,?,?,?,?)
        """,
        (
            str(tracked["room"]),
            str(tracked["job_id"]),
            str(tracked["content_hash"]),
            utc_now(),
            detail[:2000],
        ),
    )

    changed = con.execute(
        """
        UPDATE job_auto_orchestrator
        SET pipeline_state='WAITING_POSTCLAIM',
            detail='human-invoked V2 local Success repair retry',
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
        print("STOP: blocked row changed before Success retry; nothing was modified")
        return "STATE_CHANGED"
    con.commit()

    retry_cfg = dict(cfg)
    retry_cfg["job_success_repair_attempts"] = 1

    print("=== LOCAL SUCCESS REPAIR RETRY V2 ===")
    print("Structured Success BLOCKに対し、literal named-item proof補完とfrozen-contract repairをこのJOBで1回だけ有効化します。")
    print("CLAIM/DELIVERは送信しません。Groundingと最終Success verifierは従来どおりfail-closedです。")

    processed = process_sent_claims(
        con,
        retry_cfg,
        room=room,
        limit=2,
        pipeline_runner=_named_success_pipeline,
    )

    target_state = ""
    for item in processed:
        print(f"Pipeline | job={item.get('job_id','')} state={item.get('state','')}")
        if str(item.get("job_id", "")) == job_id:
            target_state = str(item.get("state", ""))

    row = _latest_auto(con, room, job_id)
    if row is not None:
        target_state = str(row["pipeline_state"] or target_state)
        if target_state == "BLOCKED":
            print(f"reason={row['detail']}")

    if target_state == "DELIVERY_READY":
        print("READY: local Success repair passed and delivery is prepared for separate human review.")
    return target_state or "WAITING"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resume one structured Success block with one V2 local repair attempt"
    )
    parser.add_argument("job_id")
    parser.add_argument("--room", default="kibble")
    parser.add_argument("--config", default="technoscout.config.json")
    args = parser.parse_args()

    cfg = _runtime_defaults(load_config(args.config))
    con = connect(database_path(cfg))
    try:
        state = resume_success_block(con, cfg, args.job_id, room=args.room)
    finally:
        con.close()
    print(f"Job Success Resume | final_state={state} job={args.job_id}")


if __name__ == "__main__":
    main()
