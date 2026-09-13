import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from job_auto_orchestrator import ensure_auto_schema
from job_resume_success import resume_success_block


class JobResumeSuccessTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_auto_schema(self.con)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.job_id = "kea74499c4b"
        self.digest = "gpu-digest"
        self.detail = (
            "success: generic Success gate blocked: structured exact-quote evidence "
            "does not satisfy frozen contract; requirements=['R2'] grounding=['G1']; "
            "semantic fallback unavailable"
        )
        self.con.execute(
            """
            INSERT INTO job_auto_orchestrator(
              room,job_id,content_hash,claim_state,pipeline_state,detail,updated_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                "kibble", self.job_id, self.digest, "CLAIM_SENT", "BLOCKED",
                self.detail, now,
            ),
        )
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def _status(self, con, table, room, job_id):
        if table == "job_claim_trials":
            return "SENT"
        if table == "job_delivery_trials":
            return ""
        raise AssertionError(table)

    def test_structured_success_block_gets_one_local_repair_retry(self):
        def run_once(con, cfg, room="kibble", limit=2):
            self.assertEqual(cfg.get("job_success_repair_attempts"), 1)
            con.execute(
                """
                UPDATE job_auto_orchestrator
                SET pipeline_state='DELIVERY_READY', detail='ready'
                WHERE room=? AND job_id=? AND content_hash=?
                """,
                (room, self.job_id, self.digest),
            )
            con.commit()
            return {"processed": [{"job_id": self.job_id, "state": "DELIVERY_READY"}]}

        with patch("job_resume_success._latest_status", side_effect=self._status), patch(
            "job_resume_success.run_auto_once", side_effect=run_once
        ):
            state = resume_success_block(self.con, {"job_success_repair_attempts": 0}, self.job_id)

        self.assertEqual(state, "DELIVERY_READY")
        retry = self.con.execute(
            "SELECT original_detail FROM job_success_human_retries WHERE job_id=?",
            (self.job_id,),
        ).fetchone()
        self.assertIsNotNone(retry)
        self.assertIn("requirements=['R2']", retry["original_detail"])

    def test_same_job_cannot_retry_success_twice(self):
        def run_once(con, cfg, room="kibble", limit=2):
            con.execute(
                """
                UPDATE job_auto_orchestrator
                SET pipeline_state='BLOCKED', detail=?
                WHERE room=? AND job_id=? AND content_hash=?
                """,
                (self.detail, room, self.job_id, self.digest),
            )
            con.commit()
            return {"processed": [{"job_id": self.job_id, "state": "BLOCKED"}]}

        with patch("job_resume_success._latest_status", side_effect=self._status), patch(
            "job_resume_success.run_auto_once", side_effect=run_once
        ):
            first = resume_success_block(self.con, {}, self.job_id)
            second = resume_success_block(self.con, {}, self.job_id)

        self.assertEqual(first, "BLOCKED")
        self.assertEqual(second, "RETRY_ALREADY_USED")

    def test_unrelated_block_is_not_rearmed(self):
        self.con.execute(
            "UPDATE job_auto_orchestrator SET detail='quality: unrelated failure' WHERE job_id=?",
            (self.job_id,),
        )
        self.con.commit()
        with patch("job_resume_success._latest_status") as status, patch(
            "job_resume_success.run_auto_once"
        ) as run_once:
            state = resume_success_block(self.con, {}, self.job_id)
        self.assertEqual(state, "BLOCKED_OTHER_REASON")
        status.assert_not_called()
        run_once.assert_not_called()


if __name__ == "__main__":
    unittest.main()
