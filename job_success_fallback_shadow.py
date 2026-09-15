#!/usr/bin/env python3
"""LOCAL-ONLY shadow evaluation of:
bad answer -> Generic Success Gate -> semantic fallback -> Success Gate recheck

No DB writes.
No Technocore network access.
No signing.
No CLAIM / DELIVER.
"""

from __future__ import annotations

from job_candidate_refiner import _runtime_defaults
from job_execution_quality_gate import deterministic_quality_flags
from job_execution_semantic_repair import _known_repair
from job_success_criterion_gate import validate_success_criterion
from job_success_gate_shadow import CASES
from technoscout.llm_backend import create_llm_backend
from technoscout_cli import load_config


def main() -> int:
    cfg = _runtime_defaults(load_config("technoscout.config.json"))
    model = str(
        cfg.get("research_model")
        or cfg.get("triage_model")
        or ""
    ).strip()

    if not model:
        print("FAIL: no local model configured")
        return 2

    llm = create_llm_backend(cfg)

    unsafe = 0
    safe = 0

    try:
        print(f"Success Fallback Shadow | model={model} cases={len(CASES)}")
        print()

        for case in CASES:
            name = case["name"]
            job = case["job"]
            bad = case["bad_answer"]

            print("=" * 72)
            print(f"CASE: {name}")
            print("-" * 72)

            first = validate_success_criterion(
                cfg,
                llm,
                model,
                job,
                bad,
            )

            print(
                "generic="
                f"{first.get('state')} "
                f"decision={first.get('decision', '-')}"
            )

            # A known-bad answer must never pass unchanged.
            if (
                first.get("state") == "SUCCESS_REVIEWED"
                and first.get("decision") == "PASS"
            ):
                print("shadow_result=UNSAFE_ACCEPT")
                unsafe += 1
                continue

            # These fixtures are known-bad inputs. A direct generic
            # SUCCESS_REVIEWED result is therefore an unsafe acceptance.
            if first.get("state") == "SUCCESS_REVIEWED":
                print("shadow_result=UNSAFE_DIRECT_ACCEPT")
                print("FINAL:")
                print(first.get("answer", ""))
                unsafe += 1
                continue

            if first.get("state") != "BLOCKED":
                print(
                    "shadow_result=UNSAFE_STATE "
                    f"state={first.get('state')}"
                )
                unsafe += 1
                continue

            # Pure deterministic lookup only. No DB access.
            repair = _known_repair(job)

            if repair is None:
                print("fallback=NONE")
                print("shadow_result=SAFE_BLOCK_NO_FALLBACK")
                safe += 1
                continue

            fallback_answer, fallback_critique = repair

            print("fallback=FOUND")
            print("fallback_critique=")
            print(fallback_critique)
            print("fallback_answer=")
            print(fallback_answer)

            flags = deterministic_quality_flags(
                job,
                fallback_answer,
            )

            if flags:
                print("fallback_quality_flags=")
                for flag in flags:
                    print(f"- {flag}")
                print("shadow_result=SAFE_BLOCK_FALLBACK_QUALITY")
                safe += 1
                continue

            # Critical: fallback cannot bypass the Generic Success Gate.
            second = validate_success_criterion(
                cfg,
                llm,
                model,
                job,
                fallback_answer,
            )

            print(
                "recheck="
                f"{second.get('state')} "
                f"decision={second.get('decision', '-')}"
            )

            if second.get("state") == "SUCCESS_REVIEWED":
                print("shadow_result=SAFE_FALLBACK_REVALIDATED")
                print("FINAL:")
                print(second.get("answer", fallback_answer))
                safe += 1
            else:
                print("shadow_result=SAFE_BLOCK_AFTER_FALLBACK")
                if second.get("reason"):
                    print("reason=")
                    print(second["reason"])
                safe += 1

        print()
        print("=" * 72)
        print(
            f"SUMMARY safe={safe} unsafe={unsafe} total={len(CASES)}"
        )

        return 0 if unsafe == 0 else 1

    finally:
        close = getattr(llm, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    raise SystemExit(main())
