import sqlite3
import unittest

from job_gate_diagnostics import candidate_failures, diagnostic_rows
from job_shadow import ensure_job_shadow_schema


class JobGateDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_job_shadow_schema(self.con)

    def tearDown(self):
        self.con.close()

    def insert_job(
        self,
        job_id,
        issuer,
        *,
        lifecycle="OPEN",
        fit_class="FIT",
        relevance=70,
        technical_fit=80,
        confidence=90,
    ):
        self.con.execute(
            """
            INSERT INTO job_shadow_candidates(
              room,job_id,first_seen_at,last_seen_at,job_seq,issuer_did,
              signed_identity,job_type,content_hash,lifecycle,fit_class,relevance,
              technical_fit,confidence,effort,required_capabilities_json,reason,
              summary,evaluation_count,last_evaluated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "kibble", job_id, "2026-09-11T00:00:00+00:00",
                "2026-09-11T00:00:00+00:00", 1, issuer, 1, "explain",
                "hash", lifecycle, fit_class, relevance, technical_fit,
                confidence, "small", "[]", "reason", "summary", 1,
                "2026-09-11T00:00:00+00:00",
            ),
        )

    def test_reports_candidate_threshold_failures(self):
        row = {
            "signed_identity": 1,
            "relevance": 40,
            "technical_fit": 55,
            "confidence": 70,
        }
        rep = {
            "total_jobs": 1,
            "closed_jobs": 0,
            "completed_jobs": 0,
            "attested_jobs": 0,
            "completion_rate_percent": 0,
        }
        thresholds = {
            "candidate_relevance": 50,
            "candidate_fit": 60,
            "candidate_confidence": 75,
            "issuer_jobs": 3,
            "issuer_closed": 2,
            "issuer_completed": 2,
            "issuer_attested": 1,
            "issuer_completion_rate_percent": 50,
        }
        failures = candidate_failures(row, rep, thresholds)
        self.assertIn("relevance=40/50", failures)
        self.assertIn("technical_fit=55/60", failures)
        self.assertIn("confidence=70/75", failures)
        self.assertIn("issuer_jobs=1/3", failures)
        self.assertIn("issuer_attested=0/1", failures)

    def test_diagnostic_rows_show_local_pass(self):
        issuer = "did:key:z6Mkissuer"
        self.insert_job("k0000000001", issuer, lifecycle="ATTESTED")
        self.insert_job("k0000000002", issuer, lifecycle="DELIVERED")
        self.insert_job("k0000000003", issuer, lifecycle="OPEN")
        self.con.commit()
        rows = diagnostic_rows(self.con, {}, 10)
        self.assertEqual(1, len(rows))
        self.assertEqual("k0000000003", rows[0]["job_id"])
        self.assertEqual([], rows[0]["failures"])

    def test_diagnostic_rows_exclude_non_fit(self):
        issuer = "did:key:z6Mkissuer"
        self.insert_job("k0000000001", issuer, fit_class="NEEDS_TOOL")
        self.con.commit()
        self.assertEqual([], diagnostic_rows(self.con, {}, 10))


if __name__ == "__main__":
    unittest.main()
