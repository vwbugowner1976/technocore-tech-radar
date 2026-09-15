import unittest

from job_success_named_proof import (
    _named_anchor,
    _supplement_explicit_named_checks,
)


class JobSuccessNamedProofTests(unittest.TestCase):
    def test_short_named_requirement_gets_literal_sentence_id(self):
        payload = {
            "mode": "local-success-verification-only",
            "contract": {
                "requirements": [
                    {"id": "R1", "text": "names one concrete failure mode"},
                    {"id": "R2", "text": "one leading indicator"},
                ]
            },
            "candidate_sentence_candidates": [
                {
                    "id": "A1",
                    "text": (
                        "The failure mode is typically an out-of-memory (OOM) error, "
                        "and the leading indicator is a decrease in GPU memory availability."
                    ),
                }
            ],
        }
        raw = {
            "decision": "PASS",
            "confidence": 98,
            "checks": [
                {"id": "R1", "satisfied": True, "evidence_ids": ["A1"]},
                {"id": "R2", "satisfied": False, "evidence_ids": []},
            ],
            "grounding_checks": [],
            "answer": payload["candidate_sentence_candidates"][0]["text"],
        }

        fixed = _supplement_explicit_named_checks(raw, payload)
        r2 = next(item for item in fixed["checks"] if item["id"] == "R2")
        self.assertTrue(r2["satisfied"])
        self.assertEqual(r2["evidence_ids"], ["A1"])

    def test_does_not_override_explicit_block(self):
        payload = {
            "contract": {"requirements": [{"id": "R2", "text": "one leading indicator"}]},
            "candidate_sentence_candidates": [
                {"id": "A1", "text": "The leading indicator is lower free GPU memory."}
            ],
        }
        raw = {"decision": "BLOCKED", "checks": []}
        self.assertIs(_supplement_explicit_named_checks(raw, payload), raw)

    def test_clause_like_requirement_is_not_supplemented(self):
        self.assertEqual(
            _named_anchor("one observation that corrects the expectation"),
            "",
        )

    def test_label_without_payload_is_not_supplemented(self):
        payload = {
            "contract": {"requirements": [{"id": "R2", "text": "one leading indicator"}]},
            "candidate_sentence_candidates": [
                {"id": "A1", "text": "Leading indicator: GPU."}
            ],
        }
        raw = {"decision": "PASS", "checks": []}
        fixed = _supplement_explicit_named_checks(raw, payload)
        self.assertEqual(fixed.get("checks", []), [])


if __name__ == "__main__":
    unittest.main()
