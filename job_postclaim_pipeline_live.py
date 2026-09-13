#!/usr/bin/env python3
"""Default live post-CLAIM pipeline composition.

This module keeps the hardened core pipeline unchanged and only selects the
validated local components that have passed the no-send E2E selftest:

- literal named-item Success proof supplementation
- narrow shared-GPU deterministic semantic repair, with fallback to existing
  known deterministic semantic repairs

It performs no signed write itself. Human CLAIM/DELIVER boundaries remain in
job_action.py and the existing trial modules.
"""

from __future__ import annotations

from typing import Any

from job_gpu_semantic_repair import repair_gpu_shared_or_known
from job_postclaim_pipeline import run_postclaim_pipeline as _run_core
from job_success_named_proof import validate_success_criterion as validate_success_named


def run_postclaim_pipeline(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run the core pipeline with the proven live Success/semantic components."""
    kwargs.setdefault("success_runner", validate_success_named)
    kwargs.setdefault("semantic_repair_runner", repair_gpu_shared_or_known)
    return _run_core(con, cfg, job_id, **kwargs)


__all__ = ["run_postclaim_pipeline"]
