import tempfile
import unittest
from pathlib import Path

from collaboration_shadow import collaboration_profile, shadow_candidate
from technoscout.db import connect, upsert_reaction_memory


class CollaborationShadowTests(unittest.TestCase):
    def test_likely_responder_gets_small_nonzero_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            upsert_reaction_memory(
                con,
                send_attempt_id=1,
                draft_id=42,
                checked_at="2026-09-10T00:00:00+00:00",
                room="lab",
                our_seq=100,
                target_agent="did:key:other",
                classification="LIKELY_REACTION",
                coverage="OBSERVED",
                responder_did="did:key:responder",
                responder_seq=107,
                overlap=2,
                foreign_posts=10,
            )
            con.commit()
            profile = collaboration_profile(con, "did:key:responder")
            self.assertEqual(profile["responder_likely"], 1)
            self.assertGreater(profile["score"], 0)
            self.assertLess(profile["score"], 50)
            con.close()

    def test_direct_target_reply_scores_more_strongly(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            upsert_reaction_memory(
                con,
                send_attempt_id=1,
                draft_id=1,
                checked_at="2026-09-10T00:00:00+00:00",
                room="agents",
                our_seq=100,
                target_agent="did:key:target",
                classification="DIRECT_REPLY",
                coverage="OBSERVED",
                responder_did="did:key:target",
                responder_seq=101,
                overlap=1,
                foreign_posts=3,
            )
            con.commit()
            profile = collaboration_profile(con, "did:key:target")
            self.assertEqual(profile["target_observed"], 1)
            self.assertEqual(profile["target_direct"], 1)
            self.assertGreaterEqual(profile["score"], 50)
            con.close()

    def test_partial_target_window_does_not_count_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            upsert_reaction_memory(
                con,
                send_attempt_id=1,
                draft_id=1,
                checked_at="2026-09-10T00:00:00+00:00",
                room="busy",
                our_seq=100,
                target_agent="did:key:target",
                classification="WINDOW_TRUNCATED",
                coverage="PARTIAL",
                responder_did="",
                foreign_posts=200,
            )
            con.commit()
            profile = collaboration_profile(con, "did:key:target")
            self.assertEqual(profile["target_observed"], 0)
            self.assertEqual(profile["target_partial"], 1)
            self.assertEqual(profile["score"], 0)
            con.close()

    def test_shadow_can_prefer_collaborative_candidate_without_changing_relationship(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            upsert_reaction_memory(
                con,
                send_attempt_id=1,
                draft_id=1,
                checked_at="2026-09-10T00:00:00+00:00",
                room="lab",
                our_seq=100,
                target_agent="did:key:someone",
                classification="DIRECT_REPLY",
                coverage="OBSERVED",
                responder_did="did:key:b",
                responder_seq=101,
                overlap=2,
                foreign_posts=4,
            )
            con.commit()

            relationships = {
                "did:key:a": {"score": 90},
                "did:key:b": {"score": 80},
            }
            best = shadow_candidate(
                con,
                ["did:key:a", "did:key:b"],
                relationship_lookup=lambda aid: relationships[aid],
                collaboration_weight_percent=35,
            )
            self.assertEqual(best["agent_id"], "did:key:b")
            self.assertTrue(best["has_reaction_evidence"])
            self.assertGreater(best["collaboration"], 0)
            con.close()

    def test_no_reaction_evidence_keeps_relationship_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            relationships = {
                "did:key:a": {"score": 90},
                "did:key:b": {"score": 80},
            }
            best = shadow_candidate(
                con,
                ["did:key:a", "did:key:b"],
                relationship_lookup=lambda aid: relationships[aid],
                collaboration_weight_percent=35,
            )
            self.assertEqual(best["agent_id"], "did:key:a")
            self.assertFalse(best["has_reaction_evidence"])
            con.close()


if __name__ == "__main__":
    unittest.main()
