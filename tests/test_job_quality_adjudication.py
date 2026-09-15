import sqlite3
import unittest
from datetime import datetime, timezone

from job_claim_trial import ensure_claim_schema
from job_execution_review import ensure_review_schema
from job_execution_quality_gate import ensure_quality_schema, quality_review


class JobQualityAdjudicationTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_claim_schema(self.con)
        ensure_review_schema(self.con)
        ensure_quality_schema(self.con)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.job_id = "kea74499c4b"
        self.digest = "gpu-digest"
        self.answer = (
            "The concrete failure mode is allocator fragmentation leading to an out-of-memory "
            "failure for the smaller inference job. A leading indicator is the largest allocatable "
            "contiguous block shrinking while total free GPU memory can still look nonzero."
        )
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
                "kibble", self.job_id, self.digest, now, "fake-model", "APPROVED", 95,
                "answer covers the failure and leading signal", "hash", self.answer,
            ),
        )
        self.con.commit()
        self.cfg = {"research_model": "fake-model"}

    def tearDown(self):
        self.con.close()

    @staticmethod
    def claim_ok(cfg, trial):
        return {"state": "CLAIM_CONFIRMED", "seq": 150}

    def exact_ok(self, cfg, candidate):
        return {
            "state": "EXACT",
            "job": {
                "verb": "JOB",
                "job_id": self.job_id,
                "job_type": "explain",
                "title": "How a GPU shared by training and inference fails first under load",
                "body": (
                    "Explain the first thing to break in a GPU shared by training and inference "
                    "when demand climbs past what it was sized for. Memory fragments and the "
                    "smaller job is the one that dies. Name the failure mode and the signal that "
                    "shows up before it. Success: names one concrete failure mode and one leading indicator."
                ),
            },
        }

    def test_unchanged_repair_can_be_binary_adjudicated_pass(self):
        calls = {"n": 0}

        def evaluator(cfg, llm, model, prompt, payload, max_tokens, timeout_seconds):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "decision": "REVISED",
                    "confidence": 86,
                    "critique": "wording could be more explicit",
                    "answer": self.answer,
                }
            if calls["n"] == 2:
                return {
                    "decision": "REVISED",
                    "confidence": 88,
                    "critique": "candidate already states both requested items",
                    "answer": self.answer,
                }
            return {
                "decision": "PASS",
                "confidence": 96,
                "critique": "No material correction is needed; it names both the failure mode and leading indicator.",
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
        self.assertEqual(result["decision"], "PASS")
        self.assertTrue(result["repair_attempted"])
        self.assertTrue(result["adjudication_attempted"])
        self.assertEqual(calls["n"], 3)
        self.assertEqual(result["answer"], self.answer)

    def test_adjudicator_revised_is_invalid_and_fails_closed(self):
        calls = {"n": 0}

        def evaluator(cfg, llm, model, prompt, payload, max_tokens, timeout_seconds):
            calls["n"] += 1
            if calls["n"] < 3:
                return {
                    "decision": "REVISED",
                    "confidence": 80,
                    "critique": "claims revision but leaves text unchanged",
                    "answer": self.answer,
                }
            return {
                "decision": "REVISED",
                "confidence": 80,
                "critique": "invalid verdict for a binary adjudicator",
                "answer": self.answer,
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
        self.assertEqual(result["state"], "BLOCKED")
        self.assertTrue(result["adjudication_attempted"])
        self.assertIn("invalid verdict", result["reason"])
        self.assertEqual(calls["n"], 3)

    def test_adjudication_disabled_preserves_fail_closed_behavior(self):
        cfg = dict(self.cfg)
        cfg["job_execution_quality_adjudication_attempts"] = 0
        calls = {"n": 0}

        def evaluator(cfg, llm, model, prompt, payload, max_tokens, timeout_seconds):
            calls["n"] += 1
            return {
                "decision": "REVISED",
                "confidence": 80,
                "critique": "unchanged",
                "answer": self.answer,
            }

        result = quality_review(
            self.con,
            cfg,
            self.job_id,
            model="fake-model",
            evaluator=evaluator,
            exact_fetcher=self.exact_ok,
            claim_verifier=self.claim_ok,
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("adjudication is disabled", result["reason"])
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
