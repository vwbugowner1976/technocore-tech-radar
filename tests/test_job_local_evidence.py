import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_claim_trial import ensure_claim_schema
from job_execution_draft import verify_claim_retained
from job_local_evidence import (
    ensure_evidence_schema,
    load_exact_job_snapshot,
    store_exact_job_snapshot,
    verify_local_claim_receipt,
)
from job_postclaim_pipeline import run_postclaim_pipeline
from job_shadow import content_hash
from technoscout.sender import SigningIdentity, _did_from_seed, sweep_text


class JobLocalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_claim_schema(self.con)
        ensure_evidence_schema(self.con)
        self.job = {
            "verb": "JOB",
            "job_id": "kabcdef0123",
            "job_type": "explain",
            "title": "Explain a retained state",
            "body": "Observed state persists. Success: names the observation.",
        }
        self.digest = content_hash(self.job)
        self.candidate = {
            "room": "kibble",
            "job_id": self.job["job_id"],
            "job_type": self.job["job_type"],
            "job_seq": 100,
            "issuer_did": "did:key:z6MkIssuer",
            "content_hash": self.digest,
        }

    def tearDown(self):
        self.con.close()

    def test_exact_snapshot_roundtrip(self):
        stored = store_exact_job_snapshot(self.con, self.candidate, self.job)
        self.assertEqual(stored["state"], "SNAPSHOT_STORED")
        loaded = load_exact_job_snapshot(self.con, self.candidate)
        self.assertEqual(loaded["state"], "EXACT")
        self.assertEqual(loaded["job"], self.job)
        self.assertEqual(loaded["source"], "local-immutable-claim-snapshot")

    def test_exact_snapshot_tamper_fails_closed(self):
        store_exact_job_snapshot(self.con, self.candidate, self.job)
        self.con.execute(
            "UPDATE job_exact_snapshots SET job_json='{}' WHERE job_id=?",
            (self.job["job_id"],),
        )
        self.con.commit()
        loaded = load_exact_job_snapshot(self.con, self.candidate)
        self.assertEqual(loaded["state"], "SNAPSHOT_MISMATCH")

    def _signed_trial_and_attempt(self, con):
        seed = bytes(range(32))
        did = _did_from_seed(seed)
        identity = SigningIdentity(seed=seed, did=did)
        text = sweep_text(f"CLAIM v1 | {self.job['job_id']} | worker")
        nonce = "123456789"
        signature = identity.sign("kibble", int(nonce), text)
        text_hash = hashlib.sha256(
            ("technoscout-job-claim-v1\0" + text).encode("utf-8")
        ).hexdigest()
        trial = {
            "room": "kibble",
            "job_id": self.job["job_id"],
            "content_hash": self.digest,
            "job_seq": 100,
            "issuer_did": "did:key:z6MkIssuer",
            "job_type": "explain",
            "sender_did": did,
            "claim_text_hash": text_hash,
            "status": "SENT",
            "sent_seq": 150,
        }
        con.execute(
            """
            INSERT INTO job_claim_attempts(
              attempted_at,room,job_id,content_hash,did,nonce,sig,text,status,http_status,detail
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                1.0,
                "kibble",
                self.job["job_id"],
                self.digest,
                did,
                nonce,
                signature,
                text,
                "sent",
                200,
                "seq=150",
            ),
        )
        con.commit()
        return trial

    def test_signed_http200_claim_receipt_is_verified(self):
        trial = self._signed_trial_and_attempt(self.con)
        result = verify_local_claim_receipt(self.con, trial)
        self.assertEqual(result["state"], "CLAIM_CONFIRMED")
        self.assertEqual(result["source"], "local-signed-http200-receipt")

    def test_tampered_claim_signature_is_rejected(self):
        trial = self._signed_trial_and_attempt(self.con)
        self.con.execute(
            "UPDATE job_claim_attempts SET sig=? WHERE job_id=?",
            ("A" * 86, self.job["job_id"]),
        )
        self.con.commit()
        result = verify_local_claim_receipt(self.con, trial)
        self.assertEqual(result["state"], "CLAIM_LOCAL_RECEIPT_INVALID")

    def test_verify_claim_retained_falls_back_to_local_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "technoscout.db"
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            ensure_claim_schema(con)
            trial = self._signed_trial_and_attempt(con)
            con.close()
            with patch("job_execution_draft.retained_export_messages", return_value=[]):
                result = verify_claim_retained({"database": str(db_path)}, trial)
        self.assertEqual(result["state"], "CLAIM_CONFIRMED")
        self.assertEqual(result["source"], "local-signed-http200-receipt")

    def test_postclaim_pipeline_uses_local_snapshot_before_remote_fetch(self):
        store_exact_job_snapshot(self.con, self.candidate, self.job)
        claim = {
            "room": "kibble",
            "job_id": self.job["job_id"],
            "content_hash": self.digest,
            "job_seq": 100,
            "issuer_did": "did:key:z6MkIssuer",
            "job_type": "explain",
            "sender_did": "did:key:z6MkWorker",
            "status": "SENT",
            "sent_seq": 150,
            "claim_text_hash": "",
            "refined_relevance": 90,
            "refined_fit": 90,
            "refined_confidence": 90,
        }

        def loader(con, job_id, room="kibble"):
            return claim, "eligible"

        def draft(*args, **kwargs):
            exact = kwargs["exact_fetcher"]({}, {})
            self.assertEqual(exact["source"], "local-immutable-claim-snapshot")
            return {"state": "DRAFTED", "answer": "draft", "confidence": 90}

        def review(*args, **kwargs):
            return {"state": "REVIEWED", "answer": "review", "decision": "APPROVED", "confidence": 90}

        def quality(*args, **kwargs):
            return {
                "state": "QUALITY_REVIEWED",
                "answer": "final answer",
                "decision": "PASS",
                "confidence": 90,
                "critique": "",
                "model": "fake",
            }

        def success(*args, **kwargs):
            return {"state": "NOT_APPLICABLE", "answer": "final answer"}

        def prepare(*args, **kwargs):
            return {"state": "PREPARED", "text": "DELIVER", "ttl_seconds": 600}

        result = run_postclaim_pipeline(
            self.con,
            {"research_model": "fake"},
            self.job["job_id"],
            llm=object(),
            model="fake",
            claim_loader=loader,
            claim_verifier=lambda cfg, trial: {
                "state": "CLAIM_CONFIRMED",
                "seq": 150,
                "source": "test-proof",
            },
            draft_runner=draft,
            review_runner=review,
            quality_runner=quality,
            success_runner=success,
            prepare_runner=prepare,
        )
        self.assertEqual(result["state"], "READY_FOR_HUMAN_DELIVERY")
        self.assertEqual(result["exact_job_source"], "local-immutable-claim-snapshot")


if __name__ == "__main__":
    unittest.main()
