import hashlib
import unittest
from datetime import datetime, timezone
from pathlib import Path

from job_claim_trial import ensure_claim_schema
from job_delivery_trial import (
    approve_delivery,
    ensure_delivery_schema,
    live_delivery_ready,
    prepare_delivery,
    send_delivery,
)
from job_execution_quality_gate import ensure_quality_schema
from job_success_criterion_gate import ensure_success_schema
from technoscout.db import connect


class FakeIdentity:
    def __init__(self, did="did:key:z6MkWorker"):
        self.did = did

    def sign(self, room, nonce, text):
        return "fakesig"


class FakeSender:
    def __init__(self, did="did:key:z6MkWorker"):
        self.identity = FakeIdentity(did)
        self.posts = []

    def _read_server_nonce(self, room):
        return 10

    def _post(self, room, text, nonce, signature):
        self.posts.append((room, text, nonce, signature))
        return {"seq": 200, "from": self.identity.did, "text": text}


class JobDeliveryTrialTests(unittest.TestCase):
    def setUp(self):
        self.con = connect(Path(":memory:"))
        ensure_claim_schema(self.con)
        ensure_quality_schema(self.con)
        ensure_success_schema(self.con)
        ensure_delivery_schema(self.con)
        self.job_id = "kabcdef0123"
        self.digest = "digest"
        self.worker = "did:key:z6MkWorker"
        self.issuer = "did:key:z6MkIssuer"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.answer = (
            "Use safe aggregate concurrent buffered bytes as the capacity number. "
            "In staging, reproduce representative response size and concurrency, "
            "increase load gradually, observe memory, spill, latency and errors, "
            "then set the operating threshold below the pressure point with margin."
        )
        ah = hashlib.sha256(self.answer.encode("utf-8")).hexdigest()
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
                "kibble", self.job_id, self.digest, 100, self.issuer, "coordinate",
                now, 80, 90, 95, 1.0, 2.0, 1.1, 2.1, 1.2,
                self.worker, "claimhash", "SENT", 150, "",
            ),
        )
        self.con.execute(
            """
            INSERT INTO job_execution_quality_reviews(
              room,job_id,content_hash,reviewed_at,model,decision,confidence,
              deterministic_flags,critique,answer_hash,answer_text,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'QUALITY_REVIEWED')
            """,
            (
                "kibble", self.job_id, self.digest, now, "fake-model", "REVISED", 85,
                "old flag", "fixed concurrency", ah, self.answer,
            ),
        )
        self.con.commit()
        self.cfg = {
            "job_delivery_min_quality_confidence": 80,
            "job_delivery_prepare_ttl_seconds": 600,
            "job_delivery_permit_ttl_seconds": 300,
        }

    def tearDown(self):
        self.con.close()

    @staticmethod
    def ready(cfg, claim):
        return {"state": "READY_CONFIRMED", "source": "test"}

    def exact(self, cfg, candidate):
        return {
            "state": "EXACT",
            "job": {
                "verb": "JOB",
                "job_id": self.job_id,
                "job_type": "coordinate",
                "title": "Sizing a reverse proxy buffering the entire response before it is under pressure",
                "body": "Give one number to establish in advance for a proxy buffering the entire response until the backend finishes, and how to obtain it safely.",
            },
        }

    def test_prepare_binds_quality_answer(self):
        result = prepare_delivery(
            self.con, self.cfg, self.job_id,
            readiness_checker=self.ready, exact_fetcher=self.exact, now=100.0,
        )
        self.assertEqual(result["state"], "PREPARED")
        self.assertIn("DELIVER v1 | kabcdef0123 |", result["text"])
        self.assertIn("aggregate concurrent buffered bytes", result["text"])
        row = self.con.execute("SELECT status,answer_hash FROM job_delivery_trials WHERE job_id=?", (self.job_id,)).fetchone()
        self.assertEqual(row["status"], "PREPARED")
        self.assertEqual(row["answer_hash"], hashlib.sha256(self.answer.encode()).hexdigest())

    def test_low_quality_confidence_blocks_prepare(self):
        self.con.execute("UPDATE job_execution_quality_reviews SET confidence=79 WHERE job_id=?", (self.job_id,))
        self.con.commit()
        result = prepare_delivery(
            self.con, self.cfg, self.job_id,
            readiness_checker=self.ready, exact_fetcher=self.exact,
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("quality confidence", result["reason"])

    def test_deterministic_quality_failure_blocks_prepare(self):
        bad = "Use the maximum response size as the capacity number."
        ah = hashlib.sha256(bad.encode()).hexdigest()
        self.con.execute(
            "UPDATE job_execution_quality_reviews SET answer_text=?,answer_hash=? WHERE job_id=?",
            (bad, ah, self.job_id),
        )
        self.con.commit()
        result = prepare_delivery(
            self.con, self.cfg, self.job_id,
            readiness_checker=self.ready, exact_fetcher=self.exact,
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("deterministic guard", result["reason"])

    def test_approve_arms_same_claim_identity(self):
        prep = prepare_delivery(
            self.con, self.cfg, self.job_id,
            readiness_checker=self.ready, exact_fetcher=self.exact, now=100.0,
        )
        self.assertEqual(prep["state"], "PREPARED")
        result = approve_delivery(
            self.con, self.cfg, self.job_id,
            readiness_checker=self.ready,
            identity_factory=lambda cfg: FakeIdentity(self.worker),
            now=101.0,
        )
        self.assertEqual(result["state"], "ARMED")
        self.assertEqual(result["did"], self.worker)

    def test_approve_rejects_different_signer(self):
        prepare_delivery(
            self.con, self.cfg, self.job_id,
            readiness_checker=self.ready, exact_fetcher=self.exact, now=100.0,
        )
        result = approve_delivery(
            self.con, self.cfg, self.job_id,
            readiness_checker=self.ready,
            identity_factory=lambda cfg: FakeIdentity("did:key:z6MkOther"),
            now=101.0,
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("not the DID", result["reason"])

    def test_send_posts_once_and_cannot_repeat(self):
        prepare_delivery(
            self.con, self.cfg, self.job_id,
            readiness_checker=self.ready, exact_fetcher=self.exact, now=100.0,
        )
        approve_delivery(
            self.con, self.cfg, self.job_id,
            readiness_checker=self.ready,
            identity_factory=lambda cfg: FakeIdentity(self.worker),
            now=101.0,
        )
        sender = FakeSender(self.worker)
        result = send_delivery(
            self.con, self.cfg, self.job_id,
            readiness_checker=self.ready,
            sender_factory=lambda cfg, con: sender,
            now=102.0,
        )
        self.assertEqual(result["state"], "SENT")
        self.assertEqual(result["seq"], 200)
        self.assertEqual(len(sender.posts), 1)
        self.assertTrue(sender.posts[0][1].startswith("DELIVER v1 | kabcdef0123 |"))
        again = send_delivery(
            self.con, self.cfg, self.job_id,
            readiness_checker=self.ready,
            sender_factory=lambda cfg, con: sender,
            now=103.0,
        )
        self.assertEqual(again["state"], "BLOCKED")
        self.assertEqual(len(sender.posts), 1)

    def test_live_ready_detects_existing_deliver(self):
        trial = {
            "room": "kibble", "job_id": self.job_id,
            "sent_seq": 150, "sender_did": self.worker,
        }
        def export_fetcher(cfg, room):
            return [
                {"seq": 150, "from": self.worker, "text": f"CLAIM v1 | {self.job_id} | worker"},
                {"seq": 151, "from": self.worker, "text": f"DELIVER v1 | {self.job_id} | answer"},
            ]
        result = live_delivery_ready(
            {}, trial,
            fetcher=lambda cfg, path, query: {"messages": []},
            export_fetcher=export_fetcher,
        )
        self.assertEqual(result["state"], "ALREADY_DELIVERED")

    def test_live_ready_retries_busy_gap_with_fresh_snapshot(self):
        trial = {
            "room": "kibble", "job_id": self.job_id,
            "sent_seq": 150, "sender_did": self.worker,
        }
        exports = {"n": 0}
        fetches = {"n": 0}
        def export_fetcher(cfg, room):
            exports["n"] += 1
            high = 151 if exports["n"] == 1 else 160
            rows = [{"seq": 150, "from": self.worker, "text": f"CLAIM v1 | {self.job_id} | worker"}]
            rows.extend({"seq": s, "from": "did:key:z6MkOther", "text": "noise"} for s in range(151, high + 1))
            return rows
        def fetcher(cfg, path, query):
            fetches["n"] += 1
            if fetches["n"] == 1:
                return {"messages": [{"seq": 155, "from": "did:key:z6MkOther", "text": "noise"}]}
            return {"messages": []}
        result = live_delivery_ready(
            {"job_delivery_snapshot_retries": 2}, trial,
            fetcher=fetcher, export_fetcher=export_fetcher,
        )
        self.assertEqual(result["state"], "READY_CONFIRMED")
        self.assertEqual(result["snapshot_attempts"], 2)


    def test_explicit_success_without_success_review_blocks_prepare(self):
        def exact_success(cfg, candidate):
            return {
                "state": "EXACT",
                "job": {
                    "verb": "JOB",
                    "job_id": self.job_id,
                    "job_type": "coordinate",
                    "title": "example",
                    "body": (
                        "Explain the decision. "
                        "Success: names one constraint and one rejected alternative."
                    ),
                },
            }

        result = prepare_delivery(
            self.con,
            self.cfg,
            self.job_id,
            readiness_checker=self.ready,
            exact_fetcher=exact_success,
        )

        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("SUCCESS_REVIEWED", result["reason"])

    def test_explicit_success_with_matching_review_can_prepare(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ah = hashlib.sha256(self.answer.encode("utf-8")).hexdigest()

        self.con.execute(
            """
            INSERT INTO job_execution_success_reviews(
              room,job_id,content_hash,reviewed_at,model,decision,confidence,
              success_clause,contract_json,critique,answer_hash,answer_text,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "kibble",
                self.job_id,
                self.digest,
                now,
                "generic-success-criterion-gate-v1",
                "PASS",
                85,
                "names one constraint and one rejected alternative.",
                "{}",
                "",
                ah,
                self.answer,
                "SUCCESS_REVIEWED",
            ),
        )
        self.con.commit()

        def exact_success(cfg, candidate):
            return {
                "state": "EXACT",
                "job": {
                    "verb": "JOB",
                    "job_id": self.job_id,
                    "job_type": "coordinate",
                    "title": "example",
                    "body": (
                        "Explain the decision. "
                        "Success: names one constraint and one rejected alternative."
                    ),
                },
            }

        result = prepare_delivery(
            self.con,
            self.cfg,
            self.job_id,
            readiness_checker=self.ready,
            exact_fetcher=exact_success,
            now=100.0,
        )

        self.assertEqual(result["state"], "PREPARED")


if __name__ == "__main__":
    unittest.main()