#!/usr/bin/env python3
"""Explain why OPEN FIT jobs do not pass the manual-claim progress gate.

Read-only diagnostic utility. It does not perform live revalidation, send,
claim, execute work, spend FLOP, or modify autonomy.
"""

from __future__ import annotations

import argparse
from typing import Any

from issuer_reputation import issuer_reputation
from job_progress_gate import _thresholds
from job_shadow import ensure_job_shadow_schema
from technoscout.db import connect
from technoscout_cli import database_path, load_config


def candidate_failures(row: Any, rep: dict[str, Any], thresholds: dict[str, int]) -> list[str]:
    failures: list[str] = []
    checks = (
        ("signed_identity", int(row["signed_identity"] or 0) == 1, "unsigned issuer"),
        ("relevance", int(row["relevance"] or 0) >= thresholds["candidate_relevance"],
         f"relevance={int(row['relevance'] or 0)}/{thresholds['candidate_relevance']}"),
        ("technical_fit", int(row["technical_fit"] or 0) >= thresholds["candidate_fit"],
         f"technical_fit={int(row['technical_fit'] or 0)}/{thresholds['candidate_fit']}"),
        ("confidence", int(row["confidence"] or 0) >= thresholds["candidate_confidence"],
         f"confidence={int(row['confidence'] or 0)}/{thresholds['candidate_confidence']}"),
        ("issuer_jobs", int(rep["total_jobs"]) >= thresholds["issuer_jobs"],
         f"issuer_jobs={int(rep['total_jobs'])}/{thresholds['issuer_jobs']}"),
        ("issuer_closed", int(rep["closed_jobs"]) >= thresholds["issuer_closed"],
         f"issuer_closed={int(rep['closed_jobs'])}/{thresholds['issuer_closed']}"),
        ("issuer_completed", int(rep["completed_jobs"]) >= thresholds["issuer_completed"],
         f"issuer_completed={int(rep['completed_jobs'])}/{thresholds['issuer_completed']}"),
        ("issuer_attested", int(rep["attested_jobs"]) >= thresholds["issuer_attested"],
         f"issuer_attested={int(rep['attested_jobs'])}/{thresholds['issuer_attested']}"),
        ("issuer_completion_rate", int(rep["completion_rate_percent"]) >= thresholds["issuer_completion_rate_percent"],
         f"issuer_completion={int(rep['completion_rate_percent'])}%/{thresholds['issuer_completion_rate_percent']}%"),
    )
    for _name, ok, text in checks:
        if not ok:
            failures.append(text)
    return failures


def diagnostic_rows(con: Any, cfg: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    ensure_job_shadow_schema(con)
    thresholds = _thresholds(cfg)
    rows = con.execute(
        """
        SELECT room,job_id,job_seq,issuer_did,signed_identity,job_type,lifecycle,
               fit_class,relevance,technical_fit,confidence,effort,reason,summary
        FROM job_shadow_candidates
        WHERE lifecycle='OPEN' AND fit_class='FIT'
        ORDER BY technical_fit DESC, confidence DESC, relevance DESC, job_seq DESC
        LIMIT ?
        """,
        (max(1, min(200, int(limit))),),
    ).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        rep = issuer_reputation(con, str(row["issuer_did"]))
        item = {key: row[key] for key in row.keys()}
        item["issuer_reputation"] = rep
        item["failures"] = candidate_failures(row, rep, thresholds)
        result.append(item)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Explain why OPEN FIT Kibble jobs do not pass Job Progress Gate"
    )
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    cfg = load_config(args.config)
    con = connect(database_path(cfg))
    try:
        thresholds = _thresholds(cfg)
        rows = diagnostic_rows(con, cfg, args.limit)
        print(
            "Job Gate Diagnostics | "
            f"open_fit_rows={len(rows)} "
            f"min_rel={thresholds['candidate_relevance']} "
            f"min_fit={thresholds['candidate_fit']} "
            f"min_conf={thresholds['candidate_confidence']}"
        )
        if not rows:
            print("  no OPEN FIT rows")
            return
        for item in rows:
            rep = item["issuer_reputation"]
            failures = item["failures"]
            print(
                f"  {item['job_id']} seq={item['job_seq']} type={item['job_type']} "
                f"rel={item['relevance']} fit={item['technical_fit']} conf={item['confidence']} "
                f"issuer_score={rep['score']} issuer_jobs={rep['total_jobs']} "
                f"closed={rep['closed_jobs']} completed={rep['completed_jobs']} "
                f"attested={rep['attested_jobs']} completion={rep['completion_rate_percent']}%"
            )
            print("    gate=" + ("PASS_LOCAL" if not failures else "BLOCK: " + "; ".join(failures)))
            if item["reason"]:
                print(f"    classifier_reason={item['reason']}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
