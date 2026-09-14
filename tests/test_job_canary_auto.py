import sqlite3
import unittest

from job_canary_auto import (
    candidate_policy,
    canary_status,
    complete_success,
    disable_canary,
    enable_canary,
    ensure_canary_schema,
    shadow_observe_candidate,
    shadow_summary,
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
                "room": "kibble",
                "job_id": "kabcdef0123",
                "content_hash": "digest",
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

    def test_shadow_eligible_is_recorded_without_claim_state_change(self):
        prepared = self.good_prepared()
        candidate = prepared["candidate"]

        result = shadow_observe_candidate(
            self.con,
            {},
            candidate["job_id"],
            candidate_loader=lambda con, cfg, job_id, room: (candidate, "eligible"),
            revalidator=lambda cfg, item: {"state": "OPEN_CONFIRMED"},
            exact_fetcher=lambda cfg, item: {"state": "EXACT", "job": prepared["job"]},
        )

        self.assertEqual(result["state"], "SHADOW_ELIGIBLE")
        report = shadow_summary(self.con)
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["eligible"], 1)
        self.assertEqual(report["skipped"], 0)
        self.assertEqual(report["rows"][0]["job_id"], candidate["job_id"])
        claim_count = self.con.execute("SELECT COUNT(*) AS n FROM job_claim_trials").fetchone()["n"]
        self.assertEqual(claim_count, 0)

    def test_shadow_closed_job_is_recorded_as_skip(self):
        prepared = self.good_prepared()
        candidate = prepared["candidate"]

        result = shadow_observe_candidate(
            self.con,
            {},
            candidate["job_id"],
            candidate_loader=lambda con, cfg, job_id, room: (candidate, "eligible"),
            revalidator=lambda cfg, item: {"state": "NOT_OPEN"},
            exact_fetcher=lambda cfg, item: self.fail("exact fetch must not run after NOT_OPEN"),
        )

        self.assertEqual(result["state"], "SHADOW_SKIP")
        report = shadow_summary(self.con)
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["skipped"], 1)
        self.assertIn("NOT_OPEN", report["rows"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
