import tempfile
import unittest
from pathlib import Path

from job_shadow import (
    evaluate_job,
    job_shadow_counts,
    job_shadow_rows,
    lifecycle_for_job,
    parse_kibble_message,
    sync_job_shadow,
)
from job_shadow_policy import deterministic_shadow_evaluator
from technoscout.db import connect


class JobShadowTests(unittest.TestCase):
    def test_parse_strict_job_v1(self):
        parsed = parse_kibble_message(
            "JOB v1 | k0123456789 | explain | Explain BLE latency | Compare connection interval effects"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["verb"], "JOB")
        self.assertEqual(parsed["job_id"], "k0123456789")
        self.assertEqual(parsed["job_type"], "explain")
        self.assertIsNone(
            parse_kibble_message(
                "JOB v2 | k0123456789 | explain | title | body"
            )
        )
        self.assertIsNone(
            parse_kibble_message(
                "JOB v1 | not-a-job | explain | title | body"
            )
        )

    def test_parse_result_and_deliver_aliases(self):
        result = parse_kibble_message(
            "RESULT v1 | k0123456789 | useful technical result"
        )
        deliver = parse_kibble_message(
            "DELIVER v1 | k0123456789 | useful technical result"
        )
        self.assertEqual(result["verb"], "RESULT")
        self.assertEqual(deliver["verb"], "DELIVER")
        self.assertEqual(result["job_id"], deliver["job_id"])

    def test_lifecycle_advances_monotonically(self):
        messages = [
            {
                "seq": 10,
                "from": "did:key:z6Mkissuer",
                "text": "JOB v1 | k0123456789 | explain | BLE | Explain BLE latency",
            },
            {
                "seq": 11,
                "from": "did:key:z6Mkworker",
                "text": "CLAIM v1 | k0123456789 | worker",
            },
            {
                "seq": 12,
                "from": "did:key:z6Mkworker",
                "text": "DELIVER v1 | k0123456789 | result",
            },
            {
                "seq": 13,
                "from": "did:key:z6Mkattestor",
                "text": "ATTEST v1 | k0123456789 | useful | rh:abc",
            },
        ]
        self.assertEqual(
            lifecycle_for_job("k0123456789", 10, messages),
            "ATTESTED",
        )

    def test_unsigned_issuer_is_blocked_without_llm(self):
        called = {"value": False}

        def evaluator(*args, **kwargs):
            called["value"] = True
            raise AssertionError("evaluator must not run")

        result = evaluate_job(
            {},
            None,
            "",
            {
                "job_id": "k0123456789",
                "job_type": "explain",
                "title": "BLE",
                "body": "Explain connection intervals",
            },
            sender="anonymous-worker",
            own_did="",
            lifecycle="OPEN",
            evaluator=evaluator,
        )
        self.assertEqual(result["fit_class"], "UNSAFE")
        self.assertFalse(called["value"])

    def test_deterministic_policy_marks_external_work_needs_tool(self):
        result = deterministic_shadow_evaluator(
            {"prefilter_keywords": ["zmk", "zephyr", "nrf52840"]},
            None,
            "",
            "",
            {
                "job": {
                    "type": "research",
                    "title": "Inspect Zephyr repository",
                    "body": "Review the GitHub repository and run tests",
                },
                "project_context": "",
            },
        )
        self.assertEqual(result["class"], "NEEDS_TOOL")
        self.assertGreaterEqual(result["confidence"], 80)

    def test_sync_resumes_deferred_jobs_without_losing_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            messages = [
                {
                    "seq": 100,
                    "from": "did:key:z6MkissuerA123456789",
                    "text": "JOB v1 | k0000000001 | explain | ZMK BLE | Explain ZMK BLE latency",
                },
                {
                    "seq": 101,
                    "from": "did:key:z6MkissuerB123456789",
                    "text": "JOB v1 | k0000000002 | research | Zephyr driver | Inspect a GitHub repository",
                },
            ]

            def fetcher(cfg, path, query):
                since = int(query.get("since", 0))
                return {"messages": [m for m in messages if m["seq"] > since]}

            cfg = {
                "job_shadow_room": "kibble",
                "job_shadow_fetch_limit": 200,
                "job_shadow_max_evaluations_per_cycle": 1,
                "prefilter_keywords": ["zmk", "zephyr", "ble"],
            }
            first = sync_job_shadow(
                con,
                cfg,
                None,
                "",
                fetcher=fetcher,
                evaluator=deterministic_shadow_evaluator,
            )
            second = sync_job_shadow(
                con,
                cfg,
                None,
                "",
                fetcher=fetcher,
                evaluator=deterministic_shadow_evaluator,
            )

            self.assertEqual(first["evaluated"], 1)
            self.assertEqual(second["evaluated"], 1)
            counts = job_shadow_counts(con)
            self.assertEqual(counts["total"], 2)
            rows = {row["job_id"]: row for row in job_shadow_rows(con, 10)}
            self.assertEqual(rows["k0000000001"]["fit_class"], "FIT")
            self.assertEqual(rows["k0000000002"]["fit_class"], "NEEDS_TOOL")
            con.close()


if __name__ == "__main__":
    unittest.main()
