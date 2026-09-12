#!/usr/bin/env python3
"""Fast local-only post-CLAIM pipeline for one Kibble job.

Run immediately after job_claim_trial.py reports SENT. This command intentionally
performs no signed write. It reuses one exact JOB snapshot, one verified CLAIM
proof, and one local LLM backend across:

    draft -> review -> adversarial quality gate -> DELIVER prepare

For the normal one-command flow, the exact JOB was persisted locally before the
signed CLAIM. A retained remote CLAIM is preferred; if it has aged out, the
execution stages may use the cryptographically verified local HTTP-200 CLAIM
receipt. DELIVER itself still requires the normal fresh live lifecycle/conflict
check and never relies on local receipt alone.

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
from job_execution_quality_gate import deterministic_quality_flags, quality_review
from job_execution_review import review_draft
from job_execution_semantic_repair import repair_known_semantic_trap
from job_local_evidence import load_exact_job_snapshot
from job_success_criterion_gate import persist_success_review, validate_success_criterion
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


def _is_grounding_only_success_block(result: dict[str, Any]) -> bool:
    """Allow one local repair only when every explicit Success requirement passed.

    This does not weaken the frozen contract. The retry enables the existing local
    repair writer only when the structured verifier says there are no missing R
    requirements and at least one required G grounding fact is still missing. The
    repaired answer must then pass the exact same final Success verifier.
    """
    if result.get("state") != "BLOCKED":
        return False
    verdict = result.get("verdict") or {}
    missing_requirements = verdict.get("missing_requirements")
    missing_grounding = verdict.get("missing_grounding")
    return (
        isinstance(missing_requirements, list)
        and len(missing_requirements) == 0
        and isinstance(missing_grounding, list)
        and len(missing_grounding) > 0
    )


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
    exact_fetcher: Callable[[dict[str, Any], dict[str,Any]], dict[str, Any]] = fetch_exact_job,
    draft_runner: Callable[..., dict[str, Any]] = generate_draft,
    review_runner: Callable[..., dict[str, Any]] = review_draft,
    quality_runner: Callable[..., dict[str, Any]] = quality_review,
    success_runner: Callable[..., dict[str, Any]] = validate_success_criterion,
    success_persister: Callable[..., dict[str, Any]] = persist_success_review,
    semantic_repair_runner: Callable[..., dict[str, Any]] = repair_known_semantic_trap,
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

    # The unified human flow stores the exact already-verified JOB before CLAIM.
    # Prefer that immutable local snapshot for post-claim local work. If this is an
    # older/manual claim with no snapshot, preserve the previous remote exact-fetch
    # behavior. A present-but-invalid snapshot is a hard failure, not a fallback.
    if exact_fetcher is fetch_exact_job:
        local_exact = load_exact_job_snapshot(con, candidate)
        if local_exact.get("state") == "EXACT":
            exact = local_exact
        elif local_exact.get("state") == "SNAPSHOT_NOT_FOUND":
            exact = exact_fetcher(cfg, candidate)
        else:
            return {
                "state": "BLOCKED",
                "stage": "exact-job",
                "reason": (
                    "local immutable JOB snapshot failed verification: "
                    f"{local_exact.get('state','UNKNOWN')} "
                    f"{local_exact.get('reason','')}"
                ).strip(),
            }
    else:
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
    success = success_runner(
        cfg,
        llm,
        str(model or ""),
        exact["job"],
        str(quality.get("answer", "")),
    )
    stage_times["success"] = time.monotonic() - t0

    # Generic LLM repair remains disabled by default. The only automatic exception
    # is a grounding-only failure: every explicit R requirement already passed,
    # but a required G fact was not explicitly linked. In that narrow case enable
    # exactly one existing local repair attempt, then rely on the same frozen final
    # verifier. No signed write exists in this path.
    grounding_repair = None
    if _is_grounding_only_success_block(success):
        repair_cfg = dict(cfg)
        repair_cfg["job_success_repair_attempts"] = 1
        t0 = time.monotonic()
        grounding_repair = success_runner(
            repair_cfg,
            llm,
            str(model or ""),
            exact["job"],
            str(quality.get("answer", "")),
        )
        stage_times["success_grounding_repair"] = time.monotonic() - t0
        if grounding_repair.get("state") == "SUCCESS_REVIEWED":
            success = grounding_repair

    semantic_fallback = None

    if success.get("state") == "BLOCKED":
        original_success_reason = success.get(
            "reason",
            "Success-criterion review failed",
        )

        t0 = time.monotonic()
        semantic_fallback = semantic_repair_runner(
            con,
            cfg,
            job_id,
            room=room,
            exact_fetcher=cached_exact,
            claim_verifier=cached_claim,
        )
        stage_times["semantic_fallback"] = time.monotonic() - t0

        if semantic_fallback.get("state") != "QUALITY_REVIEWED":
            return {
                "state": "BLOCKED",
                "stage": "success",
                "reason": (
                    f"generic Success gate blocked: {original_success_reason}; "
                    "semantic fallback unavailable: "
                    f"{semantic_fallback.get('reason', 'unknown')}"
                ),
                "success": success,
                "grounding_repair": grounding_repair,
                "semantic_fallback": semantic_fallback,
                "stage_times": stage_times,
            }

        fallback_answer = str(
            semantic_fallback.get("answer", "") or ""
        ).strip()

        if not fallback_answer:
            return {
                "state": "BLOCKED",
                "stage": "semantic-fallback",
                "reason": "semantic fallback returned no answer",
                "stage_times": stage_times,
            }

        # A deterministic semantic repair still cannot bypass the normal
        # deterministic quality guard.
        fallback_flags = deterministic_quality_flags(
            exact["job"],
            fallback_answer,
        )

        if fallback_flags:
            return {
                "state": "BLOCKED",
                "stage": "semantic-fallback",
                "reason": (
                    "semantic fallback fails deterministic quality guard: "
                    + "; ".join(fallback_flags)
                ),
                "stage_times": stage_times,
            }

        # Most important rule:
        # deterministic fallback MUST pass the same generic Success Gate.
        t0 = time.monotonic()
        success = success_runner(
            cfg,
            llm,
            str(model or ""),
            exact["job"],
            fallback_answer,
        )
        stage_times["success_recheck"] = time.monotonic() - t0

        if success.get("state") != "SUCCESS_REVIEWED":
            return {
                "state": "BLOCKED",
                "stage": "success-recheck",
                "reason": (
                    "semantic fallback still fails generic Success gate: "
                    f"{success.get('reason', success.get('state', 'UNKNOWN'))}"
                ),
                "success": success,
                "grounding_repair": grounding_repair,
                "semantic_fallback": semantic_fallback,
                "stage_times": stage_times,
            }

        quality = {
            **quality,
            "answer": fallback_answer,
            "decision": "REVISED",
            "confidence": min(
                int(quality.get("confidence", 0)),
                int(semantic_fallback.get("confidence", 100)),
                int(success.get("confidence", 0)),
            ),
            "model": str(
                semantic_fallback.get(
                    "model",
                    "deterministic-semantic-repair-v1",
                )
            ),
            "critique": str(
                semantic_fallback.get("critique", "") or ""
            ),
        }

    if success.get("state") not in {"SUCCESS_REVIEWED", "NOT_APPLICABLE"}:
        return {
            "state": "BLOCKED",
            "stage": "success",
            "reason": f"unexpected Success-gate state: {success.get('state','UNKNOWN')}",
            "stage_times": stage_times,
        }

    if success.get("state") == "SUCCESS_REVIEWED":
        success_answer = str(success.get("answer", "") or "")

        # Generic repair must still satisfy the existing deterministic/domain guard.
        success_flags = deterministic_quality_flags(exact["job"], success_answer)
        if success_flags:
            return {
                "state": "BLOCKED",
                "stage": "success",
                "reason": (
                    "Success-gate answer fails deterministic quality guard: "
                    + "; ".join(success_flags)
                ),
                "stage_times": stage_times,
            }

        persisted = success_persister(
            con,
            room=room,
            job_id=job_id,
            content_hash=str(claim["content_hash"]),
            result=success,
            quality_confidence=int(quality.get("confidence", 0)),
            quality_model=str(quality.get("model", model or "")),
            quality_answer=str(quality.get("answer", "")),
        )
        if persisted.get("state") != "SUCCESS_REVIEWED":
            return {
                "state": "BLOCKED",
                "stage": "success",
                "reason": persisted.get("reason", "could not persist Success review"),
                "stage_times": stage_times,
            }

        # Keep the in-memory result aligned with what delivery will read.
        quality = {
            **quality,
            "answer": persisted["answer"],
            "decision": (
                "REVISED"
                if persisted.get("decision") == "REVISED"
                else quality.get("decision")
            ),
            "confidence": persisted["confidence"],
        }

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
        "claim_proof_source": str(claim_proof.get("source", "unknown")),
        "exact_job_source": str(exact.get("source", "remote-retained")),
        "draft": draft,
        "review": review,
        "quality": quality,
        "success": success,
        "grounding_repair": grounding_repair,
        "semantic_fallback": semantic_fallback,
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
        f"success:{times['success']:.1f}s "
        f"prepare:{times['delivery_prepare']:.1f}s "
        f"total:{result['elapsed_seconds']:.1f}s"
    )
    print(
        f"evidence claim={result.get('claim_proof_source','unknown')} "
        f"job={result.get('exact_job_source','unknown')}"
    )
    quality = result["quality"]
    print(f"quality={quality['decision']} confidence={quality['confidence']}")
    if quality.get("critique"):
        print(f"critique={quality['critique']}")
    success = result["success"]
    print(
        f"success={success.get('state','UNKNOWN')} "
        f"decision={success.get('decision','-')} "
        f"confidence={success.get('confidence','-')}"
    )
    prepared = result["prepared"]
    print("DELIVER PREVIEW — exact one-line message; nothing has been sent")
    print(prepared["text"])
    print(f"prepared_ttl={prepared['ttl_seconds']}s")
    print(f"Next, only after human review: .venv/bin/python job_delivery_trial.py approve {args.job_id}")
    print("STOP: this pipeline never approves or sends DELIVER.")


if __name__ == "__main__":
    main()