#!/usr/bin/env python3
"""Default live post-CLAIM pipeline composition.

This module keeps the hardened core pipeline unchanged and only selects the
validated local components that have passed the no-send E2E selftest:

- literal named-item Success proof supplementation
- narrow shared-GPU deterministic semantic repair, with fallback to existing
  known deterministic semantic repairs
- live-only timeout floors for the slower local MLX review stages
- at most one local retry after an MLX TimeoutError

It performs no signed write itself. Human CLAIM/DELIVER boundaries remain in
job_action.py and the existing trial modules. A timeout retry only repeats local
DRAFT/REVIEW/QUALITY/SUCCESS/PREPARE work; it never re-sends CLAIM or DELIVER.
"""

from __future__ import annotations

from typing import Any

from job_gpu_semantic_repair import repair_gpu_shared_or_known
from job_postclaim_pipeline import run_postclaim_pipeline as _run_core
from job_success_named_proof import validate_success_criterion as validate_success_named


_LIVE_TIMEOUT_FLOORS: dict[str, float] = {
    "job_execution_draft_timeout_seconds": 90.0,
    "job_execution_review_timeout_seconds": 120.0,
    "job_execution_quality_timeout_seconds": 180.0,
    "job_success_contract_timeout_seconds": 120.0,
    "job_success_verify_timeout_seconds": 180.0,
    "job_success_repair_timeout_seconds": 180.0,
}

_RETRY_TIMEOUT_FLOORS: dict[str, float] = {
    "job_execution_draft_timeout_seconds": 120.0,
    "job_execution_review_timeout_seconds": 180.0,
    "job_execution_quality_timeout_seconds": 240.0,
    "job_success_contract_timeout_seconds": 180.0,
    "job_success_verify_timeout_seconds": 240.0,
    "job_success_repair_timeout_seconds": 240.0,
}


def _with_timeout_floors(
    cfg: dict[str, Any],
    floors: dict[str, float],
) -> dict[str, Any]:
    result = dict(cfg)
    for key, floor in floors.items():
        try:
            current = float(result.get(key, 0) or 0)
        except (TypeError, ValueError):
            current = 0.0
        if current < floor:
            result[key] = floor
    return result


def run_postclaim_pipeline(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run the proven live composition with one timeout-only local retry."""
    kwargs.setdefault("success_runner", validate_success_named)
    kwargs.setdefault("semantic_repair_runner", repair_gpu_shared_or_known)

    live_cfg = _with_timeout_floors(cfg, _LIVE_TIMEOUT_FLOORS)
    try:
        return _run_core(con, live_cfg, job_id, **kwargs)
    except TimeoutError as exc:
        print(
            "Pipeline | local MLX timeout; retrying local pipeline once with extended deadlines: "
            f"{exc}"
        )
        retry_cfg = _with_timeout_floors(live_cfg, _RETRY_TIMEOUT_FLOORS)
        return _run_core(con, retry_cfg, job_id, **kwargs)


__all__ = [
    "run_postclaim_pipeline",
    "_with_timeout_floors",
    "_LIVE_TIMEOUT_FLOORS",
    "_RETRY_TIMEOUT_FLOORS",
]
