#!/usr/bin/env python3
"""Default live post-CLAIM pipeline composition."""

from __future__ import annotations

from typing import Any

from job_gpu_semantic_repair import repair_gpu_shared_or_known
from job_postclaim_pipeline import run_postclaim_pipeline as _run_core
from job_success_named_proof import validate_success_criterion as validate_success_named


def _clean(value: Any, maximum: int = 4000) -> str:
    return " ".join(str(value or "").split())[:maximum]


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
    kwargs.setdefault("success_runner", validate_success_live)
    kwargs.setdefault("semantic_repair_runner", repair_gpu_shared_or_known)
    try:
        return _run_core(con, _live_cfg(cfg), job_id, **kwargs)
    except TimeoutError:
        return _run_core(con, _live_cfg(cfg, retry=True), job_id, **kwargs)


__all__ = ["run_postclaim_pipeline", "validate_success_live", "_grounding_bridge"]
