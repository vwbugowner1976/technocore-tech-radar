import sqlite3
import unittest
from datetime import datetime, timezone

from job_claim_trial import ensure_claim_schema
from job_execution_quality_gate import ensure_quality_schema
from job_execution_review import ensure_review_schema
from job_gpu_semantic_repair import repair_gpu_shared_or_known
from job_postclaim_pipeline import run_postclaim_pipeline
from job_success_named_proof import validate_success_criterion as validate_success_named


JOB_ID = "kea74499c4b"
CONTENT_HASH = "fixture-gpu-shared"
ORIGINAL_ANSWER = (
    "Memory fragmentation is the first thing to break in a GPU shared by training and inference "
    "when demand climbs past its initial sizing. The failure mode is typically a runtime error "
    "indicating out-of-memory (OOM), and the leading indicator is a decrease in GPU memory "
    "availability or a warning message about memory fragmentation."
)
JOB = {
    "verb": "JOB",
    "job_id": JOB_ID,
    "job_type": "explain",
    "title": "How a GPU shared by training and inference fails first under load",
    "body": (
        "Explain the first thing to break in a GPU shared by training and inference when demand "
        "climbs past what it was sized for. Memory fragments and the smaller job is the one that "
        "dies. Name the failure mode and the signal that shows up before it. Success: names one "
        "concrete failure mode and one leading indicator."
    ),
}


class FixtureSuccessCaller:
    """Deterministic local verifier fixture; performs no network or side effect."""

    def __init__(self):
        self.modes = []

    def __call__(
        self,
        cfg,
        llm,
        model,
        prompt,
        payload,
        *,
        max_tokens,
        timeout_seconds,
    ):
        mode = str(payload.get("mode", ""))
        self.modes.append(mode)

        if mode == "local-grounding-selection-only":
            # C2 is the immutable JOB fact: memory fragments and the smaller job dies.
            return {"grounding_ids": ["C2"]}

        if mode == "local-success-repair-only":
            # Deliberately reproduce the old failure: generic repair keeps the answer
            # but still does not explicitly preserve the smaller-job grounding.
            return {
                "confidence": 90,
                "critique": "kept generic OOM explanation but missed exact smaller-job grounding",
                "answer": ORIGINAL_ANSWER,
            }

        if mode in {
            "local-success-verification-only",
            "local-success-final-verification-only",
        }:
            answer = str(payload.get("candidate_answer", ""))
            repaired_gpu = (
                "smaller inference job is the one that dies first" in answer
                and "leading indicator" in answer.lower()
            )

            if repaired_gpu:
                return {
                    "decision": "PASS",
                    "confidence": 100,
                    "checks": [
                        {"id": "R1", "satisfied": True, "evidence_ids": ["A1"]},
                        {"id": "R2", "satisfied": True, "evidence_ids": ["A2"]},
                    ],
                    "grounding_checks": [
                        {"id": "G1", "satisfied": True, "evidence_ids": ["A1"]},
                    ],
                    "critique": "all frozen requirements and grounding are explicit",
                    "answer": answer,
                }

            # Intentionally omit R2 evidence to exercise the literal named-item
            # supplement. G1 remains unsatisfied so the narrow semantic fallback
            # must ultimately repair the answer.
            return {
                "decision": "PASS",
                "confidence": 98,
                "checks": [
                    {"id": "R1", "satisfied": True, "evidence_ids": ["A2"]},
                ],
                "grounding_checks": [
                    {"id": "G1", "satisfied": False, "evidence_ids": []},
                ],
                "critique": "leading-indicator proof omitted and smaller-job grounding missing",
                "answer": answer,
            }

        raise AssertionError(f"unexpected Success caller mode: {mode}")


class SharedGpuNoSendE2ETests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_claim_schema(self.con)
        ensure_review_schema(self.con)
        ensure_quality_schema(self.con)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

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
                "kibble", JOB_ID, CONTENT_HASH, 100, "did:key:z6MkIssuer", "explain",
                now, 80, 90, 95, 1.0, 2.0, 1.1, 2.1, 1.2,
                "did:key:z6MkWorker", "fixture-claim-hash", "SENT", 150, "",
            ),
        )
        self.con.execute(
            """
            INSERT INTO job_execution_reviews(
              room,job_id,content_hash,reviewed_at,model,decision,confidence,
              critique,answer_hash,answer_text,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'REVIEWED')
            """,
            (
                "kibble", JOB_ID, CONTENT_HASH, now, "fixture-model", "APPROVED", 100,
                "", "fixture-answer-hash", ORIGINAL_ANSWER,
            ),
        )
        self.con.commit()

    def tearDown(self):
        self.con.close()

    @staticmethod
    def claim_loader(con, job_id, room="kibble"):
        return {
            "room": room,
            "job_id": job_id,
            "job_seq": 100,
            "issuer_did": "did:key:z6MkIssuer",
            "content_hash": CONTENT_HASH,
            "sent_seq": 150,
            "sender_did": "did:key:z6MkWorker",
        }, "eligible"

    @staticmethod
    def claim_verifier(cfg, claim):
        return {"state": "CLAIM_CONFIRMED", "seq": 150, "source": "fixture"}

    @staticmethod
    def exact_fetcher(cfg, candidate):
        return {"state": "EXACT", "source": "fixture", "job": dict(JOB)}

    @staticmethod
    def draft_runner(*args, **kwargs):
        return {"state": "DRAFTED", "answer": ORIGINAL_ANSWER, "confidence": 90}

    @staticmethod
    def review_runner(*args, **kwargs):
        return {
            "state": "REVIEWED",
            "answer": ORIGINAL_ANSWER,
            "decision": "APPROVED",
            "confidence": 100,
        }

    @staticmethod
    def quality_runner(*args, **kwargs):
        return {
            "state": "QUALITY_REVIEWED",
            "answer": ORIGINAL_ANSWER,
            "decision": "PASS",
            "confidence": 100,
            "critique": "",
            "model": "fixture-model",
        }

    @staticmethod
    def success_persister(con, **kwargs):
        result = kwargs["result"]
        return {
            "state": "SUCCESS_REVIEWED",
            "decision": result.get("decision", "PASS"),
            "confidence": result.get("confidence", 100),
            "answer": result["answer"],
            "answer_hash": "fixture-success-hash",
        }

    @staticmethod
    def prepare_runner(con, cfg, job_id, **kwargs):
        return {
            "state": "PREPARED",
            "text": f"DELIVER v1 | {job_id} | fixture-only",
            "ttl_seconds": 600,
        }

    def test_shared_gpu_fixture_reaches_ready_without_any_signed_write(self):
        caller = FixtureSuccessCaller()

        def success_runner(cfg, llm, model, job, answer):
            return validate_success_named(
                cfg,
                llm,
                model,
                job,
                answer,
                caller=caller,
            )

        result = run_postclaim_pipeline(
            self.con,
            {"job_success_repair_attempts": 0},
            JOB_ID,
            llm=object(),
            model="fixture-model",
            claim_loader=self.claim_loader,
            claim_verifier=self.claim_verifier,
            exact_fetcher=self.exact_fetcher,
            draft_runner=self.draft_runner,
            review_runner=self.review_runner,
            quality_runner=self.quality_runner,
            success_runner=success_runner,
            semantic_repair_runner=repair_gpu_shared_or_known,
            success_persister=self.success_persister,
            prepare_runner=self.prepare_runner,
        )

        self.assertEqual(result["state"], "READY_FOR_HUMAN_DELIVERY")
        self.assertEqual(result["semantic_fallback"]["state"], "QUALITY_REVIEWED")
        self.assertEqual(result["success"]["state"], "SUCCESS_REVIEWED")
        final_answer = result["quality"]["answer"]
        self.assertIn("smaller inference job is the one that dies first", final_answer)
        self.assertIn("leading indicator", final_answer.lower())
        self.assertIn("local-success-repair-only", caller.modes)
        self.assertGreaterEqual(caller.modes.count("local-success-verification-only"), 2)


if __name__ == "__main__":
    unittest.main()
