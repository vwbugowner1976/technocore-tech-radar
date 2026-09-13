import sqlite3
import unittest
from datetime import datetime, timezone

from job_claim_trial import ensure_claim_schema
from job_execution_quality_gate import ensure_quality_schema
from job_execution_review import ensure_review_schema
from job_gpu_semantic_repair import _gpu_shared_answer, repair_gpu_shared_or_known


class JobGpuSemanticRepairTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_claim_schema(self.con)
        ensure_review_schema(self.con)
        ensure_quality_schema(self.con)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.job_id = "kea74499c4b"
        self.digest = "gpu-digest"
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
                "kibble", self.job_id, self.digest, now, "fake-model", "APPROVED", 100,
                "", "hash",
                "Memory fragmentation can cause OOM and lower available memory is a leading indicator.",
            ),
        )
        self.con.commit()

    def tearDown(self):
        self.con.close()

    @staticmethod
    def claim_ok(cfg, claim):
        return {"state": "CLAIM_CONFIRMED", "seq": 150}

    @staticmethod
    def exact_gpu(cfg, candidate):
        return {
            "state": "EXACT",
            "job": {
                "verb": "JOB",
                "job_id": "kea74499c4b",
                "job_type": "explain",
                "title": "How a GPU shared by training and inference fails first under load",
                "body": (
                    "Explain the first thing to break in a GPU shared by training and inference when demand "
                    "climbs past what it was sized for. Memory fragments and the smaller job is the one that "
                    "dies. Name the failure mode and the signal that shows up before it. Success: names one "
                    "concrete failure mode and one leading indicator."
                ),
            },
        }

    def test_shared_gpu_job_gets_grounded_deterministic_answer(self):
        result = repair_gpu_shared_or_known(
            self.con,
            {},
            self.job_id,
            exact_fetcher=self.exact_gpu,
            claim_verifier=self.claim_ok,
        )
        self.assertEqual(result["state"], "QUALITY_REVIEWED")
        self.assertEqual(result["decision"], "REVISED")
        self.assertIn("smaller inference job is the one that dies first", result["answer"])
        self.assertIn("failure mode", result["answer"])
        self.assertIn("leading indicator", result["answer"])
        row = self.con.execute(
            "SELECT model,status,answer_text FROM job_execution_quality_reviews WHERE job_id=?",
            (self.job_id,),
        ).fetchone()
        self.assertEqual(row["model"], "deterministic-gpu-shared-repair-v1")
        self.assertEqual(row["status"], "QUALITY_REVIEWED")
        self.assertIn("shrinking largest contiguous allocatable block", row["answer_text"])

    def test_gpu_match_is_narrow(self):
        repair = _gpu_shared_answer({
            "title": "GPU memory overview",
            "body": "Explain ordinary GPU memory use. Success: names one leading indicator.",
        })
        self.assertIsNone(repair)


if __name__ == "__main__":
    unittest.main()
