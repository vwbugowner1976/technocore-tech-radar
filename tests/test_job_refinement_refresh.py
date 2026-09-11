import sqlite3
import unittest
from datetime import datetime, timezone

from job_candidate_refiner import ensure_refiner_schema
from job_refinement_refresh import candidate_by_job_id, refresh_candidate
from job_shadow import ensure_job_shadow_schema, record_job_shadow_candidate


class JobRefinementRefreshTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_job_shadow_schema(self.con)
        ensure_refiner_schema(self.con)
        self.now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.issuer = "did:key:z6Mkstrong"

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
            job_id="kabcdef0123",
            job_seq=100,
            issuer_did=self.issuer,
            signed_identity=True,
            job_type="coordinate",
            digest="digest",
            lifecycle="OPEN",
            evaluation={
                "fit_class": "NOT_RELEVANT",
                "relevance": 25,
                "technical_fit": 30,
                "confidence": 65,
                "effort": "small",
                "required_capabilities": ["local-llm"],
                "reason": "lexical false negative",
                "summary": "",
            },
        )
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def test_specific_open_candidate_can_be_selected_without_age_cutoff(self):
        candidate, reason = candidate_by_job_id(self.con, {}, "kabcdef0123")
        self.assertIsNotNone(candidate)
        self.assertEqual(reason, "eligible")
        self.assertEqual(candidate["job_id"], "kabcdef0123")

    def test_live_failure_never_refreshes(self):
        candidate, _ = candidate_by_job_id(self.con, {}, "kabcdef0123")

        def revalidator(cfg, item):
            return {"state": "NOT_OPEN", "lifecycle": "CLAIMED", "pages": 1, "messages": 10}

        result = refresh_candidate(
            self.con,
            {},
            candidate,
            None,
            "model",
            revalidator=revalidator,
            refiner=lambda *a, **k: {},
        )
        self.assertEqual(result["state"], "NOT_REFRESHED")
        count = self.con.execute("SELECT COUNT(*) FROM job_candidate_refinements").fetchone()[0]
        self.assertEqual(count, 0)

    def test_live_open_candidate_can_refresh_semantic_metadata(self):
        candidate, _ = candidate_by_job_id(self.con, {}, "kabcdef0123")

        def revalidator(cfg, item):
            return {"state": "OPEN_CONFIRMED", "lifecycle": "OPEN", "pages": 1, "messages": 10}

        def refiner(*args, **kwargs):
            return {
                "decision": "SAFE_FIT",
                "relevance": 84,
                "technical_fit": 91,
                "confidence": 96,
                "effort": "small",
                "reason": "still a good semantic fit",
            }

        result = refresh_candidate(
            self.con,
            {},
            candidate,
            None,
            "model",
            revalidator=revalidator,
            refiner=refiner,
        )
        self.assertEqual(result["state"], "REFRESHED")
        row = self.con.execute(
            "SELECT decision,relevance,technical_fit,confidence FROM job_candidate_refinements WHERE job_id=?",
            ("kabcdef0123",),
        ).fetchone()
        self.assertEqual(row["decision"], "SAFE_FIT")
        self.assertEqual(row["relevance"], 84)
        self.assertEqual(row["technical_fit"], 91)
        self.assertEqual(row["confidence"], 96)


if __name__ == "__main__":
    unittest.main()
