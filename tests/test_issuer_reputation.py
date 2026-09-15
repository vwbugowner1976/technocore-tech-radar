import sqlite3
import unittest

from issuer_reputation import issuer_reputation, issuer_reputation_rows
from job_shadow import ensure_job_shadow_schema


class IssuerReputationTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_job_shadow_schema(self.con)

    def tearDown(self):
        self.con.close()

    def add_job(self, job_id, issuer, lifecycle, seq):
        self.con.execute(
            """
            INSERT INTO job_shadow_candidates(
              room,job_id,first_seen_at,last_seen_at,job_seq,issuer_did,
              signed_identity,job_type,content_hash,lifecycle,fit_class,
              relevance,technical_fit,confidence,effort
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "kibble", job_id, "2026-09-11T00:00:00+00:00",
                "2026-09-11T00:00:00+00:00", seq, issuer, 1,
                "explain", "h" + job_id, lifecycle, "FIT", 80, 80, 90, "small",
            ),
        )
        self.con.commit()

    def test_structured_lifecycle_metrics(self):
        issuer = "did:key:z6MkIssuerA"
        self.add_job("k0000000001", issuer, "OPEN", 1)
        self.add_job("k0000000002", issuer, "CLAIMED", 2)
        self.add_job("k0000000003", issuer, "DELIVERED", 3)
        self.add_job("k0000000004", issuer, "ATTESTED", 4)
        self.add_job("k0000000005", issuer, "ATTESTED", 5)

        result = issuer_reputation(self.con, issuer)
        self.assertEqual(result["total_jobs"], 5)
        self.assertEqual(result["open_jobs"], 1)
        self.assertEqual(result["claimed_jobs"], 1)
        self.assertEqual(result["delivered_jobs"], 1)
        self.assertEqual(result["attested_jobs"], 2)
        self.assertEqual(result["closed_jobs"], 4)
        self.assertEqual(result["completed_jobs"], 3)
        self.assertEqual(result["completion_rate_percent"], 75)
        self.assertEqual(result["attestation_rate_percent"], 67)
        self.assertGreater(result["score"], 0)

    def test_ranking_prefers_more_attested_evidence(self):
        a = "did:key:z6MkIssuerA"
        b = "did:key:z6MkIssuerB"
        self.add_job("k0000000011", a, "ATTESTED", 11)
        self.add_job("k0000000012", a, "ATTESTED", 12)
        self.add_job("k0000000021", b, "DELIVERED", 21)
        self.add_job("k0000000022", b, "DELIVERED", 22)
        rows = issuer_reputation_rows(self.con, 10)
        self.assertEqual(rows[0]["issuer_did"], a)


if __name__ == "__main__":
    unittest.main()
