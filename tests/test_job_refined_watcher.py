import sqlite3
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from job_candidate_refiner import ensure_refiner_schema
from job_refined_watcher import (
    autonomy_for_shadow,
    pending_candidate_rows,
    run_once,
    shadow_canary_for_summary,
)
from job_shadow import ensure_job_shadow_schema, record_job_shadow_candidate


class FakeLLM:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class JobRefinedWatcherTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_job_shadow_schema(self.con)
        ensure_refiner_schema(self.con)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.issuer = "did:key:z6Mkstrong"

        for index, lifecycle in enumerate(("ATTESTED", "DELIVERED", "ATTESTED"), start=1):
            record_job_shadow_candidate(
                self.con,
                seen_at=now,
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
            seen_at=now,
            room="kibble",
            job_id="kabcdef0123",
            job_seq=100,
            issuer_did=self.issuer,
            signed_identity=True,
            job_type="coordinate",
            digest="candidate-digest",
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
        self.con.commit()
        self.cfg = {
            "research_model": "fake-model",
            "job_gate_min_observed_jobs": 1,
            "job_gate_min_open_jobs": 1,
            "job_gate_min_closed_jobs": 1,
            "job_gate_min_signed_issuers": 1,
            "job_gate_min_issuer_jobs": 3,
            "job_gate_min_issuer_closed": 2,
            "job_gate_min_issuer_completed": 2,
            "job_gate_min_issuer_attested": 1,
            "job_gate_min_issuer_completion_rate_percent": 50,
        }

    def tearDown(self):
        self.con.close()

    def test_pending_rows_exclude_already_refined_content(self):
        rows = pending_candidate_rows(self.con, self.cfg, limit=1, max_age_seconds=900)
        self.assertEqual([row["job_id"] for row in rows], ["kabcdef0123"])
        self.con.execute(
            """
            INSERT INTO job_candidate_refinements(
              room,job_id,content_hash,refined_at,decision,relevance,
              technical_fit,confidence,effort,reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "kibble", "kabcdef0123", "candidate-digest", datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "NOT_RELEVANT", 10, 20, 90, "small", "done",
            ),
        )
        self.con.commit()
        self.assertEqual(pending_candidate_rows(self.con, self.cfg, limit=1, max_age_seconds=900), [])

    def test_terminal_live_failure_does_not_load_llm(self):
        calls = {"llm": 0}

        def llm_factory(cfg):
            calls["llm"] += 1
            return FakeLLM()

        def revalidator(cfg, candidate):
            return {"state": "JOB_NOT_RETAINED", "lifecycle": "OPEN", "pages": 0, "messages": 10}

        summary = run_once(
            self.con,
            self.cfg,
            revalidator=revalidator,
            llm_factory=llm_factory,
        )
        self.assertEqual(calls["llm"], 0)
        self.assertEqual(summary["terminal_skips"], 1)
        row = self.con.execute(
            "SELECT decision FROM job_candidate_refinements WHERE job_id=?",
            ("kabcdef0123",),
        ).fetchone()
        self.assertEqual(row["decision"], "INCONCLUSIVE")

    def test_open_candidate_refines_and_can_raise_ready_evidence(self):
        llm = FakeLLM()

        def revalidator(cfg, candidate):
            return {"state": "OPEN_CONFIRMED", "lifecycle": "OPEN", "pages": 1, "messages": 10}

        def refiner(cfg, candidate, llm_obj, model):
            return {
                "decision": "SAFE_FIT",
                "relevance": 84,
                "technical_fit": 91,
                "confidence": 96,
                "effort": "small",
                "reason": "semantic match",
            }

        def gate_evaluator(con, cfg, **kwargs):
            return SimpleNamespace(
                ready_for_manual_claim_trial=True,
                candidates=({"job_id": "kabcdef0123"},),
            )

        summary = run_once(
            self.con,
            self.cfg,
            revalidator=revalidator,
            refiner=refiner,
            llm_factory=lambda cfg: llm,
            gate_evaluator=gate_evaluator,
        )
        self.assertEqual(summary["refined"], 1)
        self.assertEqual(summary["safe_fit"], 1)
        self.assertTrue(summary["ready"])
        self.assertEqual(summary["ready_job_id"], "kabcdef0123")
        self.assertTrue(llm.closed)

    def test_refiner_timeout_is_retry_later_and_closes_llm(self):
        llm = FakeLLM()

        def revalidator(cfg, candidate):
            return {"state": "OPEN_CONFIRMED", "lifecycle": "OPEN", "pages": 1, "messages": 10}

        def refiner(cfg, candidate, llm_obj, model):
            raise TimeoutError("managed MLX process lock timed out")

        summary = run_once(
            self.con,
            self.cfg,
            revalidator=revalidator,
            refiner=refiner,
            llm_factory=lambda cfg: llm,
        )
        self.assertEqual(summary["refined"], 0)
        self.assertEqual(summary["retry_later"], 1)
        self.assertEqual(summary["rows"][0]["state"], "RETRY_LATER")
        self.assertEqual(summary["rows"][0]["detail"], "REFINER_ERROR:TimeoutError")
        self.assertTrue(llm.closed)
        row = self.con.execute(
            "SELECT 1 FROM job_candidate_refinements WHERE job_id=?",
            ("kabcdef0123",),
        ).fetchone()
        self.assertIsNone(row)

    def test_shadow_canary_hook_runs_only_for_ready_summary(self):
        calls = []

        def observer(con, cfg, job_id, room):
            calls.append((job_id, room))
            return {"state": "SHADOW_ELIGIBLE", "job_id": job_id, "reason": "fixture"}

        result = shadow_canary_for_summary(
            self.con,
            self.cfg,
            {"ready": True, "ready_job_id": "kabcdef0123"},
            observer=observer,
        )
        self.assertEqual(result["state"], "SHADOW_ELIGIBLE")
        self.assertEqual(calls, [("kabcdef0123", "kibble")])

        skipped = shadow_canary_for_summary(
            self.con,
            self.cfg,
            {"ready": False, "ready_job_id": ""},
            observer=observer,
        )
        self.assertIsNone(skipped)
        self.assertEqual(len(calls), 1)

    def test_autonomy_hook_runs_only_for_shadow_eligible(self):
        calls = []

        def reviewer(con, cfg, job_id, room):
            calls.append((job_id, room))
            return {
                "state": "AUTO_SAFE",
                "confidence": 96,
                "reason": "fixture",
                "job_id": job_id,
            }

        result = autonomy_for_shadow(
            self.con,
            self.cfg,
            {"state": "SHADOW_ELIGIBLE", "job_id": "kabcdef0123"},
            reviewer=reviewer,
        )
        self.assertEqual(result["state"], "AUTO_SAFE")
        self.assertEqual(calls, [("kabcdef0123", "kibble")])

        skipped = autonomy_for_shadow(
            self.con,
            self.cfg,
            {"state": "SHADOW_SKIP", "job_id": "kabcdef0123"},
            reviewer=reviewer,
        )
        self.assertIsNone(skipped)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
