import sqlite3
import unittest

from job_progress_gate import evaluate_gate, live_revalidate_job
from job_shadow import content_hash, ensure_job_shadow_schema, parse_kibble_message


BASE_CFG = {
    "job_gate_min_observed_jobs": 4,
    "job_gate_min_open_jobs": 1,
    "job_gate_min_closed_jobs": 2,
    "job_gate_min_signed_issuers": 1,
    "job_gate_min_candidate_relevance": 50,
    "job_gate_min_candidate_fit": 60,
    "job_gate_min_candidate_confidence": 75,
    "job_gate_min_issuer_jobs": 3,
    "job_gate_min_issuer_closed": 2,
    "job_gate_min_issuer_completed": 2,
    "job_gate_min_issuer_attested": 1,
    "job_gate_min_issuer_completion_rate_percent": 50,
    "job_gate_live_page_limit": 20,
    "job_gate_live_max_pages": 3,
}


class JobProgressGateTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_job_shadow_schema(self.con)
        self.issuer = "did:key:z6MkIssuerA"

    def tearDown(self):
        self.con.close()

    def add_job(
        self,
        job_id,
        lifecycle,
        seq,
        *,
        fit_class="FIT",
        relevance=80,
        technical_fit=80,
        confidence=90,
        issuer=None,
        body="body",
        title="title",
        job_type="explain",
    ):
        issuer = issuer or self.issuer
        parsed = {
            "job_id": job_id,
            "job_type": job_type,
            "title": title,
            "body": body,
        }
        digest = content_hash(parsed)
        self.con.execute(
            """
            INSERT INTO job_shadow_candidates(
              room,job_id,first_seen_at,last_seen_at,job_seq,issuer_did,
              signed_identity,job_type,content_hash,lifecycle,fit_class,
              relevance,technical_fit,confidence,effort
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "kibble", job_id, "2026-09-11T00:00:00+00:00",
                "2026-09-11T00:00:00+00:00", seq, issuer, 1,
                job_type, digest, lifecycle, fit_class,
                relevance, technical_fit, confidence, "small",
            ),
        )
        self.con.commit()
        return parsed

    def populate_reputable_candidate(self):
        self.add_job("k0000000002", "ATTESTED", 2)
        self.add_job("k0000000003", "DELIVERED", 3)
        self.add_job("k0000000004", "CLAIMED", 4, fit_class="NOT_RELEVANT")
        return self.add_job("k0000000001", "OPEN", 10)

    def test_collecting_when_baseline_is_small(self):
        result = evaluate_gate(self.con, BASE_CFG)
        self.assertEqual(result.state, "COLLECTING")
        self.assertFalse(result.ready_for_manual_claim_trial)

    def test_waiting_when_no_safe_fit_candidate(self):
        self.add_job("k0000000002", "ATTESTED", 2, fit_class="NOT_RELEVANT")
        self.add_job("k0000000003", "DELIVERED", 3, fit_class="NOT_RELEVANT")
        self.add_job("k0000000004", "CLAIMED", 4, fit_class="NOT_RELEVANT")
        self.add_job("k0000000001", "OPEN", 10, fit_class="NEEDS_TOOL")
        result = evaluate_gate(self.con, BASE_CFG)
        self.assertEqual(result.state, "WAITING_FOR_SAFE_CANDIDATE")
        self.assertFalse(result.ready_for_manual_claim_trial)

    def test_local_candidate_requires_live_revalidation(self):
        self.populate_reputable_candidate()
        result = evaluate_gate(self.con, BASE_CFG, live=False)
        self.assertEqual(result.state, "NEEDS_LIVE_REVALIDATION")
        self.assertFalse(result.ready_for_manual_claim_trial)
        self.assertEqual(result.candidates[0]["job_id"], "k0000000001")

    def test_live_open_candidate_becomes_ready_for_manual_trial(self):
        parsed = self.populate_reputable_candidate()
        text = "JOB v1 | k0000000001 | explain | title | body"

        def fetcher(cfg, path, query):
            return {
                "messages": [
                    {"seq": 10, "from": self.issuer, "text": text},
                    {"seq": 11, "from": "did:key:z6MkOther", "text": "hello"},
                ]
            }

        self.assertEqual(
            content_hash(parse_kibble_message(text)),
            content_hash(parsed),
        )
        result = evaluate_gate(self.con, BASE_CFG, live=True, fetcher=fetcher)
        self.assertEqual(result.state, "READY_FOR_MANUAL_CLAIM_TRIAL")
        self.assertTrue(result.ready_for_manual_claim_trial)
        self.assertEqual(result.candidates[-1]["live"]["state"], "OPEN_CONFIRMED")

    def test_live_claimed_candidate_fails_closed(self):
        self.populate_reputable_candidate()
        text = "JOB v1 | k0000000001 | explain | title | body"

        def fetcher(cfg, path, query):
            return {
                "messages": [
                    {"seq": 10, "from": self.issuer, "text": text},
                    {"seq": 12, "from": "did:key:z6MkWorker", "text": "CLAIM v1 | k0000000001 | worker"},
                ]
            }

        result = evaluate_gate(self.con, BASE_CFG, live=True, fetcher=fetcher)
        self.assertEqual(result.state, "NO_LIVE_OPEN_CANDIDATE")
        self.assertFalse(result.ready_for_manual_claim_trial)
        self.assertEqual(result.candidates[-1]["live"]["lifecycle"], "CLAIMED")

    def test_live_mismatch_never_becomes_ready(self):
        parsed = self.populate_reputable_candidate()
        candidate = {
            "room": "kibble",
            "job_id": "k0000000001",
            "job_seq": 10,
            "issuer_did": self.issuer,
            "content_hash": content_hash(parsed),
        }

        def fetcher(cfg, path, query):
            return {
                "messages": [
                    {
                        "seq": 10,
                        "from": self.issuer,
                        "text": "JOB v1 | k0000000001 | explain | changed | body",
                    }
                ]
            }

        check = live_revalidate_job(BASE_CFG, candidate, fetcher=fetcher)
        self.assertEqual(check["state"], "JOB_MISMATCH")


if __name__ == "__main__":
    unittest.main()
