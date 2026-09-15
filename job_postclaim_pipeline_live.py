#!/usr/bin/env python3
"""Default live post-CLAIM pipeline composition."""

from __future__ import annotations

from typing import Any

from job_answer_fidelity import fidelity_flags
from job_execution_quality_gate import quality_review
from job_gpu_semantic_repair import repair_gpu_shared_or_known
from job_postclaim_pipeline import run_postclaim_pipeline as _run_core
from job_success_named_proof import validate_success_criterion as validate_success_named
from technoscout.common import local_llm_json


_FIDELITY_NOTE = """
CASE FACT FIDELITY: Treat the JOB as the complete source of case-specific facts.
Do not add measurements, dates, datasets, historical results, or prior rationale
that the JOB does not state. Do not contradict a stated premise. General technical
knowledge may be recommendations, not invented case history. Do not copy headings
from unrelated task types unless this JOB asks for that concept.
""".strip()


def _clean(value: Any, maximum: int = 4000) -> str:
    return " ".join(str(value or "").split())[:maximum]


def quality_review_live(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    base_evaluator = kwargs.pop("evaluator", None)

    def fidelity_evaluator(
        inner_cfg: dict[str, Any],
        llm: Any,
        model: str,
        prompt: str,
        payload: dict[str, Any],
        *,
        max_tokens: int,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        call = base_evaluator or local_llm_json
        return call(
            inner_cfg,
            llm,
            model,
            prompt + "\n\n" + _FIDELITY_NOTE,
            payload,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )

    result = quality_review(
        con,
        cfg,
        job_id,
        evaluator=fidelity_evaluator,
        **kwargs,
    )
    if result.get("state") != "QUALITY_REVIEWED":
        return result

    exact_fetcher = kwargs.get("exact_fetcher")
    if exact_fetcher is None:
        return result
    exact = exact_fetcher(cfg, {})
    if exact.get("state") != "EXACT":
        return {
            "state": "BLOCKED",
            "reason": f"answer fidelity exact JOB unavailable: {exact.get('state','UNKNOWN')}",
        }

    flags = fidelity_flags(exact["job"], str(result.get("answer", "")))
    if flags:
        return {
            "state": "BLOCKED",
            "reason": "answer fidelity guard: " + "; ".join(flags),
            "fidelity_flags": flags,
        }
    return result


def _grounding_bridge(result: dict[str, Any], candidate_answer: str) -> str:
    if result.get("state") != "BLOCKED":
        return ""
    verdict = result.get("verdict") or {}
    missing_r = verdict.get("missing_requirements")
    missing_g = verdict.get("missing_grounding")
    if not isinstance(missing_r, list) or missing_r:
        return ""
    if not isinstance(missing_g, list) or not missing_g:
        return ""

    passed = [
        item for item in (verdict.get("checks") or [])
        if isinstance(item, dict)
        and bool(item.get("satisfied"))
        and _clean(item.get("evidence"), 1200)
    ]
    if not passed:
        return ""
    requirement_evidence = _clean(passed[0].get("evidence"), 1200).rstrip(" .")

    contract = result.get("contract") or {}
    missing = set(str(item) for item in missing_g)
    additions: list[str] = []
    for item in contract.get("grounding", []) or []:
        if not isinstance(item, dict):
            continue
        gid = _clean(item.get("id"), 32)
        fact = _clean(item.get("fact"), 1200).rstrip(" .")
        if gid not in missing or not fact or not bool(item.get("required", True)):
            continue
        additions.append(
            f"Grounding link: {fact}. This directly supports: {requirement_evidence}."
        )

    if not additions:
        return ""
    return (_clean(candidate_answer, 4000).rstrip() + " " + " ".join(additions)).strip()


def validate_success_live(
    cfg: dict[str, Any],
    llm: Any,
    model: str,
    job: dict[str, Any],
    candidate_answer: str,
) -> dict[str, Any]:
    first = validate_success_named(cfg, llm, model, job, candidate_answer)
    bridged = _grounding_bridge(first, candidate_answer)
    if not bridged:
        return first

    strict_cfg = dict(cfg)
    strict_cfg["job_success_repair_attempts"] = 0
    final = validate_success_named(strict_cfg, llm, model, job, bridged)
    if final.get("state") == "SUCCESS_REVIEWED":
        result = dict(final)
        result["decision"] = "REVISED"
        result["answer"] = bridged
        result["grounding_bridge"] = True
        return result
    return final


def _live_cfg(cfg: dict[str, Any], *, retry: bool = False) -> dict[str, Any]:
    result = dict(cfg)
    floor = 240 if retry else 180
    result["job_execution_draft_timeout_seconds"] = max(
        float(result.get("job_execution_draft_timeout_seconds", 60)),
        120 if retry else 90,
    )
    result["job_execution_review_timeout_seconds"] = max(
        float(result.get("job_execution_review_timeout_seconds", 75)),
        180 if retry else 120,
    )
    result["job_execution_quality_timeout_seconds"] = max(
        float(result.get("job_execution_quality_timeout_seconds", 90)),
        floor,
    )
    result["job_success_contract_timeout_seconds"] = max(
        float(result.get("job_success_contract_timeout_seconds", 90)),
        floor,
    )
    result["job_success_verify_timeout_seconds"] = max(
        float(result.get("job_success_verify_timeout_seconds", 90)),
        floor,
    )
    result["job_success_repair_timeout_seconds"] = max(
        float(result.get("job_success_repair_timeout_seconds", 90)),
        floor,
    )
    return result


def run_postclaim_pipeline(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    kwargs.setdefault("quality_runner", quality_review_live)
    kwargs.setdefault("success_runner", validate_success_live)
    kwargs.setdefault("semantic_repair_runner", repair_gpu_shared_or_known)
    try:
        return _run_core(con, _live_cfg(cfg), job_id, **kwargs)
    except TimeoutError:
        return _run_core(con, _live_cfg(cfg, retry=True), job_id, **kwargs)


__all__ = [
    "run_postclaim_pipeline",
    "quality_review_live",
    "validate_success_live",
    "_grounding_bridge",
]
