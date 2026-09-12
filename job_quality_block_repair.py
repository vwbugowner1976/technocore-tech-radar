#!/usr/bin/env python3
"""Compatibility wrapper for the current adjudicator-guided quality repair.

The implementation moved to job_quality_block_repair_v3.py so older v1/v2 repair
ledgers remain untouched for auditability while callers keep the stable import
path used by job_postclaim_pipeline.py and existing tests.
"""

from job_quality_block_repair_v3 import ensure_repair_schema, repair_adjudicator_block

__all__ = ["ensure_repair_schema", "repair_adjudicator_block"]
