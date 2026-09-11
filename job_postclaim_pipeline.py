#!/usr/bin/env python3
"""Fast local-only post-CLAIM pipeline for one Kibble job.

Run immediately after job_claim_trial.py reports SENT. This command intentionally
performs no signed write. It reuses one exact JOB snapshot, one retained CLAIM
proof, and one local LLM backend across:

    draft -> review -> adversarial quality gate -> DELIVER prepare

The final delivery prepare still performs the normal live delivery-readiness check
(no later DELIVER/RESULT/ATTEST/WITNESS or conflicting CLAIM). Only the expensive,
read-only local stages reuse the initial exact snapshot/proof so a busy retention
ring cannot age the JOB out between three separate model invocations.

This command never approves or sends DELIVER, never spends FLOP/tokens, never
browses, never executes JOB-provided code/commands, and never touches wallets.
"""

from __future__ import annotations

import argparse
import time
from typing import Any, Callable

from job_candidate_refiner import _runtime_defaults, fetch_exact_job
from job_delivery_trial import prepare_delivery
from job_execution_draft import claimed_trial, generate_draft, verify_claim_retained
from job_execution_quality_gate import quality_review
from job_execution_review import review_draft
from technoscout.db import connect
from technoscout.llm_backend import create_llm_backend
from technoscout_cli import database_path, load_config


def _candidate_from_claim(claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "room": claim["room"],
        "job_id": claim["job_id"],
        "job_seq": claim["job_seq"],
        "issuer_did": claim["issuer_did"],
        "content_hash": claim["content_hash"],
    }


def run_postclaim_pipeline(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    *,
    room: str = "kibble",
    llm: Any | None = None,
    model: str | None = None,
    claim_loader: Callable[..., tuple[dict[str, Any] | None, str]] = claimed_trial,
    claim_verifier: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] = verify_claim_retained,
    exact_fetcher: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] = fetch_exact_job,
    draft_runner: Callable[..., dict[str, Any]] = generate_draft,
    review_runner: Callable[..., dict[str, Any]] = review_draft,
    quality_runner: Callable[..., dict[str, Any]] = quality_review,
    prepare_runner: Callable[..., dict[str, Any]] = prepare_delivery,
) -> dict[str, Any]:
    """Run all read-only post-claim stages, ending in PREPARED delivery only."""
    started = time.monotonic()
    claim, reason = claim_loader(con, job_id, room=room)
    if claim is None:
        return {"state": "BLOCKED", "stage": "claim", "reason": reason}

    claim_proof = claim_verifier(cfg, claim)
    if claim_proof.get("state") != "CLAIM_CONFIRMED":
        return {
            "state": "BLOCKED",
            "stage": "claim-proof",
            "reason": f"claim verification failed: {claim_proof.get('state','UNKNOWN')}",
        }

    candidate = _candidate_from_claim(claim)
    exact = exact_fetcher(cfg, candidate)
    if exact.get("state") != "EXACT":
        return {
            "state": "BLOCKED",
            "stage": "exact-job",
            "reason": f"exact JOB fetch failed: {exact.get('state','UNKNOWN')}",
        }

    # Reuse the one freshly verified immutable snapshot/proof for local-only stages.
    # The delivery prepare below still does a fresh live lifecycle/readiness check.
    def cached_exact(_cfg: dict[str, Any], _candidate: dict[str, Any]) -> dict[str, Any]:
        return exact

    def cached_claim(_cfg: dict[str, Any], _claim: dict[str, Any]) -> dict[str, Any]:
        return claim_proof

    stage_times: dict[str, float] = {}
    t0 = time.monotonic()
    draft = draft_runner(
        con, cfg, job_id, room=room, llm=llm, model=model,
        exact_fetcher=cached_exact, claim_verifier=cached_claim,
    )
    stage_times["draft"] = time.monotonic() - t0
    if draft.get("state") != "DRAFTED":
        return {"state": "BLOCKED", "stage": "draft", "reason": draft.get("reason", "draft failed"), "stage_times": stage_times}

    t0 = time.monotonic()
    review = review_runner(
        con, cfg, job_id, room=room, llm=llm, model=model,
        exact_fetcher=cached_exact, claim_verifier=cached_claim,
    )
    stage_times["review"] = time.monotonic() - t0
    if review.get("state") != "REVIEWED":
        return {"state": "BLOCKED", "stage": "review", "reason": review.get("reason", "review failed"), "stage_times": stage_times}

    t0 = time.monotonic()
    quality = quality_runner(
        con, cfg, job_id, room=room, llm=llm, model=model,
        exact_fetcher=cached_exact, claim_verifier=cached_claim,
    )
    stage_times["quality"] = time.monotonic() - t0
    if quality.get("state") != "QUALITY_REVIEWED":
        return {"state": "BLOCKED", "stage": "quality", "reason": quality.get("reason", "quality review failed"), "stage_times": stage_times}

    t0 = time.monotonic()
    prepared = prepare_runner(
        con, cfg, job_id, room=room, exact_fetcher=cached_exact,
    )
    stage_times["delivery_prepare"] = time.monotonic() - t0
    if prepared.get("state") != "PREPARED":
        return {
            "state": "BLOCKED",
            "stage": "delivery-prepare",
            "reason": prepared.get("reason", "delivery prepare failed"),
            "live": prepared.get("live"),
            "stage_times": stage_times,
        }

    return {
        "state": "READY_FOR_HUMAN_DELIVERY",
        "job_id": job_id,
        "claim_seq": int(claim["sent_seq"]),
        "draft": draft,
        "review": review,
        "quality": quality,
        "prepared": prepared,
        "stage_times": stage_times,
        "elapsed_seconds": time.monotonic() - started,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fast local post-CLAIM pipeline; never sends DELIVER")
    parser.add_argument("job_id")
    parser.add_argument("--room", default="kibble")
    parser.add_argument("--config", default="technoscout.config.json")
    args = parser.parse_args()

    cfg = _runtime_defaults(load_config(args.config))
    model = str(cfg.get("research_model") or cfg.get("triage_model") or "").strip()
    if not model:
        raise SystemExit("research_model or triage_model must be configured")

    con = connect(database_path(cfg))
    llm = create_llm_backend(cfg)
    try:
        result = run_postclaim_pipeline(
            con, cfg, args.job_id, room=args.room, llm=llm, model=model,
        )
    finally:
        close = getattr(llm, "close", None)
        if callable(close):
            close()
        con.close()

    print(f"Job Post-Claim Pipeline | state={result['state']} job={args.job_id}")
    if result["state"] != "READY_FOR_HUMAN_DELIVERY":
        print(f"stage={result.get('stage','unknown')}")
        print(f"reason={result.get('reason','unknown')}")
        if result.get("live"):
            print(f"live={result['live'].get('state','UNKNOWN')}")
        print("STOP: nothing was approved or sent.")
        return

    times = result["stage_times"]
    print(
        "stages="
        f"draft:{times['draft']:.1f}s "
        f"review:{times['review']:.1f}s "
        f"quality:{times['quality']:.1f}s "
        f"prepare:{times['delivery_prepare']:.1f}s "
        f"total:{result['elapsed_seconds']:.1f}s"
    )
    quality = result["quality"]
    print(f"quality={quality['decision']} confidence={quality['confidence']}")
    if quality.get("critique"):
        print(f"critique={quality['critique']}")
    prepared = result["prepared"]
    print("DELIVER PREVIEW — exact one-line message; nothing has been sent")
    print(prepared["text"])
    print(f"prepared_ttl={prepared['ttl_seconds']}s")
    print(f"Next, only after human review: .venv/bin/python job_delivery_trial.py approve {args.job_id}")
    print("STOP: this pipeline never approves or sends DELIVER.")


if __name__ == "__main__":
    main()
