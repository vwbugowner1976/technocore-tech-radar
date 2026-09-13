#!/usr/bin/env python3
"""One-shot read-only watcher for fresh Kibble semantic candidates.

The watcher closes the timing gap between Job Shadow discovery and manual semantic
refinement. It only considers fresh signed self-contained candidates from credible
issuers that have not already been refined for the same content hash. Before loading
Qwen, it performs export-aware live OPEN revalidation. Only an OPEN_CONFIRMED job is
sent to the local semantic refiner.

This module never CLAIMs, sends, executes job content, opens job URLs, spends FLOP,
touches wallets/credentials, or changes TechnoScout autonomy. A SAFE_FIT result can
only produce read-only READY evidence through the existing Refined Job Gate.

The CLI entrypoint then hands that READY evidence to job_auto_orchestrator, which may
prepare a local CLAIM preview and may process already-SENT claims locally. Neither
module approves or sends CLAIM/DELIVER writes.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable

from job_auto_orchestrator import run_once as run_auto_once
from job_candidate_refiner import (
    _runtime_defaults,
    ensure_refiner_schema,
    recent_near_miss_rows,
    refine_candidate,
    store_refinement,
)
from job_live_revalidator import live_revalidate_job_export_aware
from job_refined_gate import evaluate_refined_gate
from technoscout.db import connect
from technoscout.llm_backend import create_llm_backend
from technoscout_cli import database_path, load_config


TERMINAL_LIVE_FAILURES = {"NOT_OPEN", "JOB_NOT_RETAINED", "JOB_MISMATCH"}


def pending_candidate_rows(
    con: Any,
    cfg: dict[str, Any],
    *,
    limit: int = 1,
    max_age_seconds: int = 900,
) -> list[dict[str, Any]]:
    """Return fresh eligible candidates with no refinement for this exact content."""
    ensure_refiner_schema(con)
    scan_limit = max(10, min(100, max(1, int(limit)) * 20))
    rows = recent_near_miss_rows(
        con,
        cfg,
        limit=scan_limit,
        max_age_seconds=max(60, int(max_age_seconds)),
    )
    result: list[dict[str, Any]] = []
    for item in rows:
        existing = con.execute(
            """
            SELECT 1
            FROM job_candidate_refinements
            WHERE room=? AND job_id=? AND content_hash=?
            LIMIT 1
            """,
            (str(item["room"]), str(item["job_id"]), str(item["content_hash"])),
        ).fetchone()
        if existing is not None:
            continue
        result.append(item)
        if len(result) >= max(1, int(limit)):
            break
    return result


def _terminal_refinement(state: str) -> dict[str, Any]:
    return {
        "decision": "INCONCLUSIVE",
        "relevance": 0,
        "technical_fit": 0,
        "confidence": 100,
        "effort": "unknown",
        "reason": f"live precheck failed closed: {state}",
    }


def run_once(
    con: Any,
    cfg: dict[str, Any],
    *,
    limit: int = 1,
    max_age_seconds: int = 900,
    revalidator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    refiner: Callable[[dict[str, Any], dict[str, Any], Any, str], dict[str, Any]] | None = None,
    llm_factory: Callable[[dict[str, Any]], Any] | None = None,
    gate_evaluator: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Refine at most ``limit`` new candidates, loading the LLM only when needed."""
    ensure_refiner_schema(con)
    candidates = pending_candidate_rows(
        con,
        cfg,
        limit=max(1, int(limit)),
        max_age_seconds=max(60, int(max_age_seconds)),
    )
    summary: dict[str, Any] = {
        "candidates": len(candidates),
        "refined": 0,
        "safe_fit": 0,
        "terminal_skips": 0,
        "retry_later": 0,
        "ready": False,
        "ready_job_id": "",
        "rows": [],
    }
    if not candidates:
        return summary

    model = str(cfg.get("research_model") or cfg.get("triage_model") or "").strip()
    if not model:
        raise RuntimeError("research_model or triage_model must be configured")

    validate = revalidator or live_revalidate_job_export_aware
    run_refiner = refiner or refine_candidate
    make_llm = llm_factory or create_llm_backend
    eval_gate = gate_evaluator or evaluate_refined_gate
    llm = None
    try:
        for candidate in candidates:
            try:
                live = validate(cfg, candidate)
            except Exception as exc:
                summary["retry_later"] += 1
                summary["rows"].append(
                    {
                        "job_id": candidate["job_id"],
                        "state": "RETRY_LATER",
                        "live": f"INCONCLUSIVE_ERROR:{type(exc).__name__}",
                    }
                )
                continue

            live_state = str(live.get("state", "INCONCLUSIVE"))
            if live_state != "OPEN_CONFIRMED":
                if live_state in TERMINAL_LIVE_FAILURES:
                    store_refinement(con, candidate, _terminal_refinement(live_state))
                    summary["terminal_skips"] += 1
                    state = "TERMINAL_SKIP"
                else:
                    summary["retry_later"] += 1
                    state = "RETRY_LATER"
                summary["rows"].append(
                    {"job_id": candidate["job_id"], "state": state, "live": live_state}
                )
                continue

            if llm is None:
                llm = make_llm(cfg)
            result = run_refiner(cfg, candidate, llm, model)
            store_refinement(con, candidate, result)
            summary["refined"] += 1
            if result.get("decision") == "SAFE_FIT":
                summary["safe_fit"] += 1
                gate = eval_gate(
                    con,
                    cfg,
                    live=True,
                    candidate_limit=max(1, min(5, int(limit))),
                )
                if bool(getattr(gate, "ready_for_manual_claim_trial", False)):
                    summary["ready"] = True
                    if getattr(gate, "candidates", ()):
                        last = gate.candidates[-1]
                        summary["ready_job_id"] = str(last.get("job_id", candidate["job_id"]))
                    else:
                        summary["ready_job_id"] = str(candidate["job_id"])
            summary["rows"].append(
                {
                    "job_id": candidate["job_id"],
                    "state": "REFINED",
                    "live": live_state,
                    "decision": result.get("decision", "INCONCLUSIVE"),
                    "relevance": int(result.get("relevance", 0)),
                    "technical_fit": int(result.get("technical_fit", 0)),
                    "confidence": int(result.get("confidence", 0)),
                }
            )
    finally:
        if llm is not None:
            llm.close()
    return summary


def print_summary(summary: dict[str, Any]) -> None:
    print(
        "Job Refined Watch | "
        f"candidates={summary['candidates']} refined={summary['refined']} "
        f"safe_fit={summary['safe_fit']} terminal_skips={summary['terminal_skips']} "
        f"retry_later={summary['retry_later']} ready={'yes' if summary['ready'] else 'no'}"
    )
    for row in summary["rows"]:
        line = f"  {row['job_id']} state={row['state']} live={row['live']}"
        if row.get("decision"):
            line += (
                f" decision={row['decision']} rel={row['relevance']} "
                f"fit={row['technical_fit']} conf={row['confidence']}"
            )
        print(line)
    if summary["ready"]:
        print(
            f"READY candidate={summary['ready_job_id']} — evidence only; "
            "no CLAIM was sent."
        )
    print("NOTE: this watcher is read-only against Technocore and never claims work.")


def main() -> None:
    parser = argparse.ArgumentParser(description="One-shot fresh Job Candidate Refiner watcher")
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--max-age-seconds", type=int, default=900)
    args = parser.parse_args()

    cfg = _runtime_defaults(load_config(args.config))
    con = connect(database_path(cfg))
    try:
        summary = run_once(
            con,
            cfg,
            limit=max(1, min(3, int(args.limit))),
            max_age_seconds=max(60, int(args.max_age_seconds)),
        )
        print_summary(summary)

        auto = run_auto_once(
            con,
            cfg,
            ready_job_id=str(summary["ready_job_id"] if summary["ready"] else ""),
            room=str(cfg.get("job_shadow_room", "kibble")),
            limit=2,
        )
        prepared = auto.get("prepared")
        if prepared:
            print(
                f"Auto Orchestrator | prepare={prepared.get('state','UNKNOWN')} "
                f"job={prepared.get('job_id','')}"
            )
        for item in auto.get("processed", []):
            print(f"Auto Orchestrator | job={item['job_id']} state={item['state']}")
        notices = auto["notifications"]
        if any(int(v) for v in notices.values()):
            print(
                "Auto Orchestrator | notifications "
                f"claim_ready={notices['claim_ready']} "
                f"delivery_ready={notices['delivery_ready']} "
                f"blocked={notices['blocked']} failed={notices['failed']}"
            )
        print("Auto Orchestrator | STOP: no CLAIM or DELIVER was approved or sent.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
