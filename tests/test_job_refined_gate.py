import sqlite3
import unittest
from datetime import datetime, timezone

from job_candidate_refiner import ensure_refiner_schema, store_refinement
from job_refined_gate import evaluate_refined_gate, refined_candidate_rows
from job_shadow import ensure_job_shadow_schema, record_job_shadow_candidate


class RefinedJobGateTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_job_shadow_schema(self.con)
        ensure_refiner_schema(self.con)
        self.now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.issuer = "did:key:z6Mkstrong"

        # Enough global baseline and strong issuer lifecycle evidence.
        for index in range(60):
            issuer = self.issuer if index < 3 else f"did:key:z6Mkissuer{index:03d}"
            lifecycle = "ATTESTED" if index < 55 else "OPEN"
            record_job_shadow_candidate(
                self.con,
                seen_at=self.now,
                room="kibble",
                job_id=f"k{index:010x}",
                job_seq=index + 1,
                issuer_did=issuer,
                signed_identity=True,
                job_type="explain",
                digest=f"d{index}",
                lifecycle=lifecycle,
                evaluation={
                    "fit_class": "SKIP_CLOSED" if lifecycle != "OPEN" else "NOT_RELEVANT",
                    "relevance": 0 if lifecycle != "OPEN" else 25,
                    "technical_fit": 0 if lifecycle != "OPEN" else 30,
                    "confidence": 100 if lifecycle != "OPEN" else 65,
                    "effort": "unknown" if lifecycle != "OPEN" else "small",
                    "required_capabilities": [],
                    "reason": "fixture",
                    "summary": "",
                },
            )

        # Add enough OPEN rows to satisfy the default board baseline.
        for index in range(60, 80):
            record_job_shadow_candidate(
                self.con,
                seen_at=self.now,
                room="kibble",
                job_id=f"k{index:010x}",
                job_seq=index + 1,
                issuer_did=f"did:key:z6Mkissuer{index:03d}",
                signed_identity=True,
                job_type="coordinate",
                digest=f"d{index}",
                lifecycle="OPEN",
                evaluation={
                    "fit_class": "NOT_RELEVANT",
                    "relevance": 25,
                    "technical_fit": 30,
                    "confidence": 65,
                    "effort": "small",
                    "required_capabilities": [],
                    "reason": "fixture",
                    "summary": "",
                },
            )

        # Semantic false negative recovered by the refiner.
        self.candidate = {
            "room": "kibble",
            "job_id": "kabcdef0123",
            "content_hash": "candidate-digest",
        }
        record_job_shadow_candidate(
            self.con,
            seen_at=self.now,
            room="kibble",
            job_id=self.candidate["job_id"],
            job_seq=1000,
            issuer_did=self.issuer,
            signed_identity=True,
            job_type="explain",
            digest=self.candidate["content_hash"],
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
        store_refinement(
            self.con,
            self.candidate,
            {
                "decision": "SAFE_FIT",
                "relevance": 80,
                "technical_fit": 90,
                "confidence": 95,
                "effort": "small",
                "reason": "semantic match",
            },
        )
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def test_safe_fit_can_recover_deterministic_not_relevant(self):
        rows = refined_candidate_rows(self.con, {}, limit=5)
        ids = [row["job_id"] for row in rows]
        self.assertIn("kabcdef0123", ids)
        row = next(row for row in rows if row["job_id"] == "kabcdef0123")
        self.assertEqual(row["deterministic_class"], "NOT_RELEVANT")
        self.assertEqual(row["refined_fit"], 90)

    def test_non_live_gate_requires_revalidation(self):
        result = evaluate_refined_gate(self.con, {}, live=False, candidate_limit=5)
        self.assertEqual(result.state, "NEEDS_LIVE_REVALIDATION")
        self.assertFalse(result.ready_for_manual_claim_trial)

    def test_live_open_candidate_becomes_ready_only_for_manual_trial(self):
        def revalidator(cfg, candidate):
            return {
                "state": "OPEN_CONFIRMED",
                "lifecycle": "OPEN",
                "pages": 1,
                "messages": 12,
            }

        result = evaluate_refined_gate(
            self.con,
            {},
            live=True,
            candidate_limit=5,
            revalidator=revalidator,
        )
        self.assertEqual(result.state, "READY_FOR_MANUAL_CLAIM_TRIAL")
        self.assertTrue(result.ready_for_manual_claim_trial)

    def test_live_error_fails_closed(self):
        def revalidator(cfg, candidate):
            raise RuntimeError("503")

        result = evaluate_refined_gate(
            self.con,
            {},
            live=True,
            candidate_limit=5,
            revalidator=revalidator,
        )
        self.assertEqual(result.state, "LIVE_CHECK_INCONCLUSIVE")
        self.assertFalse(result.ready_for_manual_claim_trial)


if __name__ == "__main__":
    unittest.main()
