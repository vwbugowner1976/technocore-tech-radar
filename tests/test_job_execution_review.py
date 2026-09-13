import sqlite3
import unittest
from datetime import datetime, timezone

from job_claim_trial import ensure_claim_schema
from job_execution_draft import ensure_exec_schema
from job_execution_review import ensure_review_schema, review_draft


class JobExecutionReviewTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_claim_schema(self.con)
        ensure_exec_schema(self.con)
        ensure_review_schema(self.con)
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
            INSERT INTO job_execution_drafts(
              room,job_id,content_hash,claim_seq,created_at,model,confidence,
              answer_hash,answer_text,note,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'DRAFT')
            """,
            ("kibble", self.job_id, self.digest, 150, now, "fake-model", 80,
             "answerhash", "prior answer", ""),
        )
        self.con.commit()
        self.cfg = {"research_model": "fake-model"}

    def tearDown(self):
        self.con.close()

    @staticmethod
    def claim_ok(cfg, trial):
        return {"state": "CLAIM_CONFIRMED", "seq": 150}

    @staticmethod
    def exact_ok(cfg, candidate):
        return {
            "state": "EXACT",
            "job": {
                "verb": "JOB",
                "job_id": "kabcdef0123",
                "job_type": "explain",
                "title": "title",
                "body": "give one metric and how to obtain it safely",
            },
        }

    def test_revised_review_is_persisted(self):
        def evaluator(*args, **kwargs):
            return {
                "decision": "REVISED",
                "confidence": 95,
                "critique": "prior draft omitted concurrency",
                "answer": "Use aggregate concurrent buffered bytes and establish it with a staged load test.",
            }

        result = review_draft(
            self.con,
            self.cfg,
            self.job_id,
            model="fake-model",
            evaluator=evaluator,
            exact_fetcher=self.exact_ok,
            claim_verifier=self.claim_ok,
        )
        self.assertEqual(result["state"], "REVIEWED")
        self.assertEqual(result["decision"], "REVISED")
        row = self.con.execute(
            "SELECT decision,answer_text FROM job_execution_reviews WHERE job_id=?",
            (self.job_id,),
        ).fetchone()
        self.assertEqual(row["decision"], "REVISED")
        self.assertIn("aggregate concurrent buffered bytes", row["answer_text"])

    def test_claim_must_still_verify(self):
        result = review_draft(
            self.con,
            self.cfg,
            self.job_id,
            model="fake-model",
            evaluator=lambda *a, **k: {},
            exact_fetcher=self.exact_ok,
            claim_verifier=lambda cfg, trial: {"state": "CLAIM_NOT_RETAINED"},
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("claim verification failed", result["reason"])

    def test_blocked_model_result_is_not_persisted(self):
        def evaluator(*args, **kwargs):
            return {
                "decision": "BLOCKED",
                "confidence": 90,
                "critique": "needs external measurements",
                "answer": "",
            }

        result = review_draft(
            self.con,
            self.cfg,
            self.job_id,
            model="fake-model",
            evaluator=evaluator,
            exact_fetcher=self.exact_ok,
            claim_verifier=self.claim_ok,
        )
        self.assertEqual(result["state"], "BLOCKED")
        count = self.con.execute("SELECT COUNT(*) n FROM job_execution_reviews").fetchone()["n"]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
