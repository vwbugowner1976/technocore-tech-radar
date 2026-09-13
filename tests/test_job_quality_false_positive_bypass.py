import sqlite3
import unittest
from datetime import datetime, timezone

from job_claim_trial import ensure_claim_schema
from job_execution_quality_gate import ensure_quality_schema
from job_execution_review import ensure_review_schema
from job_quality_block_repair import ensure_repair_schema, repair_adjudicator_block


class JobQualityFalsePositiveBypassTests(unittest.TestCase):
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
        self.answer = (
            "Memory fragmentation is the first thing to break in a GPU shared by training and "
            "inference when demand climbs past its initial sizing. The failure mode is typically "
            "a runtime error indicating out-of-memory (OOM), and the leading indicator is a "
            "decrease in GPU memory availability or a warning message about memory fragmentation."
        )
        self.con.execute(
            """
            INSERT INTO job_execution_reviews(
              room,job_id,content_hash,reviewed_at,model,decision,confidence,
              critique,answer_hash,answer_text,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'REVIEWED')
            """,
            (
                "kibble", self.job_id, self.digest, now, "fake-model", "APPROVED", 100,
                "", "hash", self.answer,
            ),
        )
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def test_false_missing_items_block_is_deferred_to_generic_success_gate(self):
        defect = (
            "The candidate answer does not provide a concrete failure mode and leading "
            "indicator as requested. It only mentions memory fragmentation and an "
            "out-of-memory (OOM) error as the failure mode, without specifying the exact "
            "signal that shows up before it."
        )
        result = repair_adjudicator_block(
            self.con,
            {"research_model": "fake-model"},
            self.job_id,
            content_hash=self.digest,
            job=self.job,
            defect=defect,
            model="fake-model",
            evaluator=lambda *a, **k: self.fail("V3 repair must not run for contradicted defect"),
        )
        self.assertEqual(result["state"], "QUALITY_REVIEWED")
        self.assertEqual(result["decision"], "PASS")
        self.assertEqual(result["answer"], self.answer)
        self.assertEqual(result["repair_strategy"], "explicit-success-coverage-bypass-v2")
        self.assertIn("Generic Success Gate", result["critique"])
        self.assertIn("current-adjudicator-defect", result["critique"])

    def test_saved_v3_missing_item_critique_can_bypass_already_attempted_defect(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.con.execute(
            """
            INSERT INTO job_quality_patch_repairs_v3(
              room,job_id,content_hash,attempted_at,defect_hash,status,micro_attempts,
              confidence,critique,first_addition_hash,final_addition_hash,answer_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "kibble",
                self.job_id,
                self.digest,
                now,
                "defect-hash",
                "NO_NEW_INFORMATION",
                2,
                90,
                (
                    "The candidate answer lacks a concrete failure mode and leading indicator. "
                    "The addition provides a specific failure mode and a leading indicator that "
                    "is observable before the failure."
                ),
                "first",
                "final",
                "",
            ),
        )
        self.con.commit()

        result = repair_adjudicator_block(
            self.con,
            {"research_model": "fake-model"},
            self.job_id,
            content_hash=self.digest,
            job=self.job,
            defect="adjudicator-guided additive-v3 repair already attempted: NO_NEW_INFORMATION",
            model="fake-model",
            evaluator=lambda *a, **k: self.fail("V3 repair must not rerun after saved false-positive critique"),
        )
        self.assertEqual(result["state"], "QUALITY_REVIEWED")
        self.assertEqual(result["decision"], "PASS")
        self.assertEqual(result["answer"], self.answer)
        self.assertEqual(result["repair_strategy"], "explicit-success-coverage-bypass-v2")
        self.assertIn("saved-v3-critique", result["critique"])

    def test_bypass_does_not_trigger_when_one_success_item_is_actually_missing(self):
        self.con.execute(
            """
            UPDATE job_execution_reviews
            SET answer_text='The failure mode is an out-of-memory error.'
            WHERE job_id=?
            """,
            (self.job_id,),
        )
        self.con.commit()
        calls = {"n": 0}

        def evaluator(*args, **kwargs):
            calls["n"] += 1
            return {
                "decision": "ADD",
                "confidence": 90,
                "critique": "adds the missing indicator",
                "addition": "Leading indicator: GPU memory availability trends downward before OOM.",
            }

        result = repair_adjudicator_block(
            self.con,
            {"research_model": "fake-model"},
            self.job_id,
            content_hash=self.digest,
            job=self.job,
            defect="The candidate is missing the failure mode and leading indicator.",
            model="fake-model",
            evaluator=evaluator,
        )
        self.assertEqual(result["state"], "QUALITY_REVIEWED")
        self.assertEqual(result["repair_strategy"], "additive-v3-two-pass")
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
