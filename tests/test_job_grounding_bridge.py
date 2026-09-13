import unittest
from unittest.mock import patch

import job_postclaim_pipeline_live as live


class GroundingBridgeTests(unittest.TestCase):
    def fixture(self):
        return {
            "state": "BLOCKED",
            "contract": {
                "requirements": [{"id": "R1", "text": "one mechanism"}],
                "grounding": [{"id": "G1", "fact": "the queue reaches its configured limit", "required": True}],
            },
            "verdict": {
                "missing_requirements": [],
                "missing_grounding": ["G1"],
                "checks": [{
                    "id": "R1",
                    "satisfied": True,
                    "evidence": "The mechanism is a bounded queue that blocks producers",
                }],
            },
        }

    def test_bridge_contains_fact_and_passed_evidence(self):
        answer = "The mechanism is a bounded queue that blocks producers."
        bridged = live._grounding_bridge(self.fixture(), answer)
        self.assertIn("the queue reaches its configured limit", bridged)
        self.assertIn("The mechanism is a bounded queue that blocks producers", bridged)

    def test_bridge_is_reverified(self):
        calls = []
        def fake(cfg, llm, model, job, answer):
            calls.append((dict(cfg), answer))
            if len(calls) == 1:
                return self.fixture()
            return {"state": "SUCCESS_REVIEWED", "decision": "PASS", "confidence": 95, "answer": answer}

        with patch("job_postclaim_pipeline_live.validate_success_named", side_effect=fake):
            result = live.validate_success_live(
                {}, object(), "model", {"body": "body"},
                "The mechanism is a bounded queue that blocks producers.",
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0]["job_success_repair_attempts"], 0)
        self.assertEqual(result["state"], "SUCCESS_REVIEWED")
        self.assertEqual(result["decision"], "REVISED")


if __name__ == "__main__":
    unittest.main()
