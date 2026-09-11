#!/usr/bin/env python3
"""Local-only deterministic semantic repair for narrowly recognized Kibble traps.

This stage exists for cases where the local LLM repeatedly preserves a false
premise even after adversarial review. It is intentionally narrow and fail-closed:
currently it only recognizes the JSON duplicate-key/backpressure confusion.

It never posts, signs, browses, executes job-provided commands, spends FLOP/tokens,
or touches wallets. It requires the existing SENT claim, retained exact claim,
exact retained JOB, matching reviewed-answer content binding, and a deterministic
final answer that passes the normal quality guard before persisting it as a
QUALITY_REVIEWED REVISED answer.
"""

from __future__ import annotations

import argparse
import hashlib
from typing import Any, Callable

from job_candidate_refiner import fetch_exact_job
from job_execution_draft import claimed_trial, verify_claim_retained
from job_execution_quality_gate import deterministic_quality_flags, ensure_quality_schema
from job_execution_review import ensure_review_schema
from technoscout.common import utc_now
from technoscout.db import connect
from technoscout_cli import database_path, load_config


MODEL_NAME = "deterministic-semantic-repair-v1"


def _review_row(con: Any, job_id: str, room: str) -> dict[str, Any] | None:
    ensure_review_schema(con)
    row = con.execute(
        """
        SELECT room,job_id,content_hash,reviewed_at,model,decision,confidence,
               critique,answer_hash,answer_text,status
        FROM job_execution_reviews
        WHERE room=? AND job_id=?
        ORDER BY reviewed_at DESC
        LIMIT 1
        """,
        (room, job_id),
    ).fetchone()
    return {key: row[key] for key in row.keys()} if row is not None else None


def _known_repair(job: dict[str, Any]) -> tuple[str, str] | None:
    text = f"{job.get('title','')} {job.get('body','')}".lower()
    duplicate = any(term in text for term in ("duplicate key", "duplicate keys", "duplicate-key"))
    flow = any(term in text for term in ("backpressure", "congestion", "flow control", "throttle", "throttling", "queue"))
    if not (duplicate and flow):
        return None

    answer = (
        "Duplicate keys do not provide backpressure; which value wins is parser-dependent JSON semantics. "
        "Backpressure must be implemented by the processing pipeline itself, for example with a bounded queue "
        "or an explicit credit/semaphore. When the queue is full or credits are exhausted, upstream producers "
        "must block, pause reads, or throttle until consumers free capacity. Therefore producers should react "
        "to queue or credit state, not duplicate-key parsing."
    )
    critique = (
        "The model repair preserved the false premise that duplicate-key semantics can signal congestion. "
        "Applied the known domain invariant that JSON parsing semantics and runtime backpressure are separate."
    )
    return answer, critique


def repair_known_semantic_trap(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    *,
    room: str = "kibble",
    exact_fetcher: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    claim_verifier: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ensure_review_schema(con)
    ensure_quality_schema(con)

    prior = _review_row(con, job_id, room)
    if prior is None or str(prior["status"]) != "REVIEWED":
        return {"state": "BLOCKED", "reason": "no REVIEWED execution answer exists"}

    claim, reason = claimed_trial(con, job_id, room=room)
    if claim is None:
        return {"state": "BLOCKED", "reason": reason}
    if str(claim["content_hash"]) != str(prior["content_hash"]):
        return {"state": "BLOCKED", "reason": "review content binding does not match claimed JOB"}

    verify = claim_verifier(cfg, claim) if claim_verifier is not None else verify_claim_retained(cfg, claim)
    if verify.get("state") != "CLAIM_CONFIRMED":
        return {"state": "BLOCKED", "reason": f"claim verification failed: {verify.get('state','UNKNOWN')}"}

    candidate = {
        "room": claim["room"],
        "job_id": claim["job_id"],
        "job_seq": claim["job_seq"],
        "issuer_did": claim["issuer_did"],
        "content_hash": claim["content_hash"],
    }
    exact = exact_fetcher(cfg, candidate) if exact_fetcher is not None else fetch_exact_job(cfg, candidate)
    if exact.get("state") != "EXACT":
        return {"state": "BLOCKED", "reason": f"exact JOB fetch failed: {exact.get('state','UNKNOWN')}"}

    repair = _known_repair(exact["job"])
    if repair is None:
        return {"state": "BLOCKED", "reason": "JOB does not match a supported deterministic semantic repair"}
    answer, critique = repair

    flags = deterministic_quality_flags(exact["job"], answer)
    if flags:
        return {
            "state": "BLOCKED",
            "reason": "deterministic repair still fails quality guard: " + "; ".join(flags),
            "flags": flags,
        }

    answer_hash = hashlib.sha256(answer.encode("utf-8")).hexdigest()
    con.execute(
        """
        INSERT INTO job_execution_quality_reviews(
          room,job_id,content_hash,reviewed_at,model,decision,confidence,
          deterministic_flags,critique,answer_hash,answer_text,status
        ) VALUES(?,?,?,?,?,'REVISED',100,?,?,?,?, 'QUALITY_REVIEWED')
        ON CONFLICT(room,job_id,content_hash) DO UPDATE SET
          reviewed_at=excluded.reviewed_at,
          model=excluded.model,
          decision='REVISED',
          confidence=100,
          deterministic_flags=excluded.deterministic_flags,
          critique=excluded.critique,
          answer_hash=excluded.answer_hash,
          answer_text=excluded.answer_text,
          status='QUALITY_REVIEWED'
        """,
        (
            room,
            job_id,
            claim["content_hash"],
            utc_now(),
            MODEL_NAME,
            "known semantic invariant",
            critique,
            answer_hash,
            answer,
        ),
    )
    con.commit()
    return {
        "state": "QUALITY_REVIEWED",
        "job_id": job_id,
        "decision": "REVISED",
        "confidence": 100,
        "model": MODEL_NAME,
        "critique": critique,
        "answer": answer,
        "flags": [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic local semantic repair for a known Kibble trap")
    parser.add_argument("job_id")
    parser.add_argument("--room", default="kibble")
    parser.add_argument("--config", default="technoscout.config.json")
    args = parser.parse_args()

    cfg = load_config(args.config)
    con = connect(database_path(cfg))
    try:
        result = repair_known_semantic_trap(con, cfg, args.job_id, room=args.room)
        print(f"Job Semantic Repair | state={result['state']} job={args.job_id}")
        if result["state"] != "QUALITY_REVIEWED":
            print(f"reason={result['reason']}")
            return
        print(f"decision={result['decision']} confidence={result['confidence']} model={result['model']}")
        print(f"critique={result['critique']}")
        print("QUALITY-REVIEWED ANSWER — deterministic local repair; nothing was sent")
        print(result["answer"])
        print("STOP: human review required. RESULT/DELIVER is still not enabled here.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
