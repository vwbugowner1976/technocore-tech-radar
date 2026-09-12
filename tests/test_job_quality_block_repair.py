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

    def _run(self, evaluator):
        return repair_adjudicator_block(
            self.con,
            {"research_model": "fake-model"},
            self.job_id,
            content_hash=self.digest,
            job=self.job,
            defect="The candidate answer is missing the requested leading indicator.",
            model="fake-model",
            evaluator=evaluator,
        )

    def test_novel_first_addition_repairs_in_one_micro_attempt(self):
        addition = (
            "Leading indicator: allocation retries or failed large allocations rise "
            "while nominal free GPU memory still remains."
        )

        def evaluator(cfg, llm, model, prompt, payload, max_tokens, timeout_seconds):
            self.assertIn("leading indicator", payload["adjudicator_defect"].lower())
            return {
                "decision": "ADD",
                "confidence": 94,
                "critique": "added an explicit leading indicator",
                "addition": addition,
            }

        result = self._run(evaluator)
        self.assertEqual(result["state"], "QUALITY_REVIEWED")
        self.assertEqual(result["repair_strategy"], "additive-v3-two-pass")
        self.assertEqual(result["repair_micro_attempts"], 1)
        self.assertIn("Leading indicator:", result["answer"])

    def test_duplicate_first_addition_gets_one_final_constrained_retry(self):
        calls = {"n": 0}

        def evaluator(cfg, llm, model, prompt, payload, max_tokens, timeout_seconds):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "decision": "ADD",
                    "confidence": 80,
                    "critique": "first attempt repeats the answer",
                    "addition": self.candidate,
                }
            self.assertIn("rejected_duplicate_addition", payload)
            self.assertIn("forbidden_content", payload)
            return {
                "decision": "ADD",
                "confidence": 93,
                "critique": "adds a genuinely observable pre-failure signal",
                "addition": (
                    "Leading indicator: allocator retries or large-allocation failures "
                    "begin rising before the final OOM."
                ),
            }

        result = self._run(evaluator)
        self.assertEqual(result["state"], "QUALITY_REVIEWED")
        self.assertEqual(result["repair_micro_attempts"], 2)
        self.assertEqual(calls["n"], 2)
        self.assertIn("allocator retries", result["answer"])

    def test_duplicate_both_micro_attempts_fail_closed(self):
        calls = {"n": 0}

        def evaluator(*args, **kwargs):
            calls["n"] += 1
            return {
                "decision": "ADD",
                "confidence": 70,
                "critique": "still repeats candidate",
                "addition": self.candidate,
            }

        result = self._run(evaluator)
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("no new information", result["reason"])
        self.assertEqual(calls["n"], 2)
        row = self.con.execute(
            "SELECT status,micro_attempts FROM job_quality_patch_repairs_v3 WHERE job_id=?",
            (self.job_id,),
        ).fetchone()
        self.assertEqual(row["status"], "NO_NEW_INFORMATION")
        self.assertEqual(row["micro_attempts"], 2)

    def test_same_job_cannot_use_v3_twice(self):
        first = self._run(
            lambda *a, **k: {
                "decision": "ADD",
                "confidence": 90,
                "critique": "fixed",
                "addition": "Leading indicator: allocator retry counters rise before OOM.",
            }
        )
        self.assertEqual(first["state"], "QUALITY_REVIEWED")
        second = self._run(lambda *a, **k: self.fail("second evaluator call is forbidden"))
        self.assertEqual(second["state"], "BLOCKED")
        self.assertIn("already attempted", second["reason"])

    def test_existing_v2_ledger_does_not_block_v3(self):
        self.con.execute(
            """
            CREATE TABLE job_quality_patch_repairs (
              room TEXT NOT NULL,
              job_id TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              attempted_at TEXT NOT NULL,
              defect_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              confidence INTEGER NOT NULL DEFAULT 0,
              critique TEXT NOT NULL DEFAULT '',
              addition_hash TEXT NOT NULL DEFAULT '',
              answer_hash TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(room, job_id, content_hash)
            )
            """
        )
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.con.execute(
            """
            INSERT INTO job_quality_patch_repairs(
              room,job_id,content_hash,attempted_at,defect_hash,status,
              confidence,critique,addition_hash,answer_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "kibble", self.job_id, self.digest, now, "old-v2", "NO_NEW_INFORMATION",
                80, "old v2 duplicate", "hash", "",
            ),
        )
        self.con.commit()

        result = self._run(
            lambda *a, **k: {
                "decision": "ADD",
                "confidence": 92,
                "critique": "new v3 signal",
                "addition": "Leading indicator: failed large-allocation attempts rise before OOM.",
            }
        )
        self.assertEqual(result["state"], "QUALITY_REVIEWED")


if __name__ == "__main__":
    unittest.main()
