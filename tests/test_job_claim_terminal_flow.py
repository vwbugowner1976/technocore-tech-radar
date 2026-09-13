import sqlite3
import unittest
from unittest.mock import patch

import job_action
from job_auto_orchestrator import ensure_auto_schema
from job_claim_trial import ensure_claim_schema


class ClaimTerminalFlowTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_auto_schema(self.con)
        ensure_claim_schema(self.con)
        self.job_id = "ke207366eab"
        self.digest = "digest"
        self.tracked = {
            "room": "kibble",
            "job_id": self.job_id,
            "content_hash": self.digest,
            "pipeline_state": "WAITING_FOR_HUMAN_CLAIM",
            "detail": "",
        }
        self.con.execute(
            """
            INSERT INTO job_auto_orchestrator(
              room,job_id,content_hash,claim_state,pipeline_state,updated_at
            ) VALUES(?,?,?,?,?,?)
            """,
            ("kibble", self.job_id, self.digest, "CLAIM_READY", "WAITING_FOR_HUMAN_CLAIM", "now"),
        )
        self.con.execute(
            """
            INSERT INTO job_claim_trials(
              room,job_id,content_hash,job_seq,issuer_did,job_type,refined_at,
              refined_relevance,refined_fit,refined_confidence,prepared_at,
              prepare_expires_at,sender_did,claim_text_hash,status,detail
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "kibble", self.job_id, self.digest, 100, "did:key:z6MkIssuer",
                "coordinate", "now", 80, 90, 95, 1.0, 9999999999.0,
                "", "claimhash", "PREPARED", "",
            ),
        )
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def test_not_open_becomes_terminal_and_never_reaches_send_confirmation(self):
        prepared = {
            "state": "PREPARED",
            "job": {"job_type": "coordinate", "title": "t", "body": "b"},
            "candidate": {
                "room": "kibble",
                "job_id": self.job_id,
                "content_hash": self.digest,
                "job_seq": 100,
                "issuer_did": "did:key:z6MkIssuer",
                "issuer_reputation": {"score": 90, "attested_jobs": 3},
                "refined_relevance": 80,
                "refined_fit": 90,
                "refined_confidence": 95,
            },
            "claim_text": f"CLAIM v1 | {self.job_id} | worker",
        }
        with (
            patch("job_action.prepare_claim", return_value=prepared),
            patch("job_action.store_exact_job_snapshot", return_value={"state": "SNAPSHOT_VERIFIED"}),
            patch("job_action._confirm_job_id", return_value=True),
            patch("job_action.approve_claim", return_value={
                "state": "BLOCKED",
                "reason": "live OPEN check failed: NOT_OPEN",
                "live": {"state": "NOT_OPEN"},
            }),
            patch("job_action._confirm_send") as confirm_send,
            patch("job_action.send_claim") as send_claim,
        ):
            state = job_action._claim_flow(
                self.con, {}, self.job_id, "kibble", self.tracked
            )

        self.assertEqual(state, "ABANDONED_CLAIM_NOT_OPEN")
        confirm_send.assert_not_called()
        send_claim.assert_not_called()
        claim = self.con.execute(
            "SELECT status FROM job_claim_trials WHERE job_id=?",
            (self.job_id,),
        ).fetchone()
        auto = self.con.execute(
            "SELECT pipeline_state FROM job_auto_orchestrator WHERE job_id=?",
            (self.job_id,),
        ).fetchone()
        self.assertEqual(claim["status"], "BLOCKED")
        self.assertEqual(auto["pipeline_state"], "ABANDONED_CLAIM_NOT_OPEN")


if __name__ == "__main__":
    unittest.main()
