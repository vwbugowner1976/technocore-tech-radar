import sqlite3
import unittest

from job_claim_trial import ensure_claim_schema
from job_execution_draft import ensure_exec_schema, generate_draft, verify_claim_retained
from job_shadow import content_hash, parse_kibble_message


class JobExecutionDraftTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_claim_schema(self.con)
        ensure_exec_schema(self.con)
        self.job_text = "JOB v1 | kabcdef0123 | explain | title | body"
        parsed = parse_kibble_message(self.job_text)
        self.digest = content_hash(parsed)
        self.trial = {
            "room": "kibble",
            "job_id": "kabcdef0123",
            "content_hash": self.digest,
            "job_seq": 100,
            "issuer_did": "did:key:z6MkIssuer",
            "job_type": "explain",
            "sender_did": "did:key:z6MkWorker",
            "status": "SENT",
            "sent_seq": 150,
            "refined_relevance": 80,
            "refined_fit": 90,
            "refined_confidence": 95,
        }
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
                "kibble","kabcdef0123",self.digest,100,"did:key:z6MkIssuer","explain",
                "2026-09-11T00:00:00+00:00",80,90,95,1,2,1,2,1,
                "did:key:z6MkWorker","h","SENT",150,"",
            ),
        )
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def test_verify_claim_retained_exact(self):
        def export_fetcher(cfg, room):
            return [
                {"seq": 150, "from": "did:key:z6MkWorker", "text": "CLAIM v1 | kabcdef0123 | worker"}
            ]
        result = verify_claim_retained({}, self.trial, export_fetcher=export_fetcher)
        self.assertEqual(result["state"], "CLAIM_CONFIRMED")

    def test_verify_claim_mismatch_fails_closed(self):
        def export_fetcher(cfg, room):
            return [
                {"seq": 150, "from": "did:key:z6MkOther", "text": "CLAIM v1 | kabcdef0123 | worker"}
            ]
        result = verify_claim_retained({}, self.trial, export_fetcher=export_fetcher)
        self.assertEqual(result["state"], "CLAIM_MISMATCH")

    def test_generate_draft_stores_local_answer(self):
        def claim_verifier(cfg, trial):
            return {"state": "CLAIM_CONFIRMED", "seq": 150}

        def exact_fetcher(cfg, candidate):
            return {
                "state": "EXACT",
                "job": {
                    "verb": "JOB",
                    "job_id": "kabcdef0123",
                    "job_type": "explain",
                    "title": "title",
                    "body": "body",
                },
            }

        def evaluator(cfg, llm, model, prompt, payload, max_tokens, timeout_seconds):
            self.assertIn("Do NOT block merely because the job is already claimed", prompt)
            self.assertTrue(payload["claim"]["claim_verified"])
            self.assertEqual(payload["claim"]["claim_owner"], "this_agent")
            self.assertEqual(payload["claim"]["claim_sender_did"], "did:key:z6MkWorker")
            return {
                "decision": "DRAFT",
                "confidence": 94,
                "answer": "Measure the peak buffered bytes per concurrent response and derive a safe concurrency budget.",
                "note": "",
            }

        result = generate_draft(
            self.con,
            {"research_model": "fake-model"},
            "kabcdef0123",
            llm=object(),
            model="fake-model",
            evaluator=evaluator,
            exact_fetcher=exact_fetcher,
            claim_verifier=claim_verifier,
        )
        self.assertEqual(result["state"], "DRAFTED")
        row = self.con.execute(
            "SELECT answer_text,status FROM job_execution_drafts WHERE job_id=?",
            ("kabcdef0123",),
        ).fetchone()
        self.assertEqual(row["status"], "DRAFT")
        self.assertIn("peak buffered bytes", row["answer_text"])

    def test_non_sent_claim_is_blocked_without_model(self):
        self.con.execute(
            "UPDATE job_claim_trials SET status='ARMED' WHERE job_id='kabcdef0123'"
        )
        self.con.commit()
        result = generate_draft(
            self.con,
            {"research_model": "fake-model"},
            "kabcdef0123",
            llm=object(),
            model="fake-model",
            evaluator=lambda *a, **k: self.fail("evaluator must not run"),
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("not SENT", result["reason"])


if __name__ == "__main__":
    unittest.main()