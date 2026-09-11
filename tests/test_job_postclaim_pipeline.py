import unittest

from job_postclaim_pipeline import run_postclaim_pipeline


class JobPostClaimPipelineTests(unittest.TestCase):
    def setUp(self):
        self.con = object()
        self.cfg = {}
        self.job_id = "kabcdef0123"
        self.claim = {
            "room": "kibble",
            "job_id": self.job_id,
            "job_seq": 100,
            "issuer_did": "did:key:z6MkIssuer",
            "content_hash": "digest",
            "sent_seq": 150,
            "sender_did": "did:key:z6MkWorker",
        }
        self.exact = {
            "state": "EXACT",
            "job": {
                "verb": "JOB",
                "job_id": self.job_id,
                "job_type": "coordinate",
                "title": "title",
                "body": "body",
            },
        }

    def loader(self, con, job_id, room="kibble"):
        self.assertIs(con, self.con)
        self.assertEqual(job_id, self.job_id)
        return dict(self.claim), "eligible"

    def test_success_reuses_one_snapshot_and_ends_prepared_only(self):
        counts = {"verify": 0, "exact": 0, "draft": 0, "review": 0, "quality": 0, "prepare": 0}
        llm = object()

        def verifier(cfg, claim):
            counts["verify"] += 1
            return {"state": "CLAIM_CONFIRMED", "seq": 150}

        def exact_fetcher(cfg, candidate):
            counts["exact"] += 1
            self.assertEqual(candidate["content_hash"], "digest")
            return self.exact

        def draft(con, cfg, job_id, **kwargs):
            counts["draft"] += 1
            self.assertIs(kwargs["llm"], llm)
            self.assertEqual(kwargs["exact_fetcher"]({}, {})["state"], "EXACT")
            self.assertEqual(kwargs["claim_verifier"]({}, {})["state"], "CLAIM_CONFIRMED")
            return {"state": "DRAFTED", "answer": "draft", "confidence": 80}

        def review(con, cfg, job_id, **kwargs):
            counts["review"] += 1
            self.assertIs(kwargs["llm"], llm)
            self.assertEqual(kwargs["exact_fetcher"]({}, {})["state"], "EXACT")
            self.assertEqual(kwargs["claim_verifier"]({}, {})["state"], "CLAIM_CONFIRMED")
            return {"state": "REVIEWED", "answer": "review", "decision": "REVISED", "confidence": 90}

        def quality(con, cfg, job_id, **kwargs):
            counts["quality"] += 1
            self.assertIs(kwargs["llm"], llm)
            self.assertEqual(kwargs["exact_fetcher"]({}, {})["state"], "EXACT")
            self.assertEqual(kwargs["claim_verifier"]({}, {})["state"], "CLAIM_CONFIRMED")
            return {"state": "QUALITY_REVIEWED", "answer": "final", "decision": "PASS", "confidence": 95, "critique": ""}

        def prepare(con, cfg, job_id, **kwargs):
            counts["prepare"] += 1
            self.assertEqual(kwargs["exact_fetcher"]({}, {})["state"], "EXACT")
            return {"state": "PREPARED", "text": f"DELIVER v1 | {job_id} | final", "ttl_seconds": 600}

        result = run_postclaim_pipeline(
            self.con, self.cfg, self.job_id,
            llm=llm, model="fake-model",
            claim_loader=self.loader,
            claim_verifier=verifier,
            exact_fetcher=exact_fetcher,
            draft_runner=draft,
            review_runner=review,
            quality_runner=quality,
            prepare_runner=prepare,
        )
        self.assertEqual(result["state"], "READY_FOR_HUMAN_DELIVERY")
        self.assertEqual(result["claim_seq"], 150)
        self.assertEqual(counts, {"verify": 1, "exact": 1, "draft": 1, "review": 1, "quality": 1, "prepare": 1})

    def test_missing_claim_stops_before_any_stage(self):
        calls = {"draft": 0}

        def no_claim(con, job_id, room="kibble"):
            return None, "no claim trial exists"

        def draft(*args, **kwargs):
            calls["draft"] += 1
            return {"state": "DRAFTED"}

        result = run_postclaim_pipeline(
            self.con, self.cfg, self.job_id,
            claim_loader=no_claim,
            draft_runner=draft,
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(result["stage"], "claim")
        self.assertEqual(calls["draft"], 0)

    def test_exact_job_must_exist_at_pipeline_start(self):
        def verifier(cfg, claim):
            return {"state": "CLAIM_CONFIRMED", "seq": 150}

        result = run_postclaim_pipeline(
            self.con, self.cfg, self.job_id,
            claim_loader=self.loader,
            claim_verifier=verifier,
            exact_fetcher=lambda cfg, candidate: {"state": "NOT_RETAINED"},
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(result["stage"], "exact-job")
        self.assertIn("NOT_RETAINED", result["reason"])

    def test_quality_block_prevents_delivery_prepare(self):
        prepared = {"calls": 0}

        def verifier(cfg, claim):
            return {"state": "CLAIM_CONFIRMED", "seq": 150}

        def draft(*args, **kwargs):
            return {"state": "DRAFTED", "answer": "draft", "confidence": 80}

        def review(*args, **kwargs):
            return {"state": "REVIEWED", "answer": "review", "decision": "APPROVED", "confidence": 90}

        def quality(*args, **kwargs):
            return {"state": "BLOCKED", "reason": "counterexample still breaks answer"}

        def prepare(*args, **kwargs):
            prepared["calls"] += 1
            return {"state": "PREPARED"}

        result = run_postclaim_pipeline(
            self.con, self.cfg, self.job_id,
            claim_loader=self.loader,
            claim_verifier=verifier,
            exact_fetcher=lambda cfg, candidate: self.exact,
            draft_runner=draft,
            review_runner=review,
            quality_runner=quality,
            prepare_runner=prepare,
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(result["stage"], "quality")
        self.assertEqual(prepared["calls"], 0)


if __name__ == "__main__":
    unittest.main()
