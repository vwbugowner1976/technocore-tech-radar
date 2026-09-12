import sqlite3
import unittest
from datetime import datetime, timezone

from job_claim_trial import ensure_claim_schema
from job_execution_quality_gate import ensure_quality_schema
from job_execution_review import ensure_review_schema
from job_quality_block_repair import ensure_repair_schema, repair_adjudicator_block


class JobQualityBlockRepairTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_claim_schema(self.con)
        ensure_review_schema(self.con)
        ensure_quality_schema(self.con)
        ensure_repair_schema(self.con)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.job_id = "kea74499c4b"
        self.digest = "gpu-digest"
        self.candidate = (
            "The failure mode is GPU memory fragmentation causing an out-of-memory "
            "failure for the smaller inference job."
        )
        self.job = {
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
        }
        self.con.execute(
            """
            INSERT INTO job_execution_reviews(
              room,job_id,content_hash,reviewed_at,model,decision,confidence,
              critique,answer_hash,answer_text,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'REVIEWED')
            """,
            (
                "kibble", self.job_id, self.digest, now, "fake-model", "APPROVED", 90,
                "needs explicit signal", "hash", self.candidate,
            ),
        )
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def test_concrete_defect_gets_one_additive_repair(self):
        addition = (
            "Leading indicator: allocation retries or failed large allocations rise "
            "while nominal free GPU memory still remains."
        )

        def evaluator(cfg, llm, model, prompt, payload, max_tokens, timeout_seconds):
            self.assertIn("leading indicator", payload["adjudicator_defect"].lower())
            self.assertEqual(payload["candidate_answer"], self.candidate)
            return {
                "decision": "ADD",
                "confidence": 94,
                "critique": "added an explicit leading indicator",
                "addition": addition,
            }

        result = repair_adjudicator_block(
            self.con,
            {"research_model": "fake-model"},
            self.job_id,
            content_hash=self.digest,
            job=self.job,
            defect="The candidate answer is missing the requested leading indicator.",
            model="fake-model",
            evaluator=evaluator,
        )
        self.assertEqual(result["state"], "QUALITY_REVIEWED")
        self.assertEqual(result["decision"], "REVISED")
        self.assertEqual(result["repair_strategy"], "additive-v2")
        self.assertIn(self.candidate, result["answer"])
        self.assertIn("Leading indicator:", result["answer"])
        row = self.con.execute(
            "SELECT decision,answer_text,status FROM job_execution_quality_reviews WHERE job_id=?",
            (self.job_id,),
        ).fetchone()
        self.assertEqual(row["decision"], "REVISED")
        self.assertEqual(row["status"], "QUALITY_REVIEWED")
        self.assertIn("Leading indicator:", row["answer_text"])

    def test_same_job_cannot_use_additive_repair_twice(self):
        def evaluator(cfg, llm, model, prompt, payload, max_tokens, timeout_seconds):
            return {
                "decision": "ADD",
                "confidence": 90,
                "critique": "fixed",
                "addition": "Leading indicator: allocator retries rise before allocation failure.",
            }

        first = repair_adjudicator_block(
            self.con,
            {"research_model": "fake-model"},
            self.job_id,
            content_hash=self.digest,
            job=self.job,
            defect="missing leading indicator",
            model="fake-model",
            evaluator=evaluator,
        )
        self.assertEqual(first["state"], "QUALITY_REVIEWED")
        second = repair_adjudicator_block(
            self.con,
            {"research_model": "fake-model"},
            self.job_id,
            content_hash=self.digest,
            job=self.job,
            defect="missing leading indicator",
            model="fake-model",
            evaluator=lambda *a, **k: self.fail("second evaluator call is forbidden"),
        )
        self.assertEqual(second["state"], "BLOCKED")
        self.assertIn("already attempted", second["reason"])

    def test_duplicate_addition_fails_closed(self):
        result = repair_adjudicator_block(
            self.con,
            {"research_model": "fake-model"},
            self.job_id,
            content_hash=self.digest,
            job=self.job,
            defect="missing leading indicator",
            model="fake-model",
            evaluator=lambda *a, **k: {
                "decision": "ADD",
                "confidence": 80,
                "critique": "claims an addition",
                "addition": self.candidate,
            },
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("no new information", result["reason"])

    def test_legacy_v1_attempt_does_not_block_one_v2_attempt(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.con.execute(
            """
            INSERT INTO job_quality_block_repairs(
              room,job_id,content_hash,attempted_at,defect_hash,status,
              confidence,critique,answer_hash
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            ("kibble", self.job_id, self.digest, now, "old", "UNCHANGED", 80, "old v1", ""),
        )
        self.con.commit()

        result = repair_adjudicator_block(
            self.con,
            {"research_model": "fake-model"},
            self.job_id,
            content_hash=self.digest,
            job=self.job,
            defect="missing leading indicator",
            model="fake-model",
            evaluator=lambda *a, **k: {
                "decision": "ADD",
                "confidence": 92,
                "critique": "adds missing signal",
                "addition": "Leading indicator: the allocator begins retrying or failing larger allocations before the final OOM.",
            },
        )
        self.assertEqual(result["state"], "QUALITY_REVIEWED")


if __name__ == "__main__":
    unittest.main()
