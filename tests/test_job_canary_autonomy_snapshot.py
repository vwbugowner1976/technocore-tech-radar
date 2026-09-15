import sqlite3
import unittest

from job_canary_auto import ensure_canary_schema
from job_canary_autonomy_gate import ensure_autonomy_schema, review_shadow_candidate
from job_canary_autonomy_snapshot import (
    ensure_autonomy_snapshot_schema,
    load_autonomy_snapshot,
    store_autonomy_snapshot,
)
from job_canary_shadow_review import review_shadow_eligible
from job_candidate_refiner import ensure_refiner_schema
from job_shadow import ensure_job_shadow_schema


class FakeLLM:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class CanaryAutonomySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_job_shadow_schema(self.con)
        ensure_refiner_schema(self.con)
        ensure_canary_schema(self.con)
        ensure_autonomy_schema(self.con)
        ensure_autonomy_snapshot_schema(self.con)
        self.candidate = {
            "room": "kibble",
            "job_id": "ksnapshot01",
            "job_seq": 100,
            "issuer_did": "did:key:z6Mkissuer",
            "job_type": "explain",
            "content_hash": "digest",
        }
        self.job = {
            "job_type": "explain",
            "title": "Self-contained fixture",
            "body": "Explain one stable concept. Success: names the concept.",
        }

    def tearDown(self):
        self.con.close()

    def test_snapshot_is_insert_once_and_conflict_fails_closed(self):
        first = store_autonomy_snapshot(self.con, self.candidate, self.job)
        self.assertEqual(first["state"], "SNAPSHOT_STORED")
        second = store_autonomy_snapshot(self.con, self.candidate, self.job)
        self.assertEqual(second["state"], "SNAPSHOT_VERIFIED")
        changed = dict(self.job)
        changed["body"] = "Different body"
        conflict = store_autonomy_snapshot(self.con, self.candidate, changed)
        self.assertEqual(conflict["state"], "SNAPSHOT_CONFLICT")
        loaded = load_autonomy_snapshot(self.con, self.candidate)
        self.assertEqual(loaded["state"], "EXACT")
        self.assertEqual(loaded["job"]["body"], self.job["body"])

    def test_autonomy_review_captures_snapshot_before_recording_auto_safe(self):
        llm = FakeLLM()
        result = review_shadow_candidate(
            self.con,
            {"research_model": "fake"},
            self.candidate["job_id"],
            candidate_loader=lambda con, cfg, job_id, room: (self.candidate, "eligible"),
            exact_fetcher=lambda cfg, candidate: {"state": "EXACT", "job": self.job},
            llm_factory=lambda cfg: llm,
            reviewer=lambda cfg, llm_obj, model, job: {
                "decision": "AUTO_SAFE",
                "confidence": 99,
                "reason": "self-contained fixture",
            },
        )
        self.assertEqual(result["state"], "AUTO_SAFE")
        self.assertTrue(llm.closed)
        loaded = load_autonomy_snapshot(self.con, self.candidate)
        self.assertEqual(loaded["state"], "EXACT")
        self.assertEqual(loaded["job"]["title"], self.job["title"])

    def test_auto_safe_audit_uses_snapshot_without_live_fetch(self):
        now = "2026-09-15T00:00:00+00:00"
        self.con.execute(
            """
            INSERT INTO job_shadow_candidates(
              room,job_id,first_seen_at,last_seen_at,job_seq,issuer_did,signed_identity,
              job_type,content_hash,lifecycle,fit_class,relevance,technical_fit,confidence,
              effort,required_capabilities_json,reason,summary,evaluation_count,last_evaluated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("kibble","ksnapshot01",now,now,100,"did:key:z6Mkissuer",1,
             "explain","digest","OPEN","NOT_RELEVANT",25,30,65,"small","[]","","",1,now),
        )
        self.con.execute(
            """
            INSERT INTO job_candidate_refinements(
              room,job_id,content_hash,refined_at,decision,relevance,technical_fit,confidence,effort,reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            ("kibble","ksnapshot01","digest",now,"SAFE_FIT",80,90,95,"small","semantic match"),
        )
        self.con.execute(
            """
            INSERT INTO job_canary_shadow_observations(
              room,job_id,content_hash,verdict,reason,job_type,relevance,technical_fit,
              confidence,issuer_score,attested_jobs,completion_rate_percent,live_state,observed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("kibble","ksnapshot01","digest","SHADOW_ELIGIBLE","accept","explain",
             80,90,95,99,10,90,"OPEN_CONFIRMED",now),
        )
        self.con.execute(
            """
            INSERT INTO job_canary_autonomy_reviews(
              room,job_id,content_hash,decision,confidence,reason,reviewed_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            ("kibble","ksnapshot01","digest","AUTO_SAFE",99,"safe",now),
        )
        self.con.commit()
        store_autonomy_snapshot(self.con, self.candidate, self.job)

        calls = {"live": 0}
        def live_fetch(cfg, candidate):
            calls["live"] += 1
            raise AssertionError("live fetch must not run when snapshot exists")

        rows = review_shadow_eligible(
            self.con,
            {},
            auto_safe_only=True,
            limit=1,
            exact_fetcher=live_fetch,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "EXACT")
        self.assertEqual(rows[0]["source"], "SNAPSHOT")
        self.assertEqual(rows[0]["job"]["body"], self.job["body"])
        self.assertEqual(calls["live"], 0)


if __name__ == "__main__":
    unittest.main()
