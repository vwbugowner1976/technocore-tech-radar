import unittest

from job_postclaim_pipeline import run_postclaim_pipeline


class GroundingOnlySuccessRepairTests(unittest.TestCase):
    def setUp(self):
        self.con = object()
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
                "title": "example",
                "body": (
                    "Observed concrete behavior. "
                    "Success: names one cause and one consequence."
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
        return {"state": "DRAFTED", "answer": "draft", "confidence": 85}

    @staticmethod
    def review(*args, **kwargs):
        return {
            "state": "REVIEWED",
            "answer": "review",
            "decision": "PASS",
            "confidence": 90,
        }

    @staticmethod
    def quality(*args, **kwargs):
        return {
            "state": "QUALITY_REVIEWED",
            "answer": "cause and consequence answer",
            "decision": "PASS",
            "confidence": 92,
            "critique": "",
            "model": "fake-model",
        }

    def test_grounding_only_block_gets_one_local_repair_then_same_gate_passes(self):
        calls = {"success": 0, "semantic": 0, "persist": 0, "prepare": 0}
        repaired = "Observed concrete behavior explains the cause; the consequence follows from that behavior."

        def success(cfg, llm, model, job, answer):
            calls["success"] += 1
            if calls["success"] == 1:
                self.assertEqual(int(cfg.get("job_success_repair_attempts", 0)), 0)
                return {
                    "state": "BLOCKED",
                    "reason": "structured exact-quote evidence does not satisfy frozen contract; requirements=[] grounding=['G1']",
                    "verdict": {
                        "missing_requirements": [],
                        "missing_grounding": ["G1"],
                    },
                }

            self.assertEqual(int(cfg.get("job_success_repair_attempts", 0)), 1)
            return {
                "state": "SUCCESS_REVIEWED",
                "decision": "REVISED",
                "confidence": 96,
                "success_clause": "names one cause and one consequence.",
                "contract": {
                    "requirements": [
                        {"id": "R1", "text": "names one cause"},
                        {"id": "R2", "text": "names one consequence"},
                    ],
                    "grounding": [
                        {"id": "G1", "fact": "Observed concrete behavior.", "required": True},
                    ],
                },
                "final_verdict": {"critique": "grounding is now explicitly linked"},
                "answer": repaired,
            }

        def semantic(*args, **kwargs):
            calls["semantic"] += 1
            return {"state": "BLOCKED", "reason": "must not be needed"}

        def persist(con, **kwargs):
            calls["persist"] += 1
            self.assertEqual(kwargs["result"]["answer"], repaired)
            return {
                "state": "SUCCESS_REVIEWED",
                "decision": "REVISED",
                "confidence": 92,
                "answer": repaired,
                "answer_hash": "hash",
            }

        def prepare(*args, **kwargs):
            calls["prepare"] += 1
            return {"state": "PREPARED", "text": "DELIVER", "ttl_seconds": 600}

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
            quality_runner=self.quality,
            success_runner=success,
            success_persister=persist,
            semantic_repair_runner=semantic,
            prepare_runner=prepare,
        )

        self.assertEqual(result["state"], "READY_FOR_HUMAN_DELIVERY")
        self.assertEqual(calls, {"success": 2, "semantic": 0, "persist": 1, "prepare": 1})
        self.assertIsNotNone(result["grounding_repair"])

    def test_missing_requirement_does_not_enable_generic_repair(self):
        calls = {"success": 0, "semantic": 0}

        def success(cfg, llm, model, job, answer):
            calls["success"] += 1
            self.assertEqual(int(cfg.get("job_success_repair_attempts", 0)), 0)
            return {
                "state": "BLOCKED",
                "reason": "R2 missing",
                "verdict": {
                    "missing_requirements": ["R2"],
                    "missing_grounding": ["G1"],
                },
            }

        def semantic(*args, **kwargs):
            calls["semantic"] += 1
            return {"state": "BLOCKED", "reason": "no supported deterministic semantic repair"}

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
            quality_runner=self.quality,
            success_runner=success,
            semantic_repair_runner=semantic,
            prepare_runner=lambda *a, **k: self.fail("prepare must not run"),
        )

        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(calls, {"success": 1, "semantic": 1})
        self.assertIsNone(result.get("grounding_repair"))


if __name__ == "__main__":
    unittest.main()
