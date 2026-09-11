#!/usr/bin/env python3
"""Refresh one existing OPEN self-contained Job Scout candidate safely.

This is a read-only bridge for candidates whose semantic refinement expired while
the Job Shadow row still says OPEN. It first requires the persisted row to remain
eligible, then performs export-aware live OPEN revalidation, then re-runs the local
semantic refiner and updates only structured refinement metadata.

It never claims a job, sends a message, executes job content, opens URLs, spends
FLOP/tokens, touches a wallet, or changes TechnoScout autonomy.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable

from issuer_reputation import issuer_reputation
from job_candidate_refiner import (
    _runtime_defaults,
    ensure_refiner_schema,
    refine_candidate,
    store_refinement,
)
from job_live_revalidator import live_revalidate_job_export_aware
from job_progress_gate import _issuer_meets_gate, _thresholds
from job_shadow import ensure_job_shadow_schema
from job_shadow_policy import SELF_CONTAINED_TYPES
from technoscout.db import connect
from technoscout.llm_backend import create_llm_backend
from technoscout_cli import database_path, load_config


def candidate_by_job_id(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    *,
    room: str = "kibble",
) -> tuple[dict[str, Any] | None, str]:
    """Return one persisted candidate only if it still passes local safety gates."""
    ensure_job_shadow_schema(con)
    row = con.execute(
        """
        SELECT room,job_id,first_seen_at,last_seen_at,job_seq,issuer_did,
               signed_identity,job_type,content_hash,lifecycle,fit_class,
               relevance,technical_fit,confidence,effort,reason
        FROM job_shadow_candidates
        WHERE room=? AND job_id=?
        LIMIT 1
        """,
        (str(room), str(job_id)),
    ).fetchone()
    if row is None:
        return None, "job is not present in Job Shadow memory"
    item = {key: row[key] for key in row.keys()}
    if str(item["lifecycle"]) != "OPEN":
        return None, f"job lifecycle is {item['lifecycle']}, not OPEN"
    if int(item["signed_identity"] or 0) != 1 or not str(item["issuer_did"]):
        return None, "issuer is not a persisted signed identity"
    if str(item["job_type"]).lower() not in SELF_CONTAINED_TYPES:
        return None, "job type is not self-contained"
    if str(item["fit_class"]) not in {"FIT", "NOT_RELEVANT"}:
        return None, f"deterministic class {item['fit_class']} is not eligible for semantic refresh"

    rep = issuer_reputation(con, str(item["issuer_did"]))
    if not _issuer_meets_gate(rep, _thresholds(cfg)):
        return None, "issuer does not meet current Job Progress Gate evidence thresholds"
    item["issuer_reputation"] = rep
    return item, "eligible"


def refresh_candidate(
    con: Any,
    cfg: dict[str, Any],
    candidate: dict[str, Any],
    llm: Any,
    model: str,
    *,
    revalidator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    evaluator: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Live-check first, then refresh semantic metadata only when still OPEN."""
    check = (
        revalidator(cfg, candidate)
        if revalidator is not None
        else live_revalidate_job_export_aware(cfg, candidate)
    )
    if check.get("state") != "OPEN_CONFIRMED":
        return {
            "state": "NOT_REFRESHED",
            "live": check,
            "reason": f"live OPEN revalidation failed: {check.get('state','UNKNOWN')}",
        }

    result = refine_candidate(cfg, candidate, llm, model, evaluator=evaluator)
    store_refinement(con, candidate, result)
    return {
        "state": "REFRESHED",
        "live": check,
        "refinement": result,
        "reason": "semantic refinement refreshed after exact live OPEN confirmation",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh one existing OPEN Job Scout candidate after live verification"
    )
    parser.add_argument("job_id")
    parser.add_argument("--room", default="kibble")
    parser.add_argument("--config", default="technoscout.config.json")
    args = parser.parse_args()

    cfg = _runtime_defaults(load_config(args.config))
    con = connect(database_path(cfg))
    ensure_refiner_schema(con)
    candidate, reason = candidate_by_job_id(con, cfg, args.job_id, room=args.room)
    if candidate is None:
        print(f"Job Refinement Refresh | state=BLOCKED job={args.job_id}")
        print(f"reason={reason}")
        con.close()
        return

    model = str(cfg.get("research_model") or cfg.get("triage_model") or "").strip()
    if not model:
        con.close()
        raise SystemExit("research_model or triage_model must be configured")

    llm = create_llm_backend(cfg)
    try:
        outcome = refresh_candidate(con, cfg, candidate, llm, model)
    finally:
        llm.close()
        con.close()

    print(
        f"Job Refinement Refresh | state={outcome['state']} job={args.job_id} "
        f"live={outcome['live'].get('state','UNKNOWN')}"
    )
    if outcome.get("refinement"):
        result = outcome["refinement"]
        print(
            f"decision={result['decision']} rel={result['relevance']} "
            f"fit={result['technical_fit']} conf={result['confidence']} effort={result['effort']}"
        )
        print(f"reason={result['reason']}")
    else:
        print(f"reason={outcome['reason']}")
    print("NOTE: this command does not claim or send anything.")


if __name__ == "__main__":
    main()
