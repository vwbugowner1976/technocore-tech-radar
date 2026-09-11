import sqlite3
import unittest
from datetime import datetime, timezone

from job_claim_trial import ensure_claim_schema
from job_execution_review import ensure_review_schema
from job_execution_quality_gate import (
    deterministic_quality_flags,
    ensure_quality_schema,
    quality_review,
)


class JobExecutionQualityGateTests(unittest.TestCase):
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
                "kibble", self.job_id, self.digest, 100, "did:key:z6MkIssuer", "coordinate",
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
                "kibble", self.job_id, self.digest, now, "fake-model", "APPROVED", 95,
                "looks good", "hash",
                "Measure the maximum response size during a representative load test.",
            ),
        )
        self.con.commit()
        self.cfg = {"research_model": "fake-model"}

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
                "job_type": "coordinate",
                "title": "Sizing a reverse proxy buffering the entire response before it is under pressure",
                "body": "Decide what to measure ahead of time to know how much a reverse proxy buffering the entire response can take. The client sees nothing until the backend finishes, turning streams into batches. Success: gives one number to establish in advance and how to obtain it safely.",
            },
        }

    @staticmethod
    def duplicate_key_job():
        return {
            "verb": "JOB",
            "job_id": "kabcdef0123",
            "job_type": "explain",
            "title": "Backpressure signaling across a JSON object with duplicate keys boundaries",
            "body": "Explain how a JSON object with duplicate keys communicates congestion upstream when worker queues fill up faster than processing capacity. Parsers disagree on which one wins, and both are defensible. Success: identifies the flow control mechanism and how upstream producers must throttle.",
        }

    @classmethod
    def duplicate_exact(cls, cfg, candidate):
        return {"state": "EXACT", "job": cls.duplicate_key_job()}

    def tearDown(self):
        self.con.close()

    def test_guard_rejects_single_response_size_only(self):
        flags = deterministic_quality_flags(
            self.exact_ok(None, None)["job"],
            "Measure the maximum response size during a representative load test.",
        )
        self.assertTrue(any("concurrency" in flag for flag in flags))
        self.assertTrue(any("single-response" in flag for flag in flags))

    def test_guard_accepts_aggregate_concurrent_buffer_metric(self):
        flags = deterministic_quality_flags(
            self.exact_ok(None, None)["job"],
            "Establish the safe aggregate concurrent buffered bytes threshold with a staged load test while watching memory, spill, latency, and errors.",
        )
        self.assertEqual(flags, [])

    def test_guard_rejects_duplicate_keys_as_backpressure_signal(self):
        flags = deterministic_quality_flags(
            self.duplicate_key_job(),
            "JSON parsers often keep the last duplicate key. This can be used to signal backpressure by setting a key to indicate congestion, and upstream producers should throttle when they see it.",
        )
        self.assertTrue(any("parser semantics" in flag for flag in flags))
        self.assertTrue(any("runtime mechanism" in flag for flag in flags))
        self.assertTrue(any("incorrectly treats" in flag for flag in flags))

    def test_guard_accepts_explicit_flow_control_separate_from_duplicate_keys(self):
        flags = deterministic_quality_flags(
            self.duplicate_key_job(),
            "Duplicate keys do not provide backpressure; their winner policy is only parser semantics. Use a bounded queue that blocks producers or an explicit credit mechanism. When the queue is full or credits reach zero, producers must wait or throttle until consumers free capacity.",
        )
        self.assertEqual(flags, [])

    def test_revised_quality_answer_is_persisted(self):
        def evaluator(*args, **kwargs):
            return {
                "decision": "REVISED",
                "confidence": 97,
                "critique": "single-response size misses concurrency",
                "answer": "Use safe aggregate concurrent buffered bytes as the one capacity number. In staging, replay representative response-size and concurrency distributions, increase load gradually, observe proxy memory/spill/latency/errors, identify the onset of pressure, and set the operating threshold below that point with margin.",
            }

        result = quality_review(
            self.con,
            self.cfg,
            self.job_id,
            model="fake-model",
            evaluator=evaluator,
            exact_fetcher=self.exact_ok,
            claim_verifier=self.claim_ok,
        )
        self.assertEqual(result["state"], "QUALITY_REVIEWED")
        self.assertEqual(result["decision"], "REVISED")
        self.assertTrue(result["flags_before"])
        self.assertFalse(result["repair_attempted"])
        row = self.con.execute(
            "SELECT decision,answer_text FROM job_execution_quality_reviews WHERE job_id=?",
            (self.job_id,),
        ).fetchone()
        self.assertEqual(row["decision"], "REVISED")
        self.assertIn("aggregate concurrent buffered bytes", row["answer_text"])

    def test_deterministic_failure_gets_one_repair_pass(self):
        self.con.execute(
            "UPDATE job_execution_reviews SET answer_text=? WHERE job_id=?",
            ("Duplicate keys can be used to signal backpressure when congestion occurs.", self.job_id),
        )
        self.con.commit()
        calls = {"n": 0}

        def evaluator(cfg, llm, model, prompt, payload, max_tokens, timeout_seconds):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "decision": "REVISED",
                    "confidence": 80,
                    "critique": "needs flow control",
                    "answer": "Duplicate keys can be used to signal backpressure by setting a congestion value.",
                }
            return {
                "decision": "REVISED",
                "confidence": 94,
                "critique": "separated parser semantics from runtime flow control",
                "answer": "Duplicate keys do not provide backpressure; they only have parser-dependent semantics. Use a bounded queue that blocks producers or a credit mechanism. When the queue is full or credits reach zero, producers must wait or throttle until downstream capacity returns.",
            }

        result = quality_review(
            self.con,
            self.cfg,
            self.job_id,
            model="fake-model",
            evaluator=evaluator,
            exact_fetcher=self.duplicate_exact,
            claim_verifier=self.claim_ok,
        )
        self.assertEqual(result["state"], "QUALITY_REVIEWED")
        self.assertTrue(result["repair_attempted"])
        self.assertEqual(calls["n"], 2)
        self.assertIn("do not provide backpressure", result["answer"])

    def test_bad_final_answer_fails_closed_even_after_repair(self):
        result = quality_review(
            self.con,
            self.cfg,
            self.job_id,
            model="fake-model",
            evaluator=lambda *a, **k: {
                "decision": "PASS",
                "confidence": 99,
                "critique": "looks fine",
                "answer": "Measure the maximum response size.",
            },
            exact_fetcher=self.exact_ok,
            claim_verifier=self.claim_ok,
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("deterministic quality guard", result["reason"])
        self.assertTrue(result["repair_attempted"])
        count = self.con.execute("SELECT COUNT(*) n FROM job_execution_quality_reviews").fetchone()["n"]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
