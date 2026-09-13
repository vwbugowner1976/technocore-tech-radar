#!/usr/bin/env python3
"""Reconcile an UNCERTAIN DELIVER without ever sending again."""

from __future__ import annotations

import argparse
import time
from typing import Any, Callable

from job_auto_orchestrator import ensure_auto_schema
from job_delivery_trial import ensure_delivery_schema
from job_live_revalidator import retained_export_messages
from job_shadow import sender_of
from technoscout.common import seq_of, utc_now
from technoscout.db import connect
from technoscout_cli import database_path, load_config


def _latest_trial(con: Any, room: str, job_id: str) -> Any | None:
    ensure_delivery_schema(con)
    return con.execute(
        "SELECT * FROM job_delivery_trials WHERE room=? AND job_id=? ORDER BY prepared_at DESC LIMIT 1",
        (str(room), str(job_id)),
    ).fetchone()


def _latest_uncertain_attempt(con: Any, room: str, job_id: str, content_hash: str) -> Any | None:
    return con.execute(
        """
        SELECT * FROM job_delivery_attempts
        WHERE room=? AND job_id=? AND content_hash=? AND status='uncertain'
        ORDER BY id DESC LIMIT 1
        """,
        (str(room), str(job_id), str(content_hash)),
    ).fetchone()


def reconcile_uncertain_delivery(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    *,
    room: str = "kibble",
    export_fetcher: Callable[[dict[str, Any], str], list[dict[str, Any]]] = retained_export_messages,
    reads: int = 3,
    delay_seconds: float = 1.0,
) -> dict[str, Any]:
    """READ-only network reconciliation using the exact locally signed record."""
    ensure_delivery_schema(con)
    trial = _latest_trial(con, room, job_id)
    if trial is None:
        return {"state": "BLOCKED", "reason": "no delivery trial exists"}

    status = str(trial["status"])
    if status == "SENT":
        return {"state": "SENT", "seq": trial["sent_seq"], "existing": True}
    if status != "UNCERTAIN":
        return {"state": "BLOCKED", "reason": f"delivery status is {status}, not UNCERTAIN"}

    attempt = _latest_uncertain_attempt(con, room, job_id, str(trial["content_hash"]))
    if attempt is None:
        return {"state": "UNCERTAIN", "reason": "no uncertain signed attempt record exists"}

    if str(attempt["did"]) != str(trial["sender_did"]):
        return {"state": "UNCERTAIN", "reason": "attempt DID does not match delivery signer binding"}

    expected = {
        "did": str(attempt["did"]),
        "nonce": str(attempt["nonce"]),
        "sig": str(attempt["sig"]),
        "text": str(attempt["text"]),
    }

    total_reads = max(1, min(5, int(reads)))
    for index in range(total_reads):
        messages = export_fetcher(cfg, str(room))
        matches = [
            item for item in messages
            if sender_of(item) == expected["did"]
            and str(item.get("nonce", "")) == expected["nonce"]
            and str(item.get("sig", "")) == expected["sig"]
            and str(item.get("text", item.get("message", ""))) == expected["text"]
        ]
        if matches:
            matched = max(matches, key=seq_of)
            seq = seq_of(matched)
            if seq <= 0:
                return {"state": "UNCERTAIN", "reason": "exact signed record matched but has no valid seq"}

            con.execute(
                "UPDATE job_delivery_attempts SET status='sent-reconciled',http_status=200,detail=? WHERE id=?",
                (f"exact signed record reconciled from retained export seq={seq}", int(attempt["id"])),
            )
            con.execute(
                """
                UPDATE job_delivery_trials
                SET status='SENT',sent_seq=?,detail='exact signed DELIVER reconciled from retained export after uncertain HTTP 200'
                WHERE room=? AND job_id=? AND content_hash=? AND status='UNCERTAIN'
                """,
                (seq, str(room), str(job_id), str(trial["content_hash"])),
            )
            ensure_auto_schema(con)
            con.execute(
                """
                UPDATE job_auto_orchestrator
                SET pipeline_state='DELIVERED',blocked_notified=1,detail=?,updated_at=?
                WHERE room=? AND job_id=? AND content_hash=?
                """,
                (
                    f"delivery reconciled exact signed record seq={seq}",
                    utc_now(), str(room), str(job_id), str(trial["content_hash"]),
                ),
            )
            con.commit()
            return {"state": "SENT_RECONCILED", "seq": seq, "reads": index + 1}

        if index + 1 < total_reads and delay_seconds > 0:
            time.sleep(float(delay_seconds))

    return {
        "state": "UNCERTAIN",
        "reason": "exact signed DELIVER was not found in retained export; do not resend",
        "reads": total_reads,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="READ-only reconciliation for one UNCERTAIN DELIVER")
    parser.add_argument("job_id")
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--room", default="kibble")
    parser.add_argument("--reads", type=int, default=3)
    args = parser.parse_args()

    cfg = load_config(args.config)
    con = connect(database_path(cfg))
    try:
        result = reconcile_uncertain_delivery(
            con, cfg, args.job_id, room=args.room, reads=args.reads
        )
    finally:
        con.close()

    print(f"Delivery Reconcile | state={result['state']} job={args.job_id}")
    if "seq" in result:
        print(f"seq={result['seq']}")
    if result.get("reason"):
        print(f"reason={result['reason']}")
    if result["state"] == "UNCERTAIN":
        print("STOP: do not resend DELIVER automatically.")


if __name__ == "__main__":
    main()
