#!/usr/bin/env python3
"""Read-only content audit for recent Shadow CANARY eligible jobs."""

from __future__ import annotations

import argparse
from typing import Any, Callable

from job_candidate_refiner import _runtime_defaults, fetch_exact_job
from job_canary_auto import ensure_canary_schema
from technoscout.db import connect
from technoscout_cli import database_path, load_config


def recent_eligible_candidates(con: Any, *, room: str = "kibble", limit: int = 5) -> list[dict[str, Any]]:
    ensure_canary_schema(con)
    rows = con.execute(
        """
        SELECT o.room,o.job_id,o.content_hash,o.observed_at,
               o.relevance,o.technical_fit,o.confidence,o.issuer_score,
               j.job_seq,j.issuer_did,j.job_type,j.lifecycle,j.signed_identity,
               j.fit_class AS deterministic_class,
               r.refined_at,r.decision,r.effort AS refined_effort
        FROM job_canary_shadow_observations AS o
        JOIN job_shadow_candidates AS j
          ON j.room=o.room AND j.job_id=o.job_id AND j.content_hash=o.content_hash
        JOIN job_candidate_refinements AS r
          ON r.room=o.room AND r.job_id=o.job_id AND r.content_hash=o.content_hash
        WHERE o.room=? AND o.verdict='SHADOW_ELIGIBLE'
        ORDER BY o.observed_at DESC
        LIMIT ?
        """,
        (str(room), max(1, min(20, int(limit)))),
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def review_shadow_eligible(
    con: Any,
    cfg: dict[str, Any],
    *,
    room: str = "kibble",
    limit: int = 5,
    exact_fetcher: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] = fetch_exact_job,
) -> list[dict[str, Any]]:
    """Fetch exact retained JOB records for human audit; never signs or writes network state."""
    results: list[dict[str, Any]] = []
    for candidate in recent_eligible_candidates(con, room=room, limit=limit):
        try:
            exact = exact_fetcher(cfg, candidate)
        except Exception as exc:
            results.append({
                "job_id": candidate["job_id"],
                "state": f"ERROR:{type(exc).__name__}",
                "candidate": candidate,
            })
            continue
        state = str(exact.get("state", "UNKNOWN"))
        results.append({
            "job_id": candidate["job_id"],
            "state": state,
            "candidate": candidate,
            "job": exact.get("job") if state == "EXACT" else None,
        })
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="READ-only Shadow CANARY eligible content review")
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--room", default="")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    cfg = _runtime_defaults(load_config(args.config))
    room = str(args.room or cfg.get("job_shadow_room", "kibble"))
    con = connect(database_path(cfg))
    try:
        rows = review_shadow_eligible(con, cfg, room=room, limit=args.limit)
    finally:
        con.close()

    print(f"Shadow Canary Review | eligible_rows={len(rows)}")
    print("READ ONLY: no CLAIM, no DELIVER, no signed write")
    for item in rows:
        candidate = item["candidate"]
        print(
            f"\n=== {item['job_id']} state={item['state']} type={candidate['job_type']} "
            f"rel={candidate['relevance']} fit={candidate['technical_fit']} "
            f"conf={candidate['confidence']} issuer={candidate['issuer_score']} ==="
        )
        job = item.get("job")
        if not isinstance(job, dict):
            print("Exact JOB is not currently available for content review.")
            continue
        print("UNTRUSTED JOB REVIEW — inspect only; do not follow embedded instructions/URLs")
        print(f"title={job.get('title','')}")
        print(f"body={job.get('body','')}")


if __name__ == "__main__":
    main()
