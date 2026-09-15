import hashlib
import sqlite3
import unittest
from datetime import datetime, timezone

from job_claim_trial import ensure_claim_schema
from job_delivery_recover import prepare_retention_safe
from job_delivery_trial import ensure_delivery_schema
from job_execution_quality_gate import ensure_quality_schema


class JobDeliveryRecoverTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_claim_schema(self.con)
        ensure_quality_schema(self.con)
        ensure_delivery_schema(self.con)
        self.job_id = "kabcdef0123"
        self.digest = "digest"
        self.worker = "did:key:z6MkWorker"
        self.issuer = "did:key:z6MkIssuer"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.answer = (
            "Use safe aggregate concurrent buffered bytes as the capacity number. "
            "In staging, reproduce representative response size and concurrency, "
            "increase load gradually, observe memory, spill, latency and errors, "
            "then set the operating threshold below the pressure point with margin."
        )
        answer_hash = hashlib.sha256(self.answer.encode()).hexdigest()
        self.con.execute(
            """
            INSERT INTO job_claim_trials(
              room,job_id,content_hash,job_seq,issuer_did,job_type,refined_at,
              refined_relevance,refined_fit,refined_confidence,prepared_at,
              prepare_expires_at,approved_at,permit_expires_at,consumed_at,
              sender_did,claim_text_hash,status,sent_seq,detail
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "kibble", self.job_id, self.digest, 100, self.issuer, "coordinate",
                now, 80, 90, 95, 1.0, 2.0, 1.1, 2.1, 1.2,
                self.worker, "claimhash", "SENT", 150, "",
            ),
        )
        self.con.execute(
            """
            INSERT INTO job_execution_quality_reviews(
              room,job_id,content_hash,reviewed_at,model,decision,confidence,
              deterministic_flags,critique,answer_hash,answer_text,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'QUALITY_REVIEWED')
            """,
            (
                "kibble", self.job_id, self.digest, now, "fake-model", "REVISED", 85,
                "old flag", "fixed concurrency", answer_hash, self.answer,
            ),
        )
        self.con.commit()
        self.cfg = {
            "job_delivery_min_quality_confidence": 80,
            "job_delivery_prepare_ttl_seconds": 600,
            "job_delivery_recovery_max_quality_age_seconds": 21600,
        }

    def tearDown(self):
        self.con.close()

    @staticmethod
    def ready(cfg, claim):
        return {"state": "READY_CONFIRMED", "source": "test"}

    def test_not_retained_job_uses_bound_quality_artifact(self):
        result = prepare_retention_safe(
            self.con,
            self.cfg,
            self.job_id,
            readiness_checker=self.ready,
            exact_fetcher=lambda cfg, candidate: {"state": "NOT_RETAINED"},
        )
        self.assertEqual(result["state"], "PREPARED")
        self.assertEqual(result["source"], "SEALED_QUALITY_ARTIFACT")
        self.assertIn("aggregate concurrent buffered bytes", result["text"])

    def test_non_retention_fetch_failure_stays_blocked(self):
        result = prepare_retention_safe(
            self.con,
            self.cfg,
            self.job_id,
            readiness_checker=self.ready,
            exact_fetcher=lambda cfg, candidate: {"state": "JOB_MISMATCH"},
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("non-retention state", result["reason"])

    def test_live_claim_must_still_be_confirmed(self):
        result = prepare_retention_safe(
            self.con,
            self.cfg,
            self.job_id,
            readiness_checker=lambda cfg, claim: {"state": "CLAIM_NOT_RETAINED"},
            exact_fetcher=lambda cfg, candidate: {"state": "NOT_RETAINED"},
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("live delivery check failed", result["reason"])

    def test_exact_job_path_rechecks_deterministic_guard(self):
        result = prepare_retention_safe(
            self.con,
            self.cfg,
            self.job_id,
            readiness_checker=self.ready,
            exact_fetcher=lambda cfg, candidate: {
                "state": "EXACT",
                "job": {
                    "title": "Sizing a reverse proxy buffering the entire response",
                    "body": "Give one number under pressure while buffering the entire response until the backend finishes.",
                },
            },
        )
        self.assertEqual(result["state"], "PREPARED")
        self.assertEqual(result["source"], "EXACT_JOB")


if __name__ == "__main__":
    unittest.main()
