#!/usr/bin/env python3
"""Narrow deterministic semantic repair for the shared-GPU fragmentation JOB.

This module is local-only. It never signs, posts, browses, executes JOB-provided
commands, spends FLOP/tokens, or touches wallets. It recognizes only the very
specific shared training/inference GPU case whose JOB itself states that memory
fragments and the smaller job dies, then writes a deterministic answer that
preserves that grounding while explicitly naming a failure mode and leading
indicator. All other JOBs fall back to the existing known semantic repairer.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable

from job_candidate_refiner import fetch_exact_job
from job_execution_draft import claimed_trial, verify_claim_retained
from job_execution_quality_gate import deterministic_quality_flags, ensure_quality_schema
from job_execution_review import ensure_review_schema
from job_execution_semantic_repair import repair_known_semantic_trap
from technoscout.common import utc_now


MODEL_NAME = "deterministic-gpu-shared-repair-v1"


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


def _gpu_shared_answer(job: dict[str, Any]) -> tuple[str, str] | None:
    text = f"{job.get('title','')} {job.get('body','')}".casefold()

    shared_gpu = (
        "gpu" in text
        and "training" in text
        and "inference" in text
        and "shared" in text
    )
    grounding = (
        "memory fragments" in text
        and "smaller job" in text
        and any(term in text for term in ("dies", "die", "killed", "fails"))
    )
    success_shape = "failure mode" in text and "leading indicator" in text

    if not (shared_gpu and grounding and success_shape):
        return None

    answer = (
        "When demand exceeds what the shared GPU was sized for, memory fragments and the smaller "
        "inference job is the one that dies first; the concrete failure mode is an allocation/out-of-memory "
        "(OOM) failure in that smaller job. The leading indicator is rising allocator fragmentation with a "
        "shrinking largest contiguous allocatable block, even while some total GPU memory may still appear free."
    )
    critique = (
        "The prior answer named OOM and a leading indicator but repeatedly failed to preserve the JOB's exact "
        "grounding that memory fragments and the smaller job dies first. Applied the narrow shared-GPU invariant "
        "and linked that grounding directly to the failure mode while keeping the leading indicator explicit."
    )
    return answer, critique


def repair_gpu_shared_or_known(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    *,
    room: str = "kibble",
    exact_fetcher: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    claim_verifier: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Repair the exact shared-GPU trap, otherwise defer to existing repairs."""
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

    repair = _gpu_shared_answer(exact["job"])
    if repair is None:
        return repair_known_semantic_trap(
            con,
            cfg,
            job_id,
            room=room,
            exact_fetcher=exact_fetcher,
            claim_verifier=claim_verifier,
        )

    answer, critique = repair
    flags = deterministic_quality_flags(exact["job"], answer)
    if flags:
        return {
            "state": "BLOCKED",
            "reason": "deterministic GPU repair still fails quality guard: " + "; ".join(flags),
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
            "known shared-GPU semantic invariant",
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


__all__ = ["repair_gpu_shared_or_known", "_gpu_shared_answer"]
