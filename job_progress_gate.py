#!/usr/bin/env python3
"""Evidence gate for the first manually approved Kibble claim trial.

This module is deliberately read-only. READY_FOR_MANUAL_CLAIM_TRIAL means only
that one candidate has enough local evidence and has just been revalidated as
OPEN on Technocore. It does NOT claim the job, send a message, spend FLOP, run
untrusted work, or change any autonomous behavior.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Callable

from issuer_reputation import issuer_reputation
from job_shadow import (
    content_hash,
    ensure_job_shadow_schema,
    parse_kibble_message,
    sender_of,
)
from technoscout.common import room_messages, seq_of, technocore_json
from technoscout.db import connect
from technoscout_cli import database_path, load_config


LIVE_LIFECYCLE = {
    "CLAIM": "CLAIMED",
    "RESULT": "DELIVERED",
    "DELIVER": "DELIVERED",
    "ATTEST": "ATTESTED",
    "WITNESS": "ATTESTED",
}
LIVE_RANK = {
    "OPEN": 0,
    "CLAIMED": 1,
    "DELIVERED": 2,
    "ATTESTED": 3,
}


@dataclass(frozen=True)
class JobGateResult:
    state: str
    ready_for_manual_claim_trial: bool
    reason: str
    metrics: dict[str, int]
    thresholds: dict[str, int]
    candidates: tuple[dict[str, Any], ...] = ()


def _thresholds(cfg: dict[str, Any]) -> dict[str, int]:
    return {
        "observed_jobs": int(cfg.get("job_gate_min_observed_jobs", 100)),
        "open_jobs": int(cfg.get("job_gate_min_open_jobs", 20)),
        "closed_jobs": int(cfg.get("job_gate_min_closed_jobs", 50)),
        "signed_issuers": int(cfg.get("job_gate_min_signed_issuers", 5)),
        "candidate_relevance": int(cfg.get("job_gate_min_candidate_relevance", 50)),
        "candidate_fit": int(cfg.get("job_gate_min_candidate_fit", 60)),
        "candidate_confidence": int(cfg.get("job_gate_min_candidate_confidence", 75)),
        "issuer_jobs": int(cfg.get("job_gate_min_issuer_jobs", 3)),
        "issuer_closed": int(cfg.get("job_gate_min_issuer_closed", 2)),
        "issuer_completed": int(cfg.get("job_gate_min_issuer_completed", 2)),
        "issuer_attested": int(cfg.get("job_gate_min_issuer_attested", 1)),
        "issuer_completion_rate_percent": int(
            cfg.get("job_gate_min_issuer_completion_rate_percent", 50)
        ),
    }


def gate_metrics(con: Any) -> dict[str, int]:
    ensure_job_shadow_schema(con)
    row = con.execute(
        """
        SELECT
          COUNT(*) AS observed_jobs,
          SUM(CASE WHEN lifecycle='OPEN' THEN 1 ELSE 0 END) AS open_jobs,
          SUM(CASE WHEN lifecycle IN ('CLAIMED','DELIVERED','ATTESTED') THEN 1 ELSE 0 END) AS closed_jobs,
          SUM(CASE WHEN lifecycle='OPEN' AND signed_identity=1 THEN 1 ELSE 0 END) AS open_signed_jobs,
          SUM(CASE WHEN lifecycle='OPEN' AND fit_class='FIT' THEN 1 ELSE 0 END) AS open_fit_jobs,
          COUNT(DISTINCT CASE WHEN signed_identity=1 AND issuer_did<>'' THEN issuer_did END) AS signed_issuers
        FROM job_shadow_candidates
        """
    ).fetchone()
    return {
        "observed_jobs": int(row["observed_jobs"] or 0),
        "open_jobs": int(row["open_jobs"] or 0),
        "closed_jobs": int(row["closed_jobs"] or 0),
        "open_signed_jobs": int(row["open_signed_jobs"] or 0),
        "open_fit_jobs": int(row["open_fit_jobs"] or 0),
        "signed_issuers": int(row["signed_issuers"] or 0),
    }


def _issuer_meets_gate(rep: dict[str, Any], thresholds: dict[str, int]) -> bool:
    return (
        int(rep["total_jobs"]) >= thresholds["issuer_jobs"]
        and int(rep["closed_jobs"]) >= thresholds["issuer_closed"]
        and int(rep["completed_jobs"]) >= thresholds["issuer_completed"]
        and int(rep["attested_jobs"]) >= thresholds["issuer_attested"]
        and int(rep["completion_rate_percent"])
        >= thresholds["issuer_completion_rate_percent"]
    )


def candidate_rows(
    con: Any,
    cfg: dict[str, Any],
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return local FIT candidates that pass deterministic issuer thresholds.

    NEEDS_TOOL and NEEDS_COMPUTE are intentionally excluded from the first
    claim trial. They require a separate Tool Planner / compute-spend safety
    boundary before they can become executable work.
    """
    ensure_job_shadow_schema(con)
    thresholds = _thresholds(cfg)
    rows = con.execute(
        """
        SELECT room,job_id,job_seq,issuer_did,signed_identity,job_type,
               content_hash,lifecycle,fit_class,relevance,technical_fit,
               confidence,effort,required_capabilities_json,reason,summary
        FROM job_shadow_candidates
        WHERE lifecycle='OPEN'
          AND signed_identity=1
          AND issuer_did<>''
          AND fit_class='FIT'
          AND relevance>=?
          AND technical_fit>=?
          AND confidence>=?
        ORDER BY technical_fit DESC, confidence DESC, relevance DESC, job_seq DESC
        LIMIT ?
        """,
        (
            thresholds["candidate_relevance"],
            thresholds["candidate_fit"],
            thresholds["candidate_confidence"],
            max(1, min(200, int(limit) * 5)),
        ),
    ).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        rep = issuer_reputation(con, str(row["issuer_did"]))
        if not _issuer_meets_gate(rep, thresholds):
            continue
        item = {key: row[key] for key in row.keys()}
        item["issuer_reputation"] = rep
        result.append(item)
        if len(result) >= max(1, int(limit)):
            break
    return result


def live_revalidate_job(
    cfg: dict[str, Any],
    candidate: dict[str, Any],
    *,
    fetcher: Callable[[dict[str, Any], str, dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Boundedly verify that the exact persisted JOB is still OPEN.

    Positive readiness requires seeing the exact original JOB (same seq, DID,
    and content hash), reaching the end of the available since-window, and
    finding no later CLAIM/DELIVER/RESULT/ATTEST/WITNESS for its job id.
    Any ambiguity is fail-closed.
    """
    read = fetcher or technocore_json
    room = str(candidate["room"])
    job_id = str(candidate["job_id"])
    job_seq = int(candidate["job_seq"])
    issuer_did = str(candidate["issuer_did"])
    expected_hash = str(candidate["content_hash"])
    page_limit = max(20, min(200, int(cfg.get("job_gate_live_page_limit", 200))))
    max_pages = max(1, min(20, int(cfg.get("job_gate_live_max_pages", 6))))

    cursor = max(0, job_seq - 1)
    saw_exact_job = False
    lifecycle = "OPEN"
    pages = 0
    messages_seen = 0

    while pages < max_pages:
        payload = read(
            cfg,
            f"/r/{room}",
            {"format": "json", "since": cursor, "limit": page_limit},
        )
        messages = sorted(room_messages(payload), key=seq_of)
        pages += 1
        messages_seen += len(messages)

        if not messages:
            if saw_exact_job:
                return {
                    "state": "OPEN_CONFIRMED",
                    "lifecycle": "OPEN",
                    "pages": pages,
                    "messages": messages_seen,
                }
            return {
                "state": "JOB_NOT_FOUND",
                "lifecycle": lifecycle,
                "pages": pages,
                "messages": messages_seen,
            }

        max_seq = cursor
        for message in messages:
            message_seq = seq_of(message)
            max_seq = max(max_seq, message_seq)
            parsed = parse_kibble_message(
                message.get("text", message.get("message", ""))
            )
            if not parsed or parsed.get("job_id") != job_id:
                continue

            if parsed["verb"] == "JOB":
                if (
                    message_seq == job_seq
                    and sender_of(message) == issuer_did
                    and content_hash(parsed) == expected_hash
                ):
                    saw_exact_job = True
                else:
                    return {
                        "state": "JOB_MISMATCH",
                        "lifecycle": lifecycle,
                        "pages": pages,
                        "messages": messages_seen,
                    }
                continue

            if message_seq <= job_seq:
                continue
            candidate_state = LIVE_LIFECYCLE.get(parsed["verb"])
            if candidate_state and LIVE_RANK[candidate_state] > LIVE_RANK[lifecycle]:
                lifecycle = candidate_state

        if lifecycle != "OPEN":
            return {
                "state": "NOT_OPEN",
                "lifecycle": lifecycle,
                "pages": pages,
                "messages": messages_seen,
            }

        # A short page means we caught up to the current end of this since-view.
        # Readiness is allowed only if the exact original JOB was also observed.
        if len(messages) < page_limit:
            return {
                "state": "OPEN_CONFIRMED" if saw_exact_job else "JOB_NOT_FOUND",
                "lifecycle": "OPEN",
                "pages": pages,
                "messages": messages_seen,
            }

        if max_seq <= cursor:
            return {
                "state": "INCONCLUSIVE",
                "lifecycle": lifecycle,
                "pages": pages,
                "messages": messages_seen,
            }
        cursor = max_seq

    return {
        "state": "INCONCLUSIVE_TRUNCATED",
        "lifecycle": lifecycle,
        "pages": pages,
        "messages": messages_seen,
    }


def evaluate_gate(
    con: Any,
    cfg: dict[str, Any],
    *,
    live: bool = False,
    fetcher: Callable[[dict[str, Any], str, dict[str, Any]], Any] | None = None,
    candidate_limit: int = 5,
) -> JobGateResult:
    metrics = gate_metrics(con)
    thresholds = _thresholds(cfg)
    baseline_keys = ("observed_jobs", "open_jobs", "closed_jobs", "signed_issuers")
    missing = [
        key for key in baseline_keys if metrics[key] < thresholds[key]
    ]
    if missing:
        reason = "insufficient baseline evidence: " + ", ".join(
            f"{key}={metrics[key]}/{thresholds[key]}" for key in missing
        )
        return JobGateResult("COLLECTING", False, reason, metrics, thresholds)

    candidates = candidate_rows(con, cfg, limit=max(1, int(candidate_limit)))
    metrics = dict(metrics)
    metrics["eligible_local_candidates"] = len(candidates)
    if not candidates:
        return JobGateResult(
            "WAITING_FOR_SAFE_CANDIDATE",
            False,
            "baseline is sufficient, but no OPEN signed FIT job passes candidate and issuer thresholds",
            metrics,
            thresholds,
        )

    if not live:
        compact = tuple(
            {
                "job_id": item["job_id"],
                "room": item["room"],
                "job_seq": item["job_seq"],
                "issuer_did": item["issuer_did"],
                "job_type": item["job_type"],
                "technical_fit": item["technical_fit"],
                "confidence": item["confidence"],
                "issuer_reputation": item["issuer_reputation"],
            }
            for item in candidates
        )
        return JobGateResult(
            "NEEDS_LIVE_REVALIDATION",
            False,
            "local evidence has candidates; run with --live to revalidate OPEN state before any manual trial",
            metrics,
            thresholds,
            compact,
        )

    checked: list[dict[str, Any]] = []
    inconclusive = False
    for item in candidates:
        check = live_revalidate_job(cfg, item, fetcher=fetcher)
        compact = {
            "job_id": item["job_id"],
            "room": item["room"],
            "job_seq": item["job_seq"],
            "issuer_did": item["issuer_did"],
            "job_type": item["job_type"],
            "technical_fit": item["technical_fit"],
            "confidence": item["confidence"],
            "issuer_reputation": item["issuer_reputation"],
            "live": check,
        }
        checked.append(compact)
        if check["state"] == "OPEN_CONFIRMED":
            return JobGateResult(
                "READY_FOR_MANUAL_CLAIM_TRIAL",
                True,
                "one candidate passed local evidence, issuer reputation, and exact live OPEN revalidation",
                metrics,
                thresholds,
                tuple(checked),
            )
        if str(check["state"]).startswith("INCONCLUSIVE") or check["state"] == "JOB_NOT_FOUND":
            inconclusive = True

    return JobGateResult(
        "LIVE_CHECK_INCONCLUSIVE" if inconclusive else "NO_LIVE_OPEN_CANDIDATE",
        False,
        (
            "live revalidation was incomplete; fail closed and try later"
            if inconclusive
            else "all locally eligible candidates were already claimed/closed or mismatched when rechecked"
        ),
        metrics,
        thresholds,
        tuple(checked),
    )


def print_result(result: JobGateResult) -> None:
    print(
        f"Job Progress Gate | state={result.state} "
        f"ready={'yes' if result.ready_for_manual_claim_trial else 'no'}"
    )
    print(f"reason={result.reason}")
    print(
        "metrics="
        + " ".join(f"{key}:{value}" for key, value in sorted(result.metrics.items()))
    )
    for item in result.candidates:
        rep = item.get("issuer_reputation", {})
        live = item.get("live")
        print(
            f"  {item['job_id']} room={item['room']} seq={item['job_seq']} "
            f"type={item['job_type']} fit={item['technical_fit']} conf={item['confidence']} "
            f"issuer_score={rep.get('score',0)} issuer_jobs={rep.get('total_jobs',0)} "
            f"issuer_attested={rep.get('attested_jobs',0)}"
        )
        if live:
            print(
                f"    live={live['state']} lifecycle={live['lifecycle']} "
                f"pages={live['pages']} messages={live['messages']}"
            )
    print(
        "NOTE: READY_FOR_MANUAL_CLAIM_TRIAL is evidence for a separate human-approved trial only. "
        "This command never claims, sends, spends, executes a job, or changes autonomy."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only evidence gate for a first manual Kibble claim trial"
    )
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--candidate-limit", type=int, default=5)
    args = parser.parse_args()

    cfg = load_config(args.config)
    con = connect(database_path(cfg))
    try:
        result = evaluate_gate(
            con,
            cfg,
            live=bool(args.live),
            candidate_limit=max(1, min(20, int(args.candidate_limit))),
        )
        print_result(result)
    finally:
        con.close()


if __name__ == "__main__":
    main()
