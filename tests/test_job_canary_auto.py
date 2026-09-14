import sqlite3
import unittest

from job_canary_auto import (
    candidate_policy,
    canary_status,
    complete_success,
    disable_canary,
    enable_canary,
    ensure_canary_schema,
)


class CanaryAutoTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_canary_schema(self.con)

    def tearDown(self):
        self.con.close()

    def good_prepared(self):
        return {
            "candidate": {
                "job_type": "coordinate",
                "deterministic_class": "FIT",
                "refined_effort": "small",
                "refined_relevance": 85,
                "refined_fit": 95,
                "refined_confidence": 97,
                "issuer_reputation": {
                    "score": 96,
                    "attested_jobs": 6,
                    "completion_rate_percent": 90,
                },
            },
            "job": {
                "title": "Record a compatibility decision",
                "body": "Explain one constraint and one rejected alternative. Success: names both clearly.",
            },
        }

    def test_default_is_off(self):
        self.assertEqual(canary_status(self.con)["mode"], "OFF")

    def test_enable_defaults_to_one_success(self):
        status = enable_canary(self.con)
        self.assertEqual(status["mode"], "CANARY")
        self.assertEqual(status["remaining_successes"], 1)

    def test_one_success_pauses_default_canary(self):
        enable_canary(self.con)
        status = complete_success(self.con, "kabcdef0123")
        self.assertEqual(status["mode"], "PAUSED")
        self.assertEqual(status["remaining_successes"], 0)
        self.assertEqual(status["total_successes"], 1)

    def test_disable_is_terminal_until_rearmed(self):
        enable_canary(self.con, 3)
        status = disable_canary(self.con)
        self.assertEqual(status["mode"], "OFF")
        self.assertEqual(status["remaining_successes"], 0)

    def test_strict_candidate_passes(self):
        self.assertEqual(candidate_policy({}, self.good_prepared())["state"], "ELIGIBLE")

    def test_non_fit_candidate_is_skipped(self):
        prepared = self.good_prepared()
        prepared["candidate"]["deterministic_class"] = "NOT_RELEVANT"
        self.assertEqual(candidate_policy({}, prepared)["state"], "SKIP")

    def test_low_issuer_evidence_is_skipped(self):
        prepared = self.good_prepared()
        prepared["candidate"]["issuer_reputation"]["attested_jobs"] = 1
        self.assertEqual(candidate_policy({}, prepared)["state"], "SKIP")

    def test_url_content_is_skipped(self):
        prepared = self.good_prepared()
        prepared["job"]["body"] += " See https://example.com for details."
        self.assertEqual(candidate_policy({}, prepared)["state"], "SKIP")

    def test_tool_hint_is_skipped(self):
        prepared = self.good_prepared()
        prepared["job"]["body"] += " Run tests before answering."
        self.assertEqual(candidate_policy({}, prepared)["state"], "SKIP")


if __name__ == "__main__":
    unittest.main()
