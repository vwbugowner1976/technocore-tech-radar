#!/usr/bin/env python3
"""Read-only gate from semantic refinement to a manual Kibble claim trial.

This is intentionally separate from ``job_progress_gate.py`` so Qwen refinement
cannot silently change the original deterministic gate. A SAFE_FIT refinement is
only advisory evidence. Before READY, the exact persisted JOB must still pass the
existing issuer thresholds and a fresh live OPEN revalidation.

This module never claims a job, sends a message, executes job content, spends
FLOP/tokens, touches a wallet, or changes TechnoScout autonomy.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from issuer_reputation import issuer_reputation
from job_candidate_refiner import ensure_refiner_schema
from job_live_revalidator import live_revalidate_job_export_aware
from job_progress_gate import (
    _issuer_meets_gate,
    _thresholds,
    gate_metrics,
)
from job_shadow import ensure_job_shadow_schema
from technoscout.db import connect
from technoscout_cli import database_path, load_config


@dataclass(frozen=True)
class RefinedJobGateResult:
    state: str
    ready_for_manual_claim_trial: bool
    reason: str
    metrics: dict[str, int]
    candidates: tuple[dict[str, Any], ...] = ()


def _refined_thresholds(cfg: dict[str, Any]) -> dict[str, int]:
    return {
        "relevance": int(cfg.get("job_refined_gate_min_relevance", 70)),
        "technical_fit": int(cfg.get("job_refined_gate_min_fit", 75)),
        "confidence": int(cfg.get("job_refined_gate_min_confidence", 80)),
        "max_age_seconds": int(cfg.get("job_refined_gate_max_age_seconds", 3600)),
    }


def refined_candidate_rows(
    con: Any,
    cfg: dict[str, Any],
    *,
    limit: int = 5,
    max_age_seconds: int | None = None,
) -> list[dict[str, Any]]:
    """Return recent SAFE_FIT refinements with strong issuer evidence.

    The deterministic Job Scout class is deliberately not required to be FIT:
    this bridge exists specifically to recover semantic false negatives such as
    lexical NOT_RELEVANT rows. NEEDS_TOOL/NEEDS_COMPUTE refinements never pass.
    """
    ensure_job_shadow_schema(con)
    ensure_refiner_schema(con)
    base_thresholds = _thresholds(cfg)
    refined = _refined_thresholds(cfg)
    age = max(
        60,
        int(
            max_age_seconds
            if max_age_seconds is not None
            else refined["max_age_seconds"]
        ),
    )
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=age)
    ).isoformat(timespec="seconds")

    rows = con.execute(
        """
        SELECT j.room,j.job_id,j.job_seq,j.issuer_did,j.signed_identity,
               j.job_type,j.content_hash,j.lifecycle,j.fit_class AS deterministic_class,
               j.relevance AS deterministic_relevance,j.technical_fit AS deterministic_fit,
               j.confidence AS deterministic_confidence,j.effort AS deterministic_effort,
               r.refined_at,r.decision,r.relevance AS refined_relevance,
               r.technical_fit AS refined_fit,r.confidence AS refined_confidence,
               r.effort AS refined_effort,r.reason AS refined_reason
        FROM job_shadow_candidates AS j
        JOIN job_candidate_refinements AS r
          ON r.room=j.room AND r.job_id=j.job_id AND r.content_hash=j.content_hash
        WHERE j.lifecycle='OPEN'
          AND j.signed_identity=1
          AND j.issuer_did<>''
          AND r.decision='SAFE_FIT'
          AND r.refined_at>=?
          AND r.relevance>=?
          AND r.technical_fit>=?
          AND r.confidence>=?
        ORDER BY r.confidence DESC,r.technical_fit DESC,r.relevance DESC,j.job_seq DESC
        LIMIT ?
        """,
        (
            cutoff,
            refined["relevance"],
            refined["technical_fit"],
            refined["confidence"],
            max(1, min(200, int(limit) * 5)),
        ),
    ).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        rep = issuer_reputation(con, str(row["issuer_did"]))
        if not _issuer_meets_gate(rep, base_thresholds):
            continue
        item = {key: row[key] for key in row.keys()}
        item["issuer_reputation"] = rep
        result.append(item)
        if len(result) >= max(1, int(limit)):
            break
    return result


def evaluate_refined_gate(
    con: Any,
    cfg: dict[str, Any],
    *,
    live: bool = False,
    candidate_limit: int = 5,
    max_age_seconds: int | None = None,
    revalidator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> RefinedJobGateResult:
    metrics = gate_metrics(con)
    base_thresholds = _thresholds(cfg)
    baseline_keys = ("observed_jobs", "open_jobs", "closed_jobs", "signed_issuers")
    missing = [key for key in baseline_keys if metrics[key] < base_thresholds[key]]
    if missing:
        return RefinedJobGateResult(
            "COLLECTING",
            False,
            "insufficient baseline evidence: "
            + ", ".join(
                f"{key}={metrics[key]}/{base_thresholds[key]}" for key in missing
            ),
            metrics,
        )

    candidates = refined_candidate_rows(
        con,
        cfg,
        limit=max(1, int(candidate_limit)),
        max_age_seconds=max_age_seconds,
    )
    metrics = dict(metrics)
    metrics["eligible_refined_candidates"] = len(candidates)
    if not candidates:
        return RefinedJobGateResult(
            "WAITING_FOR_REFINED_CANDIDATE",
            False,
            "baseline is sufficient, but no recent SAFE_FIT refinement passes semantic and issuer thresholds",
            metrics,
        )

    compact = []
    for item in candidates:
        compact.append(
            {
                "room": item["room"],
                "job_id": item["job_id"],
                "job_seq": item["job_seq"],
                "issuer_did": item["issuer_did"],
                "job_type": item["job_type"],
                "content_hash": item["content_hash"],
                "deterministic_class": item["deterministic_class"],
                "refined_at": item["refined_at"],
                "refined_relevance": item["refined_relevance"],
                "refined_fit": item["refined_fit"],
                "refined_confidence": item["refined_confidence"],
                "refined_effort": item["refined_effort"],
                "refined_reason": item["refined_reason"],
                "issuer_reputation": item["issuer_reputation"],
            }
        )

    if not live:
        return RefinedJobGateResult(
            "NEEDS_LIVE_REVALIDATION",
            False,
            "semantic SAFE_FIT candidate exists; live OPEN revalidation is still required before any manual trial",
            metrics,
            tuple(compact),
        )

    checked: list[dict[str, Any]] = []
    inconclusive = False
    for source, view in zip(candidates, compact):
        try:
            check = (
                revalidator(cfg, source)
                if revalidator is not None
                else live_revalidate_job_export_aware(cfg, source)
            )
        except Exception as exc:
            check = {
                "state": "INCONCLUSIVE_ERROR",
                "lifecycle": "UNKNOWN",
                "pages": 0,
                "messages": 0,
                "error": type(exc).__name__,
            }
        item = dict(view)
        item["live"] = check
        checked.append(item)
        if check.get("state") == "OPEN_CONFIRMED":
            return RefinedJobGateResult(
                "READY_FOR_MANUAL_CLAIM_TRIAL",
                True,
                "one semantic SAFE_FIT candidate passed issuer evidence and exact live OPEN revalidation",
                metrics,
                tuple(checked),
            )
        state = str(check.get("state", ""))
        if (
            state.startswith("INCONCLUSIVE")
            or state in {"JOB_NOT_FOUND", "JOB_NOT_RETAINED"}
        ):
            inconclusive = True

    return RefinedJobGateResult(
        "LIVE_CHECK_INCONCLUSIVE" if inconclusive else "NO_LIVE_OPEN_CANDIDATE",
        False,
        (
            "live revalidation was incomplete/unavailable; fail closed and retry later"
            if inconclusive
            else "all refined candidates were already claimed/closed or mismatched when rechecked"
        ),
        metrics,
        tuple(checked),
    )


def print_result(result: RefinedJobGateResult) -> None:
    print(
        f"Refined Job Gate | state={result.state} "
        f"ready={'yes' if result.ready_for_manual_claim_trial else 'no'}"
    )
    print(f"reason={result.reason}")
    print(
        "metrics="
        + " ".join(f"{key}:{value}" for key, value in sorted(result.metrics.items()))
    )
    for item in result.candidates:
        rep = item.get("issuer_reputation", {})
        print(
            f"  {item['job_id']} seq={item['job_seq']} type={item['job_type']} "
            f"det={item['deterministic_class']} -> SAFE_FIT "
            f"rel={item['refined_relevance']} fit={item['refined_fit']} "
            f"conf={item['refined_confidence']} issuer_score={rep.get('score',0)} "
            f"issuer_attested={rep.get('attested_jobs',0)}"
        )
        live = item.get("live")
        if live:
            print(
                f"    live={live.get('state')} lifecycle={live.get('lifecycle')} "
                f"pages={live.get('pages',0)} messages={live.get('messages',0)} "
                f"source={live.get('source','-')}"
            )
    print(
        "NOTE: READY_FOR_MANUAL_CLAIM_TRIAL still does not claim anything. "
        "The next step must remain a separate one-job, human-approved, one-use permit path."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only gate for semantically refined Job Scout candidates"
    )
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--candidate-limit", type=int, default=5)
    parser.add_argument("--max-age-seconds", type=int, default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    con = connect(database_path(cfg))
    try:
        result = evaluate_refined_gate(
            con,
            cfg,
            live=bool(args.live),
            candidate_limit=max(1, min(20, int(args.candidate_limit))),
            max_age_seconds=args.max_age_seconds,
        )
        print_result(result)
    finally:
        con.close()


if __name__ == "__main__":
    main()
