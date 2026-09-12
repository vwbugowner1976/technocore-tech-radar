import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from job_auto_orchestrator import ensure_auto_schema
from job_resume_quality import resume_quality_block


class JobResumeQualityTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_auto_schema(self.con)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.job_id = "kea74499c4b"
        self.digest = "digest"
        self.con.execute(
            """
            INSERT INTO job_auto_orchestrator(
              room,job_id,content_hash,claim_state,pipeline_state,detail,updated_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                "kibble", self.job_id, self.digest, "CLAIM_SENT", "BLOCKED",
                "quality: quality repair returned the candidate answer unchanged", now,
            ),
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
                now, 80,90,95,1.0,2.0,1.1,2.1,1.2,
                "did:key:z6MkWorker","claimhash","SENT",150,"",
            ),
        )
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def _assert_resumes(self):
        with patch("job_resume_quality.run_action", return_value="DELIVERY_READY") as action:
            state = resume_quality_block(self.con, {}, self.job_id)
        self.assertEqual(state, "DELIVERY_READY")
        action.assert_called_once_with(self.con, {}, self.job_id, room="kibble")

    def test_known_quality_block_rearms_only_local_pipeline(self):
        self._assert_resumes()
        row = self.con.execute(
            "SELECT pipeline_state,detail FROM job_auto_orchestrator WHERE job_id=?",
            (self.job_id,),
        ).fetchone()
        self.assertEqual(row["pipeline_state"], "WAITING_POSTCLAIM")
        self.assertIn("human-invoked retry", row["detail"])
        claim = self.con.execute(
            "SELECT status,sent_seq FROM job_claim_trials WHERE job_id=?",
            (self.job_id,),
        ).fetchone()
        self.assertEqual(claim["status"], "SENT")
        self.assertEqual(claim["sent_seq"], 150)

    def test_legacy_adjudicator_loop_reason_can_resume(self):
        self.con.execute(
            """
            UPDATE job_auto_orchestrator
            SET detail='quality: final quality adjudicator marked REVISED but again returned the candidate answer unchanged'
            WHERE job_id=?
            """,
            (self.job_id,),
        )
        self.con.commit()
        self._assert_resumes()

    def test_concrete_gpu_adjudicator_reason_can_resume(self):
        self.con.execute(
            """
            UPDATE job_auto_orchestrator SET detail=? WHERE job_id=?
            """,
            (
                "quality: The candidate answer does not provide a concrete failure mode and "
                "leading indicator as requested. It only mentions memory fragmentation and an "
                "out-of-memory (OOM) error as the failure mode, without specifying the exact "
                "signal that shows up before it.",
                self.job_id,
            ),
        )
        self.con.commit()
        self._assert_resumes()

    def test_legacy_full_answer_repair_unchanged_can_resume_to_v2(self):
        self.con.execute(
            """
            UPDATE job_auto_orchestrator
            SET detail='quality: quality-adjudication-repair: adjudicator-guided repair returned the candidate answer unchanged'
            WHERE job_id=?
            """,
            (self.job_id,),
        )
        self.con.commit()
        self._assert_resumes()

    def test_legacy_repair_ledger_unchanged_can_resume_to_v2(self):
        self.con.execute(
            """
            UPDATE job_auto_orchestrator
            SET detail='quality-adjudication-repair: adjudicator-guided repair already attempted: UNCHANGED'
            WHERE job_id=?
            """,
            (self.job_id,),
        )
        self.con.commit()
        self._assert_resumes()

    def test_other_block_reason_is_not_rearmed(self):
        self.con.execute(
            "UPDATE job_auto_orchestrator SET detail='success: unrelated failure' WHERE job_id=?",
            (self.job_id,),
        )
        self.con.commit()
        with patch("job_resume_quality.run_action") as action:
            state = resume_quality_block(self.con, {}, self.job_id)
        self.assertEqual(state, "BLOCKED_OTHER_REASON")
        action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
