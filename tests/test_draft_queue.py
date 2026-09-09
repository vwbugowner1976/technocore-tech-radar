import tempfile
import unittest
from pathlib import Path

from draft_queue import supersede_stale_pending
from technoscout.db import (
    connect,
    create_reply_draft,
    get_reply_draft,
    mark_pending_draft_status,
    pending_reply_drafts,
    reply_draft_counts,
    reply_drafts_by_status,
    supersede_older_pending_drafts,
)


class DraftQueueTests(unittest.TestCase):
    def test_blocked_status_moves_out_of_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            self.assertTrue(create_reply_draft(
                con,
                "2026-09-09T00:00:00Z",
                "agents",
                10,
                "did:key:test",
                50,
                "test",
                "Could you share the benchmark method?",
            ))
            draft_id = int(pending_reply_drafts(con, 1)[0]["id"])
            self.assertTrue(
                mark_pending_draft_status(con, draft_id, "autonomy_blocked")
            )
            con.commit()
            self.assertEqual(reply_draft_counts(con)["pending"], 0)
            self.assertEqual(reply_draft_counts(con)["autonomy_blocked"], 1)
            self.assertEqual(
                int(reply_drafts_by_status(
                    con, "autonomy_blocked", 1
                )[0]["id"]),
                draft_id,
            )
            con.close()

    def test_supersede_older_pending_same_room_and_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            for seq in (10, 20, 30):
                self.assertTrue(create_reply_draft(
                    con,
                    f"2026-09-09T00:00:{seq:02d}Z",
                    "flop-index",
                    seq,
                    "did:key:flop-agent",
                    100,
                    "test",
                    f"draft {seq}",
                ))
            rows = pending_reply_drafts(con, 10)
            newest = int(rows[0]["id"])
            changed = supersede_older_pending_drafts(
                con, "flop-index", "did:key:flop-agent", newest
            )
            con.commit()
            self.assertEqual(changed, 2)
            self.assertEqual(reply_draft_counts(con)["pending"], 1)
            self.assertEqual(reply_draft_counts(con)["superseded"], 2)
            self.assertEqual(get_reply_draft(con, newest)["status"], "pending")
            con.close()

    def test_pending_older_than_sent_is_superseded(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            for seq in (10, 20, 30):
                self.assertTrue(create_reply_draft(
                    con,
                    f"2026-09-09T00:00:{seq:02d}Z",
                    "flop-index",
                    seq,
                    "did:key:flop-agent",
                    100,
                    "test",
                    f"draft {seq}",
                ))
            rows = pending_reply_drafts(con, 10)
            ids_by_seq = {int(row["through_seq"]): int(row["id"]) for row in rows}
            con.execute(
                "UPDATE reply_drafts SET status='sent' WHERE id=?",
                (ids_by_seq[20],),
            )
            con.commit()

            changed = supersede_stale_pending(con)
            con.commit()
            self.assertEqual(changed, 1)
            self.assertEqual(
                get_reply_draft(con, ids_by_seq[10])["status"],
                "superseded",
            )
            self.assertEqual(
                get_reply_draft(con, ids_by_seq[30])["status"],
                "pending",
            )
            con.close()

    def test_other_agent_is_not_superseded(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            self.assertTrue(create_reply_draft(
                con, "2026-09-09T00:00:00Z", "agents", 1,
                "did:key:a", 80, "test", "draft a",
            ))
            self.assertTrue(create_reply_draft(
                con, "2026-09-09T00:00:01Z", "agents", 2,
                "did:key:b", 80, "test", "draft b",
            ))
            newest = int(pending_reply_drafts(con, 1)[0]["id"])
            changed = supersede_older_pending_drafts(
                con, "agents", "did:key:b", newest
            )
            con.commit()
            self.assertEqual(changed, 0)
            self.assertEqual(reply_draft_counts(con)["pending"], 2)
            con.close()


if __name__ == "__main__":
    unittest.main()
