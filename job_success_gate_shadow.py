#!/usr/bin/env python3
"""Shadow-test generic Success Gate against known historical failures.

LOCAL ONLY:
- no CLAIM
- no DELIVER
- no signing
- no DB writes
- no job-provided command execution
"""

from __future__ import annotations

import json

from job_candidate_refiner import _runtime_defaults
from job_success_criterion_gate import validate_success_criterion
from technoscout.llm_backend import create_llm_backend
from technoscout_cli import load_config


CASES = [
    {
        "name": "latest-image",
        "job": {
            "title": "Recording why a container image tagged as latest was chosen",
            "body": (
                "Write down what a future maintainer needs in order to keep or reverse "
                "the decision to use a container image tagged as latest, without re-deriving it. "
                "Two nodes pull at different times and run different code. "
                "Success: names one constraint worth recording and one alternative that was "
                "rejected and why."
            ),
        },
        "bad_answer": (
            "A future maintainer needs to know the specific constraints that led to the "
            "decision to use the latest container image. One constraint worth recording "
            "is the need for the latest features or security updates. An alternative that "
            "was rejected was using a specific version tag, as it would lock the system "
            "to that version and prevent access to newer features and security patches."
        ),
    },
    {
        "name": "no-down-migration",
        "job": {
            "title": (
                "What newcomers get wrong about a database migration "
                "with no down migration"
            ),
            "body": (
                "State the belief about a database migration with no down migration that "
                "someone new holds until it costs them an incident, and what actually "
                "happens instead. Rolling back the code leaves it talking to the wrong "
                "schema. Success: names one specific wrong expectation and the observation "
                "that corrects it."
            ),
        },
        "bad_answer": (
            "Newcomers often believe that a database migration with no down migration "
            "can be rolled back by simply reverting the code changes. Rolling back the "
            "code leaves the application still referencing the old schema. The correct "
            "approach is to ensure that the down migration script is available and executed."
        ),
    },
    {
        "name": "canary-restore",
        "job": {
            "title": (
                "Designing the backup and restore drill for a canary deploy "
                "that checks only error rate"
            ),
            "body": (
                "Design the backup and restore drill so a future operator can tell whether "
                "recovery actually works. The canary health check observes only error rate. "
                "Success: names a concrete backup artifact, states what restore success "
                "must prove, and identifies one assumption exposed by checking only error rate."
            ),
        },
        "bad_answer": (
            "Test that backups can be restored and verify that the canary has no errors."
        ),
    },
]


def main() -> int:
    cfg = _runtime_defaults(load_config("technoscout.config.json"))
    model = str(
        cfg.get("research_model")
        or cfg.get("triage_model")
        or ""
    ).strip()

    if not model:
        print("FAIL: research_model or triage_model is not configured")
        return 2

    llm = create_llm_backend(cfg)
    failures = 0

    try:
        print(f"Success Gate Shadow | model={model}")
        print(f"cases={len(CASES)}")
        print()

        for case in CASES:
            print("=" * 72)
            print(f"CASE: {case['name']}")
            print("-" * 72)

            result = validate_success_criterion(
                cfg,
                llm,
                model,
                case["job"],
                case["bad_answer"],
            )

            print(f"state={result.get('state', 'UNKNOWN')}")
            print(f"decision={result.get('decision', '-')}")
            print(f"confidence={result.get('confidence', '-')}")

            print("contract=")
            print(
                json.dumps(
                    result.get("contract", {}),
                    indent=2,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )

            verdict = result.get("verdict") or {}
            final_verdict = result.get("final_verdict") or {}
            repair_attempt = result.get("repair_attempt") or {}

            if repair_attempt:
                print("REPAIR ATTEMPT:")
                print(repair_attempt.get("answer", ""))
                print("repair_critique=")
                print(repair_attempt.get("critique", ""))

            if verdict:
                print("first_critique=")
                print(verdict.get("critique", ""))
                print("first_checks=")
                print(json.dumps(verdict.get("checks", []), indent=2, ensure_ascii=False))
                print("first_grounding_checks=")
                print(json.dumps(verdict.get("grounding_checks", []), indent=2, ensure_ascii=False))

            if final_verdict:
                print("final_critique=")
                print(final_verdict.get("critique", ""))
                print("final_checks=")
                print(json.dumps(final_verdict.get("checks", []), indent=2, ensure_ascii=False))
                print("final_grounding_checks=")
                print(json.dumps(final_verdict.get("grounding_checks", []), indent=2, ensure_ascii=False))

            print("FINAL ANSWER:")
            print(result.get("answer", ""))

            # These are intentionally known-bad historical answers.
            # A correct generic gate should repair them, not PASS them unchanged.
            if result.get("state") == "BLOCKED":
                outcome = "SAFE_BLOCK"
                ok = True
            else:
                # Every CASE in this shadow starts from a known-bad answer.
                # Generic LLM rewriting is not trusted as an acceptance path.
                outcome = "UNSAFE_ACCEPT"
                ok = False

            print(f"shadow_result={outcome}")

            if not ok:
                failures += 1

            print()

    finally:
        close = getattr(llm, "close", None)
        if callable(close):
            close()

    print("=" * 72)
    print(
        f"SUMMARY passed={len(CASES) - failures} "
        f"failed={failures} total={len(CASES)}"
    )

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
