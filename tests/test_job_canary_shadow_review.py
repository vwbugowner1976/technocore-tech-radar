import sqlite3
import unittest

from job_canary_auto import ensure_canary_schema
from job_canary_shadow_review import review_shadow_eligible
from job_candidate_refiner import ensure_refiner_schema
from job_shadow import ensure_job_shadow_schema


class ShadowCanaryReviewTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_job_shadow_schema(self.con)
        ensure_refiner_schema(self.con)
        ensure_canary_schema(self.con)
        self.con.execute(
            """
            INSERT INTO job_shadow_candidates(
              room,job_id,first_seen_at,last_seen_at,job_seq,issuer_did,signed_identity,
              job_type,content_hash,lifecycle,fit_class,relevance,technical_fit,confidence,
              effort,required_capabilities_json,reason,summary,evaluation_count,last_evaluated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("kibble","kabcdef0123","now","now",100,"did:key:z6Mkissuer",1,
             "explain","digest","OPEN","NOT_RELEVANT",25,30,65,"small","[]","","",1,"now"),
        )
        self.con.execute(
            """
            INSERT INTO job_candidate_refinements(
              room,job_id,content_hash,refined_at,decision,relevance,technical_fit,confidence,effort,reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            ("kibble","kabcdef0123","digest","now","SAFE_FIT",80,90,95,"small","semantic match"),
        )
        self.con.execute(
            """
            INSERT INTO job_canary_shadow_observations(
              room,job_id,content_hash,verdict,reason,job_type,relevance,technical_fit,
              confidence,issuer_score,attested_jobs,completion_rate_percent,live_state,observed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("kibble","kabcdef0123","digest","SHADOW_ELIGIBLE","accept","explain",
             80,90,95,99,10,90,"OPEN_CONFIRMED","now"),
        )
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def test_review_fetches_exact_job_without_creating_claim_trial(self):
        rows = review_shadow_eligible(
            self.con,
            {},
            exact_fetcher=lambda cfg, candidate: {
                "state": "EXACT",
                "job": {"title": "A title", "body": "A body. Success: clear."},
            },
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "EXACT")
        self.assertEqual(rows[0]["job"]["title"], "A title")
        claim_count = self.con.execute("SELECT COUNT(*) AS n FROM job_claim_trials").fetchone()["n"]
        self.assertEqual(claim_count, 0)


if __name__ == "__main__":
    unittest.main()
