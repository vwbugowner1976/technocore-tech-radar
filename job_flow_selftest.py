#!/usr/bin/env python3
"""One-command local no-send regression for the hardened JOB flow."""

from __future__ import annotations

import sys
import unittest


TESTS = [
    "tests.test_job_flow_e2e",
    "tests.test_job_live_pipeline_composition",
    "tests.test_job_answer_fidelity",
    "tests.test_job_grounding_bridge",
    "tests.test_job_gpu_semantic_repair",
    "tests.test_job_success_named_proof",
    "tests.test_job_resume_success",
    "tests.test_job_success_criterion_gate",
    "tests.test_job_postclaim_pipeline",
    "tests.test_job_action",
]


def main() -> int:
    print("=== TechnoScout JOB FLOW SELFTEST ===")
    print("LOCAL ONLY: no network, no CLAIM, no DELIVER, no signed write")
    print("fixtures=shared-GPU regression + answer-fidelity grounding regression")

    suite = unittest.defaultTestLoader.loadTestsFromNames(TESTS)
    result = unittest.TextTestRunner(verbosity=1).run(suite)

    if not result.wasSuccessful():
        print("SELFTEST FAIL — do not use a new live JOB yet")
        return 1

    print("SELFTEST PASS — local flow and answer-fidelity guards are ready")
    print("Next live JOB should use the normal human CLAIM/DELIVER boundaries only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
