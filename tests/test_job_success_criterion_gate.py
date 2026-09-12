import unittest

from job_success_criterion_gate import (
    extract_success_clause,
    validate_success_criterion,
)


class FakeCaller:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(
        self,
        cfg,
        llm,
        model,
        prompt,
        payload,
        *,
        max_tokens,
        timeout_seconds,
    ):
        self.calls.append(payload)
        if not self.responses:
            raise AssertionError("unexpected extra LLM call")
        return self.responses.pop(0)


class SuccessCriterionGateTests(unittest.TestCase):
    def test_extracts_literal_success_clause(self):
        job = {
            "body": (
                "Two nodes pull at different times and run different code. "
                "Success: names one constraint worth recording and one alternative "
                "that was rejected and why."
            )
        }
        body, success = extract_success_clause(job)
        self.assertIn("Two nodes pull", body)
        self.assertEqual(
            success,
            "names one constraint worth recording and one alternative that was rejected and why.",
        )

    def test_no_success_clause_is_not_applicable(self):
        result = validate_success_criterion(
            {},
            object(),
            "fake",
            {"title": "x", "body": "Explain the issue."},
            "answer",
            caller=FakeCaller([]),
        )
        self.assertEqual(result["state"], "NOT_APPLICABLE")

    def test_kef_generic_answer_is_repaired_then_verified(self):
        job = {
            "title": "Recording why a container image tagged as latest was chosen",
            "body": (
                "Write down what a future maintainer needs in order to keep or reverse "
                "the decision to use a container image tagged as latest, without re-deriving it. "
                "Two nodes pull at different times and run different code. "
                "Success: names one constraint worth recording and one alternative that was "
                "rejected and why."
            ),
        }
        bad = (
            "One constraint worth recording is the need for the latest features or security "
            "updates. An alternative that was rejected was using a specific version tag, "
            "as it would lock the system to that version."
        )
        repaired = (
            "One constraint worth recording is that latest is a mutable tag: two nodes pulling "
            "at different times can resolve it to different image digests and run different code. "
            "The rejected alternative was pinning an immutable digest; it was rejected because "
            "that requires an explicit image update and rollout for each new image."
        )

        caller = FakeCaller([
            {
                "requirements": [
                    {"id": "R1", "text": "name one constraint worth recording"},
                    {"id": "R2", "text": "name one rejected alternative"},
                    {"id": "R3", "text": "state why the alternative was rejected"},
                ],
                "grounding": [
                    {
                        "id": "G1",
                        "fact": "two nodes pull at different times and run different code",
                        "required": True,
                    }
                ],
            },
            {
                "decision": "REVISED",
                "confidence": 94,
                "checks": [
                    {"id": "R1", "satisfied": True, "evidence": "latest features or security updates"},
                    {"id": "R2", "satisfied": True, "evidence": "specific version tag"},
                    {"id": "R3", "satisfied": True, "evidence": "lock the system to that version"},
                ],
                "grounding_checks": [
                    {"id": "G1", "satisfied": False, "evidence": ""}
                ],
                "critique": "generic rationale ignores the concrete mutable-tag observation",
                "answer": repaired,
            },
            {
                "decision": "PASS",
                "confidence": 98,
                "checks": [
                    {"id": "R1", "satisfied": True, "evidence": "latest is a mutable tag"},
                    {"id": "R2", "satisfied": True, "evidence": "pinning an immutable digest"},
                    {"id": "R3", "satisfied": True, "evidence": "requires an explicit image update and rollout"},
                ],
                "grounding_checks": [
                    {
                        "id": "G1",
                        "satisfied": True,
                        "evidence": "two nodes pulling at different times can resolve it to different image digests",
                    }
                ],
                "critique": "all frozen criteria are now explicit",
                "answer": repaired,
            },
        ])

        result = validate_success_criterion(
            {"job_success_repair_attempts": 1},
            object(),
            "fake",
            job,
            bad,
            caller=caller,
        )

        self.assertEqual(result["state"], "SUCCESS_REVIEWED")
        self.assertEqual(result["decision"], "REVISED")
        self.assertEqual(result["answer"], repaired)
        self.assertEqual(len(caller.calls), 3)

    def test_false_pass_is_forced_blocked_without_structured_evidence(self):
        job = {
            "title": "Example",
            "body": "Explain it. Success: names one cause and one consequence.",
        }

        caller = FakeCaller([
            {
                "requirements": [
                    {"id": "R1", "text": "name one cause"},
                    {"id": "R2", "text": "name one consequence"},
                ],
                "grounding": [],
            },
            {
                "decision": "PASS",
                "confidence": 99,
                "checks": [
                    {"id": "R1", "satisfied": True, "evidence": "a cause"},
                    # R2 deliberately omitted
                ],
                "grounding_checks": [],
                "critique": "looks fine",
                "answer": "a cause only",
            },
        ])

        result = validate_success_criterion(
            {"job_success_repair_attempts": 0},
            object(),
            "fake",
            job,
            "a cause only",
            caller=caller,
        )

        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("R2", result["reason"])


    def test_no_down_migration_regression(self):
        job = {
            "title": "What newcomers get wrong about a database migration with no down migration",
            "body": (
                "State the belief about a database migration with no down migration "
                "that someone new holds until it costs them an incident, and what "
                "actually happens instead. Rolling back the code leaves it talking "
                "to the wrong schema. Success: names one specific wrong expectation "
                "and the observation that corrects it."
            ),
        }

        bad = (
            "Rolling back the code leaves the application referencing the old schema. "
            "Ensure the down migration is executed."
        )

        repaired = (
            "The wrong expectation is that reverting application code also rolls "
            "back the database migration. With no down migration, the database "
            "remains on the new schema while the rolled-back code expects the old "
            "schema; observing that schema mismatch corrects the expectation."
        )

        caller = FakeCaller([
            {
                "requirements": [
                    {"id": "R1", "text": "name one specific wrong expectation"},
                    {"id": "R2", "text": "name the observation that corrects it"},
                ],
                "grounding": [
                    {
                        "id": "G1",
                        "fact": "rolling back code leaves it talking to the wrong schema",
                        "required": True,
                    }
                ],
            },
            {
                "decision": "REVISED",
                "confidence": 96,
                "checks": [
                    {"id": "R1", "satisfied": False, "evidence": ""},
                    {"id": "R2", "satisfied": False, "evidence": ""},
                ],
                "grounding_checks": [
                    {"id": "G1", "satisfied": False, "evidence": ""}
                ],
                "critique": "schema direction is reversed and a nonexistent down migration is assumed",
                "answer": repaired,
            },
            {
                "decision": "PASS",
                "confidence": 99,
                "checks": [
                    {
                        "id": "R1",
                        "satisfied": True,
                        "evidence": "reverting application code also rolls back the database migration",
                    },
                    {
                        "id": "R2",
                        "satisfied": True,
                        "evidence": "database remains on the new schema while the rolled-back code expects the old schema",
                    },
                ],
                "grounding_checks": [
                    {
                        "id": "G1",
                        "satisfied": True,
                        "evidence": "schema mismatch",
                    }
                ],
                "critique": "all explicit criteria are now satisfied",
                "answer": repaired,
            },
        ])

        result = validate_success_criterion(
            {"job_success_repair_attempts": 1},
            object(),
            "fake",
            job,
            bad,
            caller=caller,
        )

        self.assertEqual(result["state"], "SUCCESS_REVIEWED")
        self.assertEqual(result["decision"], "REVISED")
        self.assertEqual(result["answer"], repaired)

    def test_canary_restore_regression(self):
        job = {
            "title": "Designing the backup and restore drill for a canary deploy that checks only error rate",
            "body": (
                "Design the drill so a future operator can tell whether recovery "
                "actually works. The canary health check observes only error rate. "
                "Success: names a concrete backup artifact, states what restore "
                "success must prove, and identifies one assumption exposed by checking "
                "only error rate."
            ),
        }

        bad = (
            "Test that backups can be restored and verify the canary has no errors."
        )

        repaired = (
            "Use a known-good database snapshot as the backup artifact. Restore "
            "success must prove the service returns to usable operation within the "
            "declared RTO with recovered data no older than the declared RPO. The "
            "exposed assumption is that zero error rate means the canary is healthy; "
            "latency can double while requests still succeed."
        )

        caller = FakeCaller([
            {
                "requirements": [
                    {"id": "R1", "text": "name a concrete backup artifact"},
                    {"id": "R2", "text": "state what restore success must prove"},
                    {"id": "R3", "text": "identify one assumption exposed by checking only error rate"},
                ],
                "grounding": [
                    {
                        "id": "G1",
                        "fact": "the canary health check observes only error rate",
                        "required": True,
                    }
                ],
            },
            {
                "decision": "REVISED",
                "confidence": 93,
                "checks": [
                    {"id": "R1", "satisfied": False, "evidence": ""},
                    {"id": "R2", "satisfied": False, "evidence": ""},
                    {"id": "R3", "satisfied": False, "evidence": ""},
                ],
                "grounding_checks": [
                    {"id": "G1", "satisfied": False, "evidence": ""}
                ],
                "critique": "answer is generic and does not establish recovery proof",
                "answer": repaired,
            },
            {
                "decision": "PASS",
                "confidence": 98,
                "checks": [
                    {
                        "id": "R1",
                        "satisfied": True,
                        "evidence": "known-good database snapshot",
                    },
                    {
                        "id": "R2",
                        "satisfied": True,
                        "evidence": "usable operation within the declared RTO with recovered data no older than the declared RPO",
                    },
                    {
                        "id": "R3",
                        "satisfied": True,
                        "evidence": "zero error rate means the canary is healthy",
                    },
                ],
                "grounding_checks": [
                    {
                        "id": "G1",
                        "satisfied": True,
                        "evidence": "latency can double while requests still succeed",
                    }
                ],
                "critique": "all recovery and canary criteria are explicit",
                "answer": repaired,
            },
        ])

        result = validate_success_criterion(
            {"job_success_repair_attempts": 1},
            object(),
            "fake",
            job,
            bad,
            caller=caller,
        )

        self.assertEqual(result["state"], "SUCCESS_REVIEWED")
        self.assertEqual(result["decision"], "REVISED")
        self.assertEqual(result["answer"], repaired)


    def test_grounding_cannot_be_satisfied_by_unrelated_exact_quote(self):
        job = {
            "title": "Latest image decision",
            "body": (
                "Write down why latest was chosen. "
                "Two nodes pull at different times and run different code. "
                "Success: names one constraint worth recording and one alternative "
                "that was rejected and why."
            ),
        }

        bad = (
            "A future maintainer needs to know the constraints behind the decision. "
            "One constraint worth recording is the need for current security updates. "
            "An alternative that was rejected was a fixed tag because it would require "
            "manual updates."
        )

        caller = FakeCaller([
            {
                "grounding_ids": ["C2"],
            },
            {
                "decision": "PASS",
                "confidence": 100,
                "checks": [
                    {
                        "id": "R1",
                        "satisfied": True,
                        "evidence": "One constraint worth recording is the need for current security updates.",
                    },
                    {
                        "id": "R2",
                        "satisfied": True,
                        "evidence": "An alternative that was rejected was a fixed tag",
                    },
                    {
                        "id": "R3",
                        "satisfied": True,
                        "evidence": "because it would require manual updates.",
                    },
                ],
                "grounding_checks": [
                    {
                        "id": "G1",
                        "satisfied": True,
                        "evidence": "A future maintainer needs to know the constraints behind the decision.",
                    }
                ],
                "critique": "all requirements appear satisfied",
                "answer": bad,
            },
        ])

        result = validate_success_criterion(
            {"job_success_repair_attempts": 0},
            object(),
            "fake",
            job,
            bad,
            caller=caller,
        )

        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("G1", result["reason"])


    def test_generic_llm_repair_is_disabled_by_default(self):
        job = {
            "title": "Generic validation only",
            "body": (
                "Observed mismatch remains after rollback. "
                "Success: names the observation."
            ),
        }

        caller = FakeCaller([
            {
                "grounding_ids": ["C1"],
            },
            {
                "decision": "REVISED",
                "confidence": 100,
                "checks": [
                    {
                        "id": "R1",
                        "satisfied": True,
                        "evidence_ids": ["A1"],
                    }
                ],
                "grounding_checks": [
                    {
                        "id": "G1",
                        "satisfied": True,
                        "evidence_ids": ["A1"],
                    }
                ],
                "critique": "I can repair this.",
                "answer": "Observed mismatch remains after rollback.",
            },
        ])

        result = validate_success_criterion(
            {},
            object(),
            "fake",
            job,
            "Bad answer.",
            caller=caller,
        )

        self.assertEqual(result["state"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
