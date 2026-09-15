import sqlite3
import unittest
from unittest.mock import patch

from job_canary_auto import ensure_canary_schema
from job_canary_autonomy_gate import (
    autonomy_precheck,
    autonomy_summary,
    ensure_autonomy_schema,
    normalize_autonomy,
    pending_shadow_rows,
    review_pending,
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
        ensure_canary_schema(self.con)
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

    def test_observer_only_backpressure_job_fails_deterministic_precheck(self):
        job = {
            "job_type": "explain",
            "title": "Backpressure signaling across a liveness probe that checks too much boundaries",
            "body": (
                "Explain how a liveness probe that checks too much communicates congestion upstream "
                "when worker queues fill up faster than processing capacity. A transient dependency "
                "failure causes a restart loop. Success: identifies the flow control mechanism and "
                "how upstream producers must throttle."
            ),
        }
        result = autonomy_precheck(job)
        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "NEEDS_HUMAN")
        self.assertIn("does not state a concrete backpressure propagation mechanism", result["reason"])

    def test_observer_job_with_explicit_control_path_is_not_deterministically_blocked(self):
        job = {
            "job_type": "explain",
            "title": "Explain probe observations and queue backpressure",
            "body": (
                "A liveness probe observes health while a bounded queue blocks producers when full. "
                "Explain the backpressure path and how upstream producers must throttle. "
                "Success: identify the flow control mechanism."
            ),
        }
        self.assertIsNone(autonomy_precheck(job))

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

    def test_observer_only_backpressure_blocks_before_loading_llm(self):
        calls = {"llm": 0}

        def llm_factory(cfg):
            calls["llm"] += 1
            return FakeLLM()

        result = review_shadow_candidate(
            self.con,
            {"research_model": "fake"},
            self.candidate["job_id"],
            candidate_loader=lambda con, cfg, job_id, room: (self.candidate, "eligible"),
            exact_fetcher=lambda cfg, candidate: {
                "state": "EXACT",
                "job": {
                    "job_type": "explain",
                    "title": "Backpressure signaling across a liveness probe that checks too much boundaries",
                    "body": (
                        "Explain how a liveness probe that checks too much communicates congestion upstream "
                        "when worker queues fill up faster than processing capacity. A transient dependency "
                        "failure causes a restart loop. Success: identifies the flow control mechanism and "
                        "how upstream producers must throttle."
                    ),
                },
            },
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

    def test_pending_persists_candidate_unavailable_fail_closed_outcome(self):
        self.con.execute(
            """
            INSERT INTO job_canary_shadow_observations(
              room,job_id,content_hash,verdict,reason,job_type,relevance,technical_fit,
              confidence,issuer_score,attested_jobs,completion_rate_percent,live_state,observed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "kibble", "kstale00001", "stale-digest", "SHADOW_ELIGIBLE", "fixture",
                "explain", 80, 90, 95, 99, 10, 90, "OPEN_CONFIRMED", "2026-09-14T00:00:00+00:00",
            ),
        )
        self.con.commit()

        unavailable = {
            "state": "NEEDS_HUMAN",
            "confidence": 100,
            "reason": "candidate unavailable: SAFE_FIT refinement is older than 900s",
            "job_id": "kstale00001",
            "recorded": False,
        }
        with patch("job_canary_autonomy_gate.review_shadow_candidate", return_value=unavailable):
            results = review_pending(self.con, {}, room="kibble", limit=1)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["recorded"])
        report = autonomy_summary(self.con)
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["needs_human"], 1)
        self.assertIn("older than 900s", report["rows"][0]["reason"])
        self.assertEqual(pending_shadow_rows(self.con, room="kibble", limit=10), [])


if __name__ == "__main__":
    unittest.main()
