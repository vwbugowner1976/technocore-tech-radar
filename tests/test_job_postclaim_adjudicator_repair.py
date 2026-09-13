import unittest

from job_postclaim_pipeline import run_postclaim_pipeline


class JobPostClaimAdjudicatorRepairTests(unittest.TestCase):
    def setUp(self):
        self.con = object()
        self.job_id = "kea74499c4b"
        self.claim = {
            "room": "kibble",
            "job_id": self.job_id,
            "job_seq": 100,
            "issuer_did": "did:key:z6MkIssuer",
            "content_hash": "gpu-digest",
            "sent_seq": 150,
            "sender_did": "did:key:z6MkWorker",
        }
        self.exact = {
            "state": "EXACT",
            "job": {
                "verb": "JOB",
                "job_id": self.job_id,
                "job_type": "explain",
                "title": "How a GPU shared by training and inference fails first under load",
                "body": (
                    "Memory fragments and the smaller job is the one that dies. "
                    "Success: names one concrete failure mode and one leading indicator."
                ),
            },
        }

    def loader(self, con, job_id, room="kibble"):
        return dict(self.claim), "eligible"

    @staticmethod
    def verifier(cfg, claim):
        return {"state": "CLAIM_CONFIRMED", "seq": 150}

    @staticmethod
    def draft(*args, **kwargs):
        return {"state": "DRAFTED", "answer": "draft", "confidence": 80}

    @staticmethod
    def review(*args, **kwargs):
        return {"state": "REVIEWED", "answer": "review", "decision": "APPROVED", "confidence": 90}

    def test_adjudicator_block_gets_one_repair_then_success_gate(self):
        calls = {"repair": 0, "success": 0, "prepare": 0}
        repaired = (
            "Failure mode: GPU allocator fragmentation causes OOM for the smaller job. "
            "Leading indicator: allocator retries or failed large allocations rise first."
        )

        def quality(*args, **kwargs):
            return {
                "state": "BLOCKED",
                "reason": (
                    "The candidate answer does not provide a concrete failure mode and "
                    "leading indicator as requested."
                ),
                "repair_attempted": True,
                "adjudication_attempted": True,
            }

        def targeted_repair(con, cfg, job_id, **kwargs):
            calls["repair"] += 1
            self.assertEqual(kwargs["content_hash"], "gpu-digest")
            self.assertIn("leading indicator", kwargs["defect"].lower())
            return {
                "state": "QUALITY_REVIEWED",
                "answer": repaired,
                "decision": "REVISED",
                "confidence": 94,
                "critique": "added explicit leading indicator",
                "model": "fake-model",
            }

        def success(cfg, llm, model, job, answer):
            calls["success"] += 1
            self.assertEqual(answer, repaired)
            return {"state": "NOT_APPLICABLE", "answer": answer}

        def prepare(*args, **kwargs):
            calls["prepare"] += 1
            return {"state": "PREPARED", "text": "DELIVER v1 | x", "ttl_seconds": 600}

        result = run_postclaim_pipeline(
            self.con,
            {},
            self.job_id,
            llm=object(),
            model="fake-model",
            claim_loader=self.loader,
            claim_verifier=self.verifier,
            exact_fetcher=lambda cfg, candidate: self.exact,
            draft_runner=self.draft,
            review_runner=self.review,
            quality_runner=quality,
            quality_block_repair_runner=targeted_repair,
            success_runner=success,
            prepare_runner=prepare,
        )
        self.assertEqual(result["state"], "READY_FOR_HUMAN_DELIVERY")
        self.assertEqual(result["quality"]["answer"], repaired)
        self.assertIsNotNone(result["quality_block_repair"])
        self.assertEqual(calls, {"repair": 1, "success": 1, "prepare": 1})

    def test_plain_quality_block_does_not_use_targeted_repair(self):
        calls = {"repair": 0}

        def quality(*args, **kwargs):
            return {"state": "BLOCKED", "reason": "ordinary quality failure"}

        def targeted_repair(*args, **kwargs):
            calls["repair"] += 1
            return {"state": "QUALITY_REVIEWED"}

        result = run_postclaim_pipeline(
            self.con,
            {},
            self.job_id,
            claim_loader=self.loader,
            claim_verifier=self.verifier,
            exact_fetcher=lambda cfg, candidate: self.exact,
            draft_runner=self.draft,
            review_runner=self.review,
            quality_runner=quality,
            quality_block_repair_runner=targeted_repair,
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(result["stage"], "quality")
        self.assertEqual(calls["repair"], 0)


if __name__ == "__main__":
    unittest.main()
