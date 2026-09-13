import sqlite3
import unittest
from datetime import datetime, timezone

from job_claim_trial import ensure_claim_schema
from job_execution_quality_gate import ensure_quality_schema
from job_execution_review import ensure_review_schema
from job_execution_semantic_repair import repair_known_semantic_trap


class JobExecutionSemanticRepairTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_claim_schema(self.con)
        ensure_review_schema(self.con)
        ensure_quality_schema(self.con)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.job_id = "kabcdef0123"
        self.digest = "digest"
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
                "kibble", self.job_id, self.digest, 100, "did:key:z6MkIssuer", "explain",
                now, 80, 90, 95, 1.0, 2.0, 1.1, 2.1, 1.2,
                "did:key:z6MkWorker", "claimhash", "SENT", 150, "",
            ),
        )
        self.con.execute(
            """
            INSERT INTO job_execution_reviews(
              room,job_id,content_hash,reviewed_at,model,decision,confidence,
              critique,answer_hash,answer_text,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'REVIEWED')
            """,
            (
                "kibble", self.job_id, self.digest, now, "fake-model", "APPROVED", 80,
                "bad premise", "hash", "Duplicate keys signal congestion upstream.",
            ),
        )
        self.con.commit()

    def tearDown(self):
        self.con.close()

    @staticmethod
    def claim_ok(cfg, claim):
        return {"state": "CLAIM_CONFIRMED", "seq": 150}

    @staticmethod
    def exact_duplicate(cfg, candidate):
        return {
            "state": "EXACT",
            "job": {
                "verb": "JOB",
                "job_id": "kabcdef0123",
                "job_type": "explain",
                "title": "Backpressure signaling across a JSON object with duplicate keys boundaries",
                "body": "Explain how a JSON object with duplicate keys communicates congestion upstream when worker queues fill up faster than processing capacity. Success: identifies the flow control mechanism and how upstream producers must throttle.",
            },
        }

    def test_known_duplicate_key_trap_is_repaired_and_persisted(self):
        result = repair_known_semantic_trap(
            self.con,
            {},
            self.job_id,
            exact_fetcher=self.exact_duplicate,
            claim_verifier=self.claim_ok,
        )
        self.assertEqual(result["state"], "QUALITY_REVIEWED")
        self.assertEqual(result["decision"], "REVISED")
        self.assertIn("Duplicate keys do not provide backpressure", result["answer"])
        self.assertIn("bounded queue", result["answer"])
        row = self.con.execute(
            "SELECT status,decision,model,answer_text FROM job_execution_quality_reviews WHERE job_id=?",
            (self.job_id,),
        ).fetchone()
        self.assertEqual(row["status"], "QUALITY_REVIEWED")
        self.assertEqual(row["decision"], "REVISED")
        self.assertEqual(row["model"], "deterministic-semantic-repair-v1")
        self.assertIn("queue or credit state", row["answer_text"])

    def test_unsupported_job_fails_closed(self):
        def exact_other(cfg, candidate):
            return {
                "state": "EXACT",
                "job": {
                    "verb": "JOB",
                    "job_id": self.job_id,
                    "job_type": "explain",
                    "title": "Explain a checksum",
                    "body": "Explain how a checksum detects accidental corruption.",
                },
            }

        result = repair_known_semantic_trap(
            self.con,
            {},
            self.job_id,
            exact_fetcher=exact_other,
            claim_verifier=self.claim_ok,
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("supported deterministic semantic repair", result["reason"])
        count = self.con.execute("SELECT COUNT(*) n FROM job_execution_quality_reviews").fetchone()["n"]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
