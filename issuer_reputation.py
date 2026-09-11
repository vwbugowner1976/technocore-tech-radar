#!/usr/bin/env python3
"""Read-only reputation metrics for Kibble job issuers.

The score is intentionally evidence-based and conservative. It uses only
persisted Job Scout lifecycle metadata; it never reads job bodies, sends a
message, claims work, spends tokens, or changes autonomous behavior.
"""

from __future__ import annotations

import argparse
from typing import Any

from job_shadow import ensure_job_shadow_schema
from technoscout.db import connect
from technoscout_cli import database_path, load_config


def _pct(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        return 0
    return max(0, min(100, round(100 * numerator / denominator)))


def issuer_reputation(con: Any, issuer_did: str) -> dict[str, Any]:
    """Return structured lifecycle evidence for one issuer DID."""
    ensure_job_shadow_schema(con)
    row = con.execute(
        """
        SELECT
          COUNT(*) AS total_jobs,
          SUM(CASE WHEN signed_identity=1 THEN 1 ELSE 0 END) AS signed_jobs,
          SUM(CASE WHEN lifecycle='OPEN' THEN 1 ELSE 0 END) AS open_jobs,
          SUM(CASE WHEN lifecycle='CLAIMED' THEN 1 ELSE 0 END) AS claimed_jobs,
          SUM(CASE WHEN lifecycle='DELIVERED' THEN 1 ELSE 0 END) AS delivered_jobs,
          SUM(CASE WHEN lifecycle='ATTESTED' THEN 1 ELSE 0 END) AS attested_jobs
        FROM job_shadow_candidates
        WHERE issuer_did=?
        """,
        (str(issuer_did),),
    ).fetchone()

    total = int(row["total_jobs"] or 0) if row else 0
    signed = int(row["signed_jobs"] or 0) if row else 0
    open_jobs = int(row["open_jobs"] or 0) if row else 0
    claimed = int(row["claimed_jobs"] or 0) if row else 0
    delivered = int(row["delivered_jobs"] or 0) if row else 0
    attested = int(row["attested_jobs"] or 0) if row else 0
    closed = claimed + delivered + attested
    completed = delivered + attested

    # This is not a trust score. It is a compact activity/evidence score used
    # only to rank manual-trial candidates. Direct threshold metrics remain
    # available to the gate and are more important than this score.
    score = 0
    score += min(20, total * 2)
    score += min(20, completed * 4)
    score += min(25, attested * 5)
    score += round(25 * _pct(completed, closed) / 100) if closed else 0
    score += 10 if total > 0 and signed == total else 0
    score = max(0, min(100, score))

    return {
        "issuer_did": str(issuer_did),
        "total_jobs": total,
        "signed_jobs": signed,
        "open_jobs": open_jobs,
        "claimed_jobs": claimed,
        "delivered_jobs": delivered,
        "attested_jobs": attested,
        "closed_jobs": closed,
        "completed_jobs": completed,
        "completion_rate_percent": _pct(completed, closed),
        "attestation_rate_percent": _pct(attested, completed),
        "score": score,
    }


def issuer_reputation_rows(con: Any, limit: int = 20) -> list[dict[str, Any]]:
    """Rank issuers by attested/completed evidence, then observation volume."""
    ensure_job_shadow_schema(con)
    rows = con.execute(
        """
        SELECT issuer_did
        FROM job_shadow_candidates
        WHERE signed_identity=1 AND issuer_did<>''
        GROUP BY issuer_did
        ORDER BY
          SUM(CASE WHEN lifecycle='ATTESTED' THEN 1 ELSE 0 END) DESC,
          SUM(CASE WHEN lifecycle IN ('DELIVERED','ATTESTED') THEN 1 ELSE 0 END) DESC,
          COUNT(*) DESC,
          issuer_did ASC
        LIMIT ?
        """,
        (max(1, min(500, int(limit))),),
    ).fetchall()
    return [issuer_reputation(con, str(row["issuer_did"])) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Show read-only Kibble issuer lifecycle reputation"
    )
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--issuer", default="")
    args = parser.parse_args()

    cfg = load_config(args.config)
    con = connect(database_path(cfg))
    try:
        if args.issuer:
            rows = [issuer_reputation(con, args.issuer)]
        else:
            rows = issuer_reputation_rows(con, args.limit)
        print(f"Issuer Reputation | rows={len(rows)}")
        for item in rows:
            print(
                f"  {item['issuer_did'][:42]} score={item['score']} "
                f"jobs={item['total_jobs']} closed={item['closed_jobs']} "
                f"completed={item['completed_jobs']} attested={item['attested_jobs']} "
                f"completion={item['completion_rate_percent']}% "
                f"attestation={item['attestation_rate_percent']}%"
            )
    finally:
        con.close()


if __name__ == "__main__":
    main()
