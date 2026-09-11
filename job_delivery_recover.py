#!/usr/bin/env python3
"""Prepare a Kibble DELIVER when the original JOB has aged out of retention.

This is a narrow recovery path for a job that this agent already CLAIMed and
quality-reviewed while the exact JOB was still available. It never sends,
approves, signs, or posts anything. It only creates the same PREPARED delivery
record used by job_delivery_trial.py.

The fallback is allowed only when:
- the sent CLAIM is still retained and exactly matches our DID/seq,
- no later DELIVER/RESULT/ATTEST/WITNESS or conflicting CLAIM is observed,
- the quality artifact is bound to the same claimed JOB content hash,
- the stored answer hash is intact and already passed QUALITY_REVIEWED,
- the quality artifact is recent,
- exact JOB fetch fails specifically with NOT_RETAINED (not mismatch/error).

If the exact JOB is still available, the normal deterministic delivery guard is
re-run as usual. Persistent ambiguity fails closed.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from typing import Any, Callable

from job_candidate_refiner import fetch_exact_job
from job_delivery_trial import (
    TERMINAL_TRIAL_STATES,
    _deliver_text,
    _text_hash,
    eligible_delivery,
    ensure_delivery_schema,
    live_delivery_ready,
)
from job_execution_quality_gate import deterministic_quality_flags
from technoscout.db import connect
from technoscout_cli import database_path, load_config


def _iso_to_epoch(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def prepare_retention_safe(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    *,
    room: str = "kibble",
    readiness_checker: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    exact_fetcher: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    ensure_delivery_schema(con)
    claim, quality, reason = eligible_delivery(con, cfg, job_id, room=room)
    if claim is None or quality is None:
        return {"state": "BLOCKED", "reason": reason}

    clock = time.time() if now is None else float(now)
    max_quality_age = max(300, min(86400, int(cfg.get("job_delivery_recovery_max_quality_age_seconds", 21600))))
    reviewed_epoch = _iso_to_epoch(str(quality["reviewed_at"]))
    if reviewed_epoch <= 0 or clock - reviewed_epoch > max_quality_age:
        return {"state": "BLOCKED", "reason": f"quality artifact is older than {max_quality_age}s; regenerate review instead"}

    # First prove the live CLAIM/lifecycle state. The retention fallback is never
    # allowed merely because the JOB itself disappeared from the ring.
    check = readiness_checker or live_delivery_ready
    live = check(cfg, claim)
    if live.get("state") != "READY_CONFIRMED":
        return {"state": "BLOCKED", "reason": f"live delivery check failed: {live.get('state','UNKNOWN')}", "live": live}

    candidate = {
        "room": claim["room"],
        "job_id": claim["job_id"],
        "job_seq": claim["job_seq"],
        "issuer_did": claim["issuer_did"],
        "content_hash": claim["content_hash"],
    }
    exact = exact_fetcher(cfg, candidate) if exact_fetcher is not None else fetch_exact_job(cfg, candidate)
    exact_state = str(exact.get("state", "UNKNOWN"))
    source = "EXACT_JOB"
    if exact_state == "EXACT":
        flags = deterministic_quality_flags(exact["job"], str(quality["answer_text"]))
        if flags:
            return {"state": "BLOCKED", "reason": "quality answer fails deterministic guard at delivery: " + "; ".join(flags)}
    elif exact_state == "NOT_RETAINED":
        # The exact JOB was verified earlier by the execution/quality stages and
        # the resulting artifact is cryptographically bound locally by content
        # hash + answer hash. Do not accept any other fetch failure here.
        source = "SEALED_QUALITY_ARTIFACT"
    else:
        return {"state": "BLOCKED", "reason": f"exact JOB fetch failed with non-retention state: {exact_state}"}

    existing = con.execute(
        "SELECT status FROM job_delivery_trials WHERE room=? AND job_id=? AND content_hash=?",
        (claim["room"], claim["job_id"], claim["content_hash"]),
    ).fetchone()
    if existing is not None and str(existing["status"]) in TERMINAL_TRIAL_STATES:
        return {"state": "BLOCKED", "reason": f"existing delivery trial is {existing['status']}; automatic re-arm is forbidden"}

    answer = str(quality["answer_text"])
    text = _deliver_text(job_id, answer)
    ttl = max(60, min(1800, int(cfg.get("job_delivery_prepare_ttl_seconds", 600))))
    detail = "prepared from exact JOB" if source == "EXACT_JOB" else "prepared from sealed quality artifact; exact JOB no longer retained"
    con.execute(
        """
        INSERT INTO job_delivery_trials(
          room,job_id,content_hash,claim_seq,claim_sender_did,quality_reviewed_at,
          quality_decision,quality_confidence,answer_hash,prepared_at,prepare_expires_at,
          sender_did,deliver_text_hash,status,detail
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(room,job_id,content_hash) DO UPDATE SET
          claim_seq=excluded.claim_seq,
          claim_sender_did=excluded.claim_sender_did,
          quality_reviewed_at=excluded.quality_reviewed_at,
          quality_decision=excluded.quality_decision,
          quality_confidence=excluded.quality_confidence,
          answer_hash=excluded.answer_hash,
          prepared_at=excluded.prepared_at,
          prepare_expires_at=excluded.prepare_expires_at,
          approved_at=NULL,
          permit_expires_at=NULL,
          consumed_at=NULL,
          sender_did='',
          deliver_text_hash=excluded.deliver_text_hash,
          status='PREPARED',
          sent_seq=NULL,
          detail=excluded.detail
        """,
        (
            claim["room"], claim["job_id"], claim["content_hash"], int(claim["sent_seq"]),
            claim["sender_did"], quality["reviewed_at"], quality["decision"],
            int(quality["confidence"]), quality["answer_hash"], clock, clock + ttl,
            "", _text_hash(text), "PREPARED", detail,
        ),
    )
    con.commit()
    return {
        "state": "PREPARED",
        "job_id": job_id,
        "answer": answer,
        "text": text,
        "quality": quality,
        "live": live,
        "ttl_seconds": ttl,
        "source": source,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Retention-safe PREPARE for an already-claimed Kibble DELIVER")
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("job_id")
    parser.add_argument("--room", default="kibble")
    args = parser.parse_args()

    cfg = load_config(args.config)
    con = connect(database_path(cfg))
    try:
        result = prepare_retention_safe(con, cfg, args.job_id, room=args.room)
        print(f"Job Delivery Recovery | state={result['state']} job={args.job_id}")
        if result["state"] != "PREPARED":
            print(f"reason={result['reason']}")
            return
        print(f"source={result['source']} live={result['live'].get('state','UNKNOWN')}")
        if result["source"] == "SEALED_QUALITY_ARTIFACT":
            print("NOTE: original JOB aged out of retention; using the previously exact-bound QUALITY_REVIEWED artifact. Nothing has been sent.")
        print(f"quality={result['quality']['decision']} confidence={result['quality']['confidence']}")
        print("DELIVER PREVIEW — exact one-line message; nothing has been sent")
        print(result["text"])
        print(f"prepared_ttl={result['ttl_seconds']}s")
        print(f"Next, only after human review: .venv/bin/python job_delivery_trial.py approve {args.job_id}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
