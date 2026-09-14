import sqlite3
import unittest

from job_canary_autonomy_gate import (
    autonomy_summary,
    ensure_autonomy_schema,
    normalize_autonomy,
    review_shadow_candidate,
)


class FakeLLM:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class CanaryAutonomyGateTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_autonomy_schema(self.con)
        self.candidate = {
            "room": "kibble",
            "job_id": "kabcdef0123",
            "content_hash": "digest",
        }

    def tearDown(self):
        self.con.close()

    def test_invalid_decision_fails_closed(self):
        result = normalize_autonomy({"decision": "MAYBE", "confidence": 99, "reason": "unclear"})
        self.assertEqual(result["decision"], "NEEDS_HUMAN")

    def test_missing_reason_fails_closed(self):
        result = normalize_autonomy({"decision": "AUTO_SAFE", "confidence": 99, "reason": ""})
        self.assertEqual(result["decision"], "NEEDS_HUMAN")

    def test_exact_unavailable_is_needs_human_without_loading_llm(self):
        calls = {"llm": 0}

        def llm_factory(cfg):
            calls["llm"] += 1
            return FakeLLM()

        result = review_shadow_candidate(
            self.con,
            {"research_model": "fake"},
            self.candidate["job_id"],
            candidate_loader=lambda con, cfg, job_id, room: (self.candidate, "eligible"),
            exact_fetcher=lambda cfg, candidate: {"state": "NOT_RETAINED"},
            llm_factory=llm_factory,
        )
        self.assertEqual(result["state"], "NEEDS_HUMAN")
        self.assertEqual(calls["llm"], 0)
        report = autonomy_summary(self.con)
        self.assertEqual(report["needs_human"], 1)

    def test_auto_safe_result_is_recorded_and_llm_closed(self):
        llm = FakeLLM()

        def reviewer(cfg, llm_obj, model, job):
            return {"decision": "AUTO_SAFE", "confidence": 97, "reason": "self-contained fixture"}

        result = review_shadow_candidate(
            self.con,
            {"research_model": "fake"},
            self.candidate["job_id"],
            candidate_loader=lambda con, cfg, job_id, room: (self.candidate, "eligible"),
            exact_fetcher=lambda cfg, candidate: {
                "state": "EXACT",
                "job": {"job_type": "explain", "title": "fixture", "body": "Success: explain one fact."},
            },
            llm_factory=lambda cfg: llm,
            reviewer=reviewer,
        )
        self.assertEqual(result["state"], "AUTO_SAFE")
        self.assertEqual(result["confidence"], 97)
        self.assertTrue(llm.closed)
        report = autonomy_summary(self.con)
        self.assertEqual(report["auto_safe"], 1)


if __name__ == "__main__":
    unittest.main()
