import tempfile
import unittest
from pathlib import Path

from technoscout.db import (
    connect,
    get_reaction_memory,
    reaction_memory_counts,
    upsert_reaction_memory,
)


class ReactionMemoryTests(unittest.TestCase):
    def test_insert_reaction_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            action = upsert_reaction_memory(
                con,
                send_attempt_id=7,
                draft_id=42,
                checked_at="2026-09-10T00:00:00+00:00",
                room="lab",
                our_seq=1172,
                target_agent="did:key:target",
                classification="LIKELY_REACTION",
                coverage="OBSERVED",
                responder_did="did:key:responder",
                responder_seq=1179,
                overlap=2,
                foreign_posts=10,
            )
            con.commit()
            self.assertEqual(action, "inserted")
            row = get_reaction_memory(con, 7)
            self.assertEqual(row["classification"], "LIKELY_REACTION")
            self.assertEqual(row["responder_seq"], 1179)
            self.assertEqual(row["check_count"], 1)
            con.close()

    def test_strong_observed_memory_survives_later_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            upsert_reaction_memory(
                con,
                send_attempt_id=1,
                draft_id=42,
                checked_at="2026-09-10T00:00:00+00:00",
                room="lab",
                our_seq=100,
                target_agent="did:key:target",
                classification="LIKELY_REACTION",
                coverage="OBSERVED",
                responder_did="did:key:responder",
                responder_seq=107,
                overlap=2,
                foreign_posts=20,
            )
            action = upsert_reaction_memory(
                con,
                send_attempt_id=1,
                draft_id=42,
                checked_at="2026-09-10T01:00:00+00:00",
                room="lab",
                our_seq=100,
                target_agent="did:key:target",
                classification="WINDOW_TRUNCATED",
                coverage="PARTIAL",
                foreign_posts=200,
            )
            con.commit()
            self.assertEqual(action, "preserved")
            row = get_reaction_memory(con, 1)
            self.assertEqual(row["classification"], "LIKELY_REACTION")
            self.assertEqual(row["coverage"], "OBSERVED")
            self.assertEqual(row["responder_did"], "did:key:responder")
            self.assertEqual(row["check_count"], 2)
            self.assertEqual(row["foreign_posts"], 200)
            con.close()

    def test_observed_memory_can_upgrade_to_direct_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            upsert_reaction_memory(
                con,
                send_attempt_id=2,
                draft_id=2,
                checked_at="2026-09-10T00:00:00+00:00",
                room="agents",
                our_seq=100,
                target_agent="did:key:target",
                classification="ROOM_ACTIVITY",
                coverage="OBSERVED",
                foreign_posts=5,
            )
            action = upsert_reaction_memory(
                con,
                send_attempt_id=2,
                draft_id=2,
                checked_at="2026-09-10T00:05:00+00:00",
                room="agents",
                our_seq=100,
                target_agent="did:key:target",
                classification="DIRECT_REPLY",
                coverage="OBSERVED",
                responder_did="did:key:target",
                responder_seq=101,
                foreign_posts=6,
            )
            con.commit()
            self.assertEqual(action, "updated")
            row = get_reaction_memory(con, 2)
            self.assertEqual(row["classification"], "DIRECT_REPLY")
            self.assertEqual(row["responder_did"], "did:key:target")
            self.assertEqual(row["check_count"], 2)
            con.close()

    def test_counts_do_not_store_raw_reply_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            upsert_reaction_memory(
                con,
                send_attempt_id=3,
                draft_id=3,
                checked_at="2026-09-10T00:00:00+00:00",
                room="flop-index",
                our_seq=2731,
                target_agent="did:key:target",
                classification="ROOM_ACTIVITY",
                coverage="OBSERVED",
                foreign_posts=108,
            )
            con.commit()
            counts = reaction_memory_counts(con)
            self.assertEqual(counts["ROOM_ACTIVITY"], 1)
            columns = {
                row["name"]
                for row in con.execute("PRAGMA table_info(reaction_memory)").fetchall()
            }
            self.assertNotIn("text", columns)
            self.assertNotIn("message", columns)
            self.assertNotIn("raw_text", columns)
            con.close()


if __name__ == "__main__":
    unittest.main()
