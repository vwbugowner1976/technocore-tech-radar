import sqlite3
import unittest
from datetime import datetime, timezone

from job_auto_orchestrator import (
    ensure_auto_schema,
    process_sent_claims,
    publish_pending_notifications,
)
from job_claim_trial import ensure_claim_schema
from job_delivery_trial import ensure_delivery_schema


class FakeLLM:
    def close(self):
        pass


class DeliveryRaceTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_auto_schema(self.con)
        self.job_id = "kabcdef0123"
        self.digest = "digest"
        self.now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.cfg = {"research_model": "fake-model"}
        self._insert_sent_claim_and_tracking()

    def tearDown(self):
        self.con.close()

    def _insert_sent_claim_and_tracking(self):
        ensure_claim_schema(self.con)
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
                "kibble", self.job_id, self.digest, 100, "did:key:z6MkIssuer",
                "explain", self.now, 90, 90, 95, 1.0, 2.0, 1.1, 2.1, 1.2,
                "did:key:z6MkWorker", "claimhash", "SENT", 150, "",
            ),
        )
        self.con.execute(
            """
            INSERT INTO job_auto_orchestrator(
              room,job_id,content_hash,claim_state,pipeline_state,updated_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                "kibble", self.job_id, self.digest,
                "CLAIM_SENT", "WAITING_POSTCLAIM", self.now,
            ),
        )
        self.con.commit()

    def _insert_sent_delivery(self):
        ensure_delivery_schema(self.con)
        self.con.execute(
            """
            INSERT INTO job_delivery_trials(
              room,job_id,content_hash,claim_seq,claim_sender_did,
              quality_reviewed_at,quality_decision,quality_confidence,
              answer_hash,prepared_at,prepare_expires_at,approved_at,
              permit_expires_at,consumed_at,sender_did,deliver_text_hash,
              status,sent_seq,detail
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "kibble", self.job_id, self.digest, 150,
                "did:key:z6MkWorker", self.now, "PASS", 95,
                "answerhash", 2.0, 3.0, 2.1, 3.1, 2.2,
                "did:key:z6MkWorker", "deliverhash", "SENT", 200, "",
            ),
        )
        self.con.commit()

    def test_concurrent_success_overrides_late_pipeline_block(self):
        def pipeline_runner(*args, **kwargs):
            self._insert_sent_delivery()
            return {
                "state": "BLOCKED",
                "stage": "delivery-prepare",
                "reason": "stale background result",
            }

        processed = process_sent_claims(
            self.con,
            self.cfg,
            pipeline_runner=pipeline_runner,
            llm_factory=lambda cfg: FakeLLM(),
        )

        self.assertEqual(processed[0]["state"], "DELIVERED")
        row = self.con.execute(
            "SELECT pipeline_state FROM job_auto_orchestrator WHERE job_id=?",
            (self.job_id,),
        ).fetchone()
        self.assertEqual(row["pipeline_state"], "DELIVERED")

    def test_stale_blocked_notification_is_suppressed_after_delivery_sent(self):
        self._insert_sent_delivery()
        self.con.execute(
            """
            UPDATE job_auto_orchestrator
            SET pipeline_state='BLOCKED', detail='delivery-prepare: stale race', blocked_notified=0
            WHERE job_id=?
            """,
            (self.job_id,),
        )
        self.con.commit()
        calls = {"blocked": 0}

        def blocked_notifier(*args, **kwargs):
            calls["blocked"] += 1
            return {"state": "PUBLISHED_LOCAL"}

        stats = publish_pending_notifications(
            self.con,
            self.cfg,
            claim_notifier=lambda *a, **k: {"state": "DISABLED"},
            delivery_notifier=lambda *a, **k: {"state": "DISABLED"},
            blocked_notifier=blocked_notifier,
        )

        self.assertEqual(calls["blocked"], 0)
        self.assertEqual(stats["blocked"], 0)
        row = self.con.execute(
            "SELECT pipeline_state FROM job_auto_orchestrator WHERE job_id=?",
            (self.job_id,),
        ).fetchone()
        self.assertEqual(row["pipeline_state"], "DELIVERED")


if __name__ == "__main__":
    unittest.main()
