import sqlite3
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from job_candidate_refiner import ensure_refiner_schema, store_refinement
from job_claim_trial import (
    approve_claim,
    candidate_for_claim,
    ensure_claim_schema,
    prepare_claim,
    send_claim,
)
from job_shadow import ensure_job_shadow_schema, record_job_shadow_candidate
from technoscout.db import SCHEMA
from technoscout.sender import SendUncertain


class FakeIdentity:
    def __init__(self, did="did:key:z6MkWorker"):
        self.did = did

    def sign(self, room, nonce, text):
        return "fake-signature"


class FakeSender:
    def __init__(self, *, did="did:key:z6MkWorker", outcome="sent"):
        self.identity = FakeIdentity(did)
        self.outcome = outcome
        self.posts = []

    def _read_server_nonce(self, room):
        return 5

    def _post(self, room, text, nonce, signature):
        self.posts.append((room, text, nonce, signature))
        if self.outcome == "uncertain":
            raise SendUncertain("simulated timeout after reservation")
        return {"seq": 999, "from": self.identity.did, "text": text}


class JobClaimTrialTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        ensure_job_shadow_schema(self.con)
        ensure_refiner_schema(self.con)
        ensure_claim_schema(self.con)
        self.now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.issuer = "did:key:z6MkIssuerStrong"
        self.job_id = "kabcdef0123"
        self.digest = "candidate-digest"

        for index, lifecycle in enumerate(("ATTESTED", "DELIVERED", "ATTESTED"), start=1):
            record_job_shadow_candidate(
                self.con,
                seen_at=self.now,
                room="kibble",
                job_id=f"k{index:010x}",
                job_seq=index,
                issuer_did=self.issuer,
                signed_identity=True,
                job_type="explain",
                digest=f"d{index}",
                lifecycle=lifecycle,
                evaluation={
                    "fit_class": "SKIP_CLOSED",
                    "relevance": 0,
                    "technical_fit": 0,
                    "confidence": 100,
                    "effort": "unknown",
                    "required_capabilities": [],
                    "reason": "fixture",
                    "summary": "",
                },
            )

        record_job_shadow_candidate(
            self.con,
            seen_at=self.now,
            room="kibble",
            job_id=self.job_id,
            job_seq=100,
            issuer_did=self.issuer,
            signed_identity=True,
            job_type="coordinate",
            digest=self.digest,
            lifecycle="OPEN",
            evaluation={
                "fit_class": "NOT_RELEVANT",
                "relevance": 25,
                "technical_fit": 30,
                "confidence": 65,
                "effort": "small",
                "required_capabilities": ["local-llm"],
                "reason": "lexical miss",
                "summary": "",
            },
        )
        store_refinement(
            self.con,
            {"room": "kibble", "job_id": self.job_id, "content_hash": self.digest},
            {
                "decision": "SAFE_FIT",
                "relevance": 80,
                "technical_fit": 90,
                "confidence": 95,
                "effort": "small",
                "reason": "semantic fit",
            },
        )
        self.cfg = {
            "job_gate_min_issuer_jobs": 3,
            "job_gate_min_issuer_closed": 2,
            "job_gate_min_issuer_completed": 2,
            "job_gate_min_issuer_attested": 1,
            "job_gate_min_issuer_completion_rate_percent": 50,
            "job_refined_gate_min_relevance": 70,
            "job_refined_gate_min_fit": 75,
            "job_refined_gate_min_confidence": 80,
            "job_claim_max_refinement_age_seconds": 900,
            "job_claim_prepare_ttl_seconds": 600,
            "job_claim_permit_ttl_seconds": 300,
        }

    def tearDown(self):
        self.con.close()

    @staticmethod
    def live_open(cfg, candidate):
        return {
            "state": "OPEN_CONFIRMED",
            "lifecycle": "OPEN",
            "pages": 1,
            "messages": 12,
            "source": "test",
        }

    def exact_job(self, cfg, candidate):
        return {
            "state": "EXACT",
            "job": {
                "verb": "JOB",
                "job_id": self.job_id,
                "job_type": "coordinate",
                "title": "Compare two inference constraints",
                "body": "Write a concise tradeoff note without external tools.",
            },
        }

    def test_candidate_requires_safe_fit(self):
        candidate, reason = candidate_for_claim(self.con, self.cfg, self.job_id)
        self.assertIsNotNone(candidate)
        self.assertEqual(reason, "eligible")
        self.con.execute(
            "UPDATE job_candidate_refinements SET decision='NOT_RELEVANT' WHERE job_id=?",
            (self.job_id,),
        )
        self.con.commit()
        candidate, reason = candidate_for_claim(self.con, self.cfg, self.job_id)
        self.assertIsNone(candidate)
        self.assertIn("not SAFE_FIT", reason)

    def test_prepare_is_read_only_and_shows_exact_job(self):
        result = prepare_claim(
            self.con,
            self.cfg,
            self.job_id,
            revalidator=self.live_open,
            exact_fetcher=self.exact_job,
            now=1000,
        )
        self.assertEqual(result["state"], "PREPARED")
        self.assertEqual(result["job"]["title"], "Compare two inference constraints")
        self.assertEqual(result["claim_text"], f"CLAIM v1 | {self.job_id} | worker")
        row = self.con.execute("SELECT status,sender_did FROM job_claim_trials WHERE job_id=?", (self.job_id,)).fetchone()
        self.assertEqual(row["status"], "PREPARED")
        self.assertEqual(row["sender_did"], "")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM job_claim_attempts").fetchone()[0], 0)

    def test_prepare_fails_closed_when_not_open(self):
        result = prepare_claim(
            self.con,
            self.cfg,
            self.job_id,
            revalidator=lambda cfg, item: {"state": "NOT_OPEN", "lifecycle": "CLAIMED"},
            exact_fetcher=self.exact_job,
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM job_claim_trials").fetchone()[0], 0)

    def _prepare(self, now=1000):
        return prepare_claim(
            self.con,
            self.cfg,
            self.job_id,
            revalidator=self.live_open,
            exact_fetcher=self.exact_job,
            now=now,
        )

    def _approve(self, now=1100):
        return approve_claim(
            self.con,
            self.cfg,
            self.job_id,
            revalidator=self.live_open,
            identity_factory=lambda cfg: FakeIdentity(),
            now=now,
        )

    def test_approve_arms_short_lived_one_use_permit(self):
        self._prepare()
        result = self._approve()
        self.assertEqual(result["state"], "ARMED")
        row = self.con.execute("SELECT status,sender_did,consumed_at FROM job_claim_trials WHERE job_id=?", (self.job_id,)).fetchone()
        self.assertEqual(row["status"], "ARMED")
        self.assertEqual(row["sender_did"], "did:key:z6MkWorker")
        self.assertIsNone(row["consumed_at"])

    def test_approve_rechecks_open_state(self):
        self._prepare()
        result = approve_claim(
            self.con,
            self.cfg,
            self.job_id,
            revalidator=lambda cfg, item: {"state": "NOT_OPEN", "lifecycle": "CLAIMED"},
            identity_factory=lambda cfg: FakeIdentity(),
            now=1100,
        )
        self.assertEqual(result["state"], "BLOCKED")
        row = self.con.execute("SELECT status FROM job_claim_trials WHERE job_id=?", (self.job_id,)).fetchone()
        self.assertEqual(row["status"], "PREPARED")

    def test_send_consumes_permit_and_posts_exactly_once(self):
        self._prepare()
        self._approve()
        sender = FakeSender()
        result = send_claim(
            self.con,
            self.cfg,
            self.job_id,
            revalidator=self.live_open,
            sender_factory=lambda cfg, con: sender,
            now=1200,
        )
        self.assertEqual(result["state"], "SENT")
        self.assertEqual(len(sender.posts), 1)
        self.assertEqual(sender.posts[0][1], f"CLAIM v1 | {self.job_id} | worker")
        row = self.con.execute("SELECT status,sent_seq,consumed_at FROM job_claim_trials WHERE job_id=?", (self.job_id,)).fetchone()
        self.assertEqual(row["status"], "SENT")
        self.assertEqual(row["sent_seq"], 999)
        self.assertIsNotNone(row["consumed_at"])
        again = send_claim(
            self.con,
            self.cfg,
            self.job_id,
            revalidator=self.live_open,
            sender_factory=lambda cfg, con: sender,
            now=1201,
        )
        self.assertEqual(again["state"], "BLOCKED")
        self.assertEqual(len(sender.posts), 1)

    def test_uncertain_send_is_terminal_and_not_retried(self):
        self._prepare()
        self._approve()
        sender = FakeSender(outcome="uncertain")
        with self.assertRaises(SendUncertain):
            send_claim(
                self.con,
                self.cfg,
                self.job_id,
                revalidator=self.live_open,
                sender_factory=lambda cfg, con: sender,
                now=1200,
            )
        row = self.con.execute("SELECT status FROM job_claim_trials WHERE job_id=?", (self.job_id,)).fetchone()
        self.assertEqual(row["status"], "UNCERTAIN")
        self.assertEqual(len(sender.posts), 1)
        retry = send_claim(
            self.con,
            self.cfg,
            self.job_id,
            revalidator=self.live_open,
            sender_factory=lambda cfg, con: sender,
            now=1201,
        )
        self.assertEqual(retry["state"], "BLOCKED")
        self.assertEqual(len(sender.posts), 1)

    def test_send_time_closed_job_blocks_before_post(self):
        self._prepare()
        self._approve()
        sender = FakeSender()
        result = send_claim(
            self.con,
            self.cfg,
            self.job_id,
            revalidator=lambda cfg, item: {"state": "NOT_OPEN", "lifecycle": "CLAIMED"},
            sender_factory=lambda cfg, con: sender,
            now=1200,
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(len(sender.posts), 0)
        row = self.con.execute("SELECT status FROM job_claim_trials WHERE job_id=?", (self.job_id,)).fetchone()
        self.assertEqual(row["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
