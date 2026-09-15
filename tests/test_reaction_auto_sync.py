import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reaction_memory import (
    auto_sync_reaction_memory,
    reaction_check_due,
)
from technoscout.db import connect, get_reaction_memory


class ReactionAutoSyncTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 10, 0, 10, 0, tzinfo=timezone.utc)

    def test_unsynced_send_is_due(self):
        self.assertTrue(
            reaction_check_due(
                attempted_at=(self.now - timedelta(days=2)).isoformat(),
                last_checked_at="",
                classification="",
                now=self.now,
                max_age_seconds=86400,
            )
        )

    def test_recent_send_waits_one_minute_between_checks(self):
        attempted = (self.now - timedelta(minutes=5)).isoformat()
        self.assertFalse(
            reaction_check_due(
                attempted_at=attempted,
                last_checked_at=(self.now - timedelta(seconds=30)).isoformat(),
                classification="ROOM_ACTIVITY",
                now=self.now,
            )
        )
        self.assertTrue(
            reaction_check_due(
                attempted_at=attempted,
                last_checked_at=(self.now - timedelta(seconds=61)).isoformat(),
                classification="ROOM_ACTIVITY",
                now=self.now,
            )
        )

    def test_direct_reply_is_terminal_for_auto_recheck(self):
        self.assertFalse(
            reaction_check_due(
                attempted_at=(self.now - timedelta(minutes=1)).isoformat(),
                last_checked_at=(self.now - timedelta(seconds=90)).isoformat(),
                classification="DIRECT_REPLY",
                now=self.now,
            )
        )

    def test_old_synced_send_exits_auto_tracking_horizon(self):
        self.assertFalse(
            reaction_check_due(
                attempted_at=(self.now - timedelta(days=2)).isoformat(),
                last_checked_at=(self.now - timedelta(hours=2)).isoformat(),
                classification="ROOM_ACTIVITY",
                now=self.now,
                max_age_seconds=86400,
            )
        )

    def test_auto_sync_persists_direct_reply_with_fake_fetcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            attempted_at = (self.now - timedelta(minutes=1)).isoformat()
            con.execute(
                """
                INSERT INTO reply_drafts(
                  id,created_at,room,through_seq,target_agent,
                  relationship_score,reason,draft_text,status
                ) VALUES(1,?,'agents',99,'did:key:target',80,
                         'reason','benchmark question','sent')
                """,
                (attempted_at,),
            )
            con.execute(
                """
                INSERT INTO send_attempts(
                  id,draft_id,attempted_at,did,room,nonce,sig,text,status,
                  http_status,detail
                ) VALUES(1,1,?,'did:key:self','agents','1','sig',
                         'Could you share the nRF52840 benchmark?',
                         'sent',200,'seq=100')
                """,
                (attempted_at,),
            )
            con.commit()

            def fetcher(cfg, room, seq, limit):
                self.assertEqual(room, "agents")
                self.assertEqual(seq, 100)
                return [
                    {
                        "seq": 101,
                        "from": "did:key:target",
                        "text": "Re: seq 100 — nRF52840 benchmark is 1.2 ms/token.",
                    }
                ]

            stats = auto_sync_reaction_memory(
                con,
                {},
                now=self.now,
                limit=6,
                message_limit=200,
                fetcher=fetcher,
            )
            self.assertEqual(stats["due"], 1)
            self.assertEqual(stats["inserted"], 1)
            row = get_reaction_memory(con, 1)
            self.assertEqual(row["classification"], "DIRECT_REPLY")
            self.assertEqual(row["responder_did"], "did:key:target")
            con.close()


if __name__ == "__main__":
    unittest.main()
