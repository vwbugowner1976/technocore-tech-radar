import sqlite3
import unittest
from datetime import datetime, timezone

from job_candidate_refiner import (
    fetch_exact_job,
    recent_near_miss_rows,
    refine_candidate,
)
from job_shadow import ensure_job_shadow_schema, record_job_shadow_candidate


class JobCandidateRefinerTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_job_shadow_schema(self.con)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Strong issuer evidence.
        issuer = "did:key:z6Mkstrong"
        for index, lifecycle in enumerate(("ATTESTED", "DELIVERED", "ATTESTED"), start=1):
            record_job_shadow_candidate(
                self.con,
                seen_at=now,
                room="kibble",
                job_id=f"k{index:010x}",
                job_seq=index,
                issuer_did=issuer,
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
                    "reason": "closed",
                    "summary": "",
                },
            )
        record_job_shadow_candidate(
            self.con,
            seen_at=now,
            room="kibble",
            job_id="kabcdef0123",
            job_seq=100,
            issuer_did=issuer,
            signed_identity=True,
            job_type="explain",
            digest="placeholder",
            lifecycle="OPEN",
            evaluation={
                "fit_class": "FIT",
                "relevance": 47,
                "technical_fit": 71,
                "confidence": 75,
                "effort": "small",
                "required_capabilities": ["local-llm"],
                "reason": "near miss",
                "summary": "",
            },
        )
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def test_recent_near_miss_requires_good_issuer(self):
        rows = recent_near_miss_rows(self.con, {}, limit=5, max_age_seconds=3600)
        self.assertEqual([r["job_id"] for r in rows], ["kabcdef0123"])

    def test_fetch_exact_job_fails_closed_on_hash_mismatch(self):
        candidate = dict(recent_near_miss_rows(self.con, {}, limit=1, max_age_seconds=3600)[0])
        candidate["content_hash"] = "not-the-real-hash"

        def fetcher(cfg, path, query):
            return {"messages": [{
                "seq": 100,
                "from": candidate["issuer_did"],
                "text": "JOB v1 | kabcdef0123 | explain | Title | Body",
            }]}

        result = fetch_exact_job({}, candidate, fetcher=fetcher)
        self.assertEqual(result["state"], "MISMATCH")

    def test_fetch_exact_job_falls_back_to_retained_export(self):
        candidate = dict(recent_near_miss_rows(self.con, {}, limit=1, max_age_seconds=3600)[0])
        from job_shadow import content_hash, parse_kibble_message
        parsed = parse_kibble_message("JOB v1 | kabcdef0123 | explain | Title | Body")
        self.assertIsNotNone(parsed)
        candidate["content_hash"] = content_hash(parsed)

        def fetcher(cfg, path, query):
            # Simulate a very busy room: the normal tail no longer includes seq 100.
            return {"messages": [{"seq": 1000, "from": "did:key:z6Mkother", "text": "noise"}]}

        def export_fetcher(cfg, room):
            return [{
                "seq": 100,
                "from": candidate["issuer_did"],
                "text": "JOB v1 | kabcdef0123 | explain | Title | Body",
            }]

        result = fetch_exact_job({}, candidate, fetcher=fetcher, export_fetcher=export_fetcher)
        self.assertEqual(result["state"], "EXACT")
        self.assertEqual(result["job"]["job_id"], "kabcdef0123")

    def test_refine_candidate_normalizes_semantic_result(self):
        candidate = dict(recent_near_miss_rows(self.con, {}, limit=1, max_age_seconds=3600)[0])

        def fake_fetch(cfg, path, query):
            return {"messages": [{
                "seq": 100,
                "from": candidate["issuer_did"],
                "text": "JOB v1 | kabcdef0123 | explain | Title | Body",
            }]}

        # Make expected hash exactly match the fetched record.
        from job_shadow import content_hash, parse_kibble_message
        parsed = parse_kibble_message("JOB v1 | kabcdef0123 | explain | Title | Body")
        self.assertIsNotNone(parsed)
        candidate["content_hash"] = content_hash(parsed)

        def evaluator(*args, **kwargs):
            return {
                "decision": "SAFE_FIT",
                "relevance": 82,
                "technical_fit": 88,
                "confidence": 91,
                "effort": "small",
                "reason": "self-contained semantic match",
            }

        result = refine_candidate({}, candidate, None, "model", fetcher=fake_fetch, evaluator=evaluator)
        self.assertEqual(result["decision"], "SAFE_FIT")
        self.assertEqual(result["relevance"], 82)
        self.assertEqual(result["technical_fit"], 88)


if __name__ == "__main__":
    unittest.main()
