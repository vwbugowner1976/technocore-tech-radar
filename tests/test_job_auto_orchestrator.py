import sqlite3
import unittest
from datetime import datetime, timezone

from job_auto_orchestrator import (
    ensure_auto_schema,
    prepare_claim_ready,
    process_sent_claims,
    publish_pending_notifications,
    run_once,
)
from job_claim_trial import ensure_claim_schema
from job_delivery_trial import ensure_delivery_schema


class FakeLLM:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class JobAutoOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_auto_schema(self.con)
        self.cfg = {"research_model": "fake-model"}
        self.job_id = "kabcdef0123"
        self.digest = "digest"
        self.now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def tearDown(self):
        self.con.close()

    def _insert_sent_claim(self, *, tracked=True):
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
        if tracked:
            self.con.execute(
                """
                INSERT INTO job_auto_orchestrator(
                  room,job_id,content_hash,claim_state,pipeline_state,updated_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    "kibble", self.job_id, self.digest, "CLAIM_READY",
                    "WAITING_FOR_HUMAN_CLAIM", self.now,
                ),
            )
        self.con.commit()

    @staticmethod
    def _published(*args, **kwargs):
        return {"state": "PUBLISHED_LOCAL", "detail": "ok"}

    def test_ready_candidate_is_prepared_but_never_sent(self):
        calls = {"prepare": 0, "claim_notice": 0}

        def prepare_runner(con, cfg, job_id, room="kibble"):
            calls["prepare"] += 1
            ensure_claim_schema(con)
            con.execute(
                """
                INSERT INTO job_claim_trials(
                  room,job_id,content_hash,job_seq,issuer_did,job_type,refined_at,
                  refined_relevance,refined_fit,refined_confidence,prepared_at,
                  prepare_expires_at,sender_did,claim_text_hash,status,detail
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    room, job_id, self.digest, 100, "did:key:z6MkIssuer", "explain",
                    self.now, 90, 90, 95, 1.0, 2.0, "", "claimhash", "PREPARED", "",
                ),
            )
            con.commit()
            return {
                "state": "PREPARED",
                "candidate": {"room": room, "job_id": job_id, "content_hash": self.digest},
            }

        def claim_notifier(cfg, job_id):
            calls["claim_notice"] += 1
            return {"state": "PUBLISHED_LOCAL", "detail": "ok"}

        result = run_once(
            self.con,
            self.cfg,
            ready_job_id=self.job_id,
            prepare_runner=prepare_runner,
            claim_notifier=claim_notifier,
            delivery_notifier=self._published,
            blocked_notifier=self._published,
            pipeline_runner=lambda *a, **k: self.fail("pipeline must not run without SENT claim"),
        )

        self.assertEqual(result["prepared"]["state"], "CLAIM_READY")
        self.assertEqual(calls["prepare"], 1)
        self.assertEqual(calls["claim_notice"], 1)
        claim = self.con.execute(
            "SELECT status,sent_seq FROM job_claim_trials WHERE job_id=?",
            (self.job_id,),
        ).fetchone()
        self.assertEqual(claim["status"], "PREPARED")
        self.assertIsNone(claim["sent_seq"])

    def test_sent_claim_runs_postclaim_once_and_notifies_delivery_once(self):
        self._insert_sent_claim()
        calls = {"pipeline": 0, "delivery_notice": 0}
        llm = FakeLLM()

        def pipeline_runner(con, cfg, job_id, **kwargs):
            calls["pipeline"] += 1
            ensure_delivery_schema(con)
            con.execute(
                """
                INSERT INTO job_delivery_trials(
                  room,job_id,content_hash,claim_seq,claim_sender_did,
                  quality_reviewed_at,quality_decision,quality_confidence,
                  answer_hash,prepared_at,prepare_expires_at,sender_did,
                  deliver_text_hash,status,detail
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "kibble", job_id, self.digest, 150, "did:key:z6MkWorker",
                    self.now, "PASS", 95, "answerhash", 2.0, 3.0, "",
                    "deliverhash", "PREPARED", "",
                ),
            )
            con.commit()
            return {"state": "READY_FOR_HUMAN_DELIVERY", "job_id": job_id}

        def delivery_notifier(cfg, job_id):
            calls["delivery_notice"] += 1
            return {"state": "PUBLISHED_LOCAL", "detail": "ok"}

        first = run_once(
            self.con,
            self.cfg,
            pipeline_runner=pipeline_runner,
            llm_factory=lambda cfg: llm,
            claim_notifier=self._published,
            delivery_notifier=delivery_notifier,
            blocked_notifier=self._published,
        )
        second = run_once(
            self.con,
            self.cfg,
            pipeline_runner=pipeline_runner,
            llm_factory=lambda cfg: self.fail("LLM must not reload for completed local pipeline"),
            claim_notifier=self._published,
            delivery_notifier=delivery_notifier,
            blocked_notifier=self._published,
        )

        self.assertEqual(first["processed"][0]["state"], "DELIVERY_READY")
        self.assertEqual(second["processed"], [])
        self.assertEqual(calls["pipeline"], 1)
        self.assertEqual(calls["delivery_notice"], 1)
        self.assertTrue(llm.closed)

    def test_blocked_pipeline_is_terminal_for_automatic_retry(self):
        self._insert_sent_claim()
        calls = {"pipeline": 0, "blocked_notice": 0}

        def pipeline_runner(*args, **kwargs):
            calls["pipeline"] += 1
            return {"state": "BLOCKED", "stage": "success", "reason": "missing grounding"}

        def blocked_notifier(cfg, job_id, stage):
            calls["blocked_notice"] += 1
            self.assertEqual(stage, "success")
            return {"state": "PUBLISHED_LOCAL", "detail": "ok"}

        first = run_once(
            self.con,
            self.cfg,
            pipeline_runner=pipeline_runner,
            llm_factory=lambda cfg: FakeLLM(),
            claim_notifier=self._published,
            delivery_notifier=self._published,
            blocked_notifier=blocked_notifier,
        )
        second = run_once(
            self.con,
            self.cfg,
            pipeline_runner=pipeline_runner,
            llm_factory=lambda cfg: self.fail("blocked job must not auto-retry"),
            claim_notifier=self._published,
            delivery_notifier=self._published,
            blocked_notifier=blocked_notifier,
        )

        self.assertEqual(first["processed"][0]["state"], "BLOCKED")
        self.assertEqual(second["processed"], [])
        self.assertEqual(calls["pipeline"], 1)
        self.assertEqual(calls["blocked_notice"], 1)

    def test_existing_prepared_delivery_is_not_regenerated(self):
        self._insert_sent_claim()
        ensure_delivery_schema(self.con)
        self.con.execute(
            """
            INSERT INTO job_delivery_trials(
              room,job_id,content_hash,claim_seq,claim_sender_did,
              quality_reviewed_at,quality_decision,quality_confidence,
              answer_hash,prepared_at,prepare_expires_at,sender_did,
              deliver_text_hash,status,detail
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "kibble", self.job_id, self.digest, 150, "did:key:z6MkWorker",
                self.now, "PASS", 95, "answerhash", 2.0, 3.0, "",
                "deliverhash", "PREPARED", "",
            ),
        )
        self.con.commit()

        processed = process_sent_claims(
            self.con,
            self.cfg,
            pipeline_runner=lambda *a, **k: self.fail("existing PREPARED delivery must not rerun pipeline"),
            llm_factory=lambda cfg: self.fail("LLM must not load"),
        )
        self.assertEqual(processed[0]["state"], "DELIVERY_READY")
        self.assertTrue(processed[0]["existing"])

    def test_untracked_historical_sent_claim_is_ignored(self):
        self._insert_sent_claim(tracked=False)

        processed = process_sent_claims(
            self.con,
            self.cfg,
            pipeline_runner=lambda *a, **k: self.fail("untracked historical claim must not run pipeline"),
            llm_factory=lambda cfg: self.fail("LLM must not load for untracked historical claim"),
        )
        self.assertEqual(processed, [])

    def test_historical_sent_delivery_is_ignored(self):
        self._insert_sent_claim(tracked=False)
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
                "kibble", self.job_id, self.digest, 150, "did:key:z6MkWorker",
                self.now, "PASS", 95, "answerhash", 2.0, 3.0, 2.1,
                3.1, 2.2, "did:key:z6MkWorker", "deliverhash",
                "SENT", 200, "",
            ),
        )
        self.con.commit()

        processed = process_sent_claims(
            self.con,
            self.cfg,
            pipeline_runner=lambda *a, **k: self.fail("historical delivered job must not rerun pipeline"),
            llm_factory=lambda cfg: self.fail("LLM must not load for historical delivered job"),
        )
        self.assertEqual(processed, [])


if __name__ == "__main__":
    unittest.main()
