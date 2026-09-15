import tempfile
import unittest
from pathlib import Path

from collaboration_shadow_eval import (
    evaluate_shadow_row,
    sync_shadow_evaluations,
)
from technoscout.db import (
    collaboration_shadow_evaluation_counts,
    collaboration_shadow_evaluation_rows,
    collaboration_shadow_rows,
    connect,
    record_collaboration_shadow_decision,
    upsert_reaction_memory,
)


class CollaborationShadowEvalTests(unittest.TestCase):
    def _decision(self, con, marker="SAME", actual="did:key:target", shadow=None):
        record_collaboration_shadow_decision(
            con,
            observed_at="2026-09-10T00:00:00+00:00",
            room="lab",
            through_seq=100,
            marker=marker,
            actual_agent=actual,
            shadow_agent=shadow or actual,
            actual_relationship=80,
            shadow_relationship=75,
            shadow_collaboration=50,
            shadow_combined=66,
            candidate_count=2,
            responder_direct=0,
            responder_likely=1,
            target_direct=0,
            target_likely=0,
        )
        con.commit()
        return collaboration_shadow_rows(con, 1)[0]

    def _sent(self, con, target="did:key:target"):
        con.execute(
            """
            INSERT INTO reply_drafts(
              id,created_at,room,through_seq,target_agent,
              relationship_score,reason,draft_text,status
            ) VALUES(1,'2026-09-10T00:00:01+00:00','lab',100,?,
                     80,'reason','question','sent')
            """,
            (target,),
        )
        con.execute(
            """
            INSERT INTO send_attempts(
              id,draft_id,attempted_at,did,room,nonce,sig,text,status,
              http_status,detail
            ) VALUES(1,1,'2026-09-10T00:00:02+00:00','did:key:self',
                     'lab','1','sig','question','sent',200,'seq=101')
            """
        )
        con.commit()

    def test_unsent_shadow_decision_is_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            row = self._decision(con)
            result = evaluate_shadow_row(con, row)
            self.assertEqual(result["state"], "UNRESOLVED")
            self.assertIsNone(result["send_attempt_id"])
            con.close()

    def test_direct_reply_from_actual_target_resolves_replied(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            row = self._decision(con)
            self._sent(con)
            upsert_reaction_memory(
                con,
                send_attempt_id=1,
                draft_id=1,
                checked_at="2026-09-10T00:00:03+00:00",
                room="lab",
                our_seq=101,
                target_agent="did:key:target",
                classification="DIRECT_REPLY",
                coverage="OBSERVED",
                responder_did="did:key:target",
                responder_seq=102,
                overlap=1,
                foreign_posts=1,
            )
            con.commit()
            result = evaluate_shadow_row(con, row)
            self.assertEqual(result["state"], "ACTUAL_REPLIED")
            self.assertTrue(result["actual_target_replied"])
            con.close()

    def test_observed_reaction_from_other_agent_is_actual_no_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            row = self._decision(con)
            self._sent(con)
            upsert_reaction_memory(
                con,
                send_attempt_id=1,
                draft_id=1,
                checked_at="2026-09-10T00:00:03+00:00",
                room="lab",
                our_seq=101,
                target_agent="did:key:target",
                classification="LIKELY_REACTION",
                coverage="OBSERVED",
                responder_did="did:key:other",
                responder_seq=104,
                overlap=2,
                foreign_posts=3,
            )
            con.commit()
            result = evaluate_shadow_row(con, row)
            self.assertEqual(result["state"], "ACTUAL_NO_REPLY")
            self.assertFalse(result["actual_target_replied"])
            con.close()

    def test_partial_window_remains_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            row = self._decision(con)
            self._sent(con)
            upsert_reaction_memory(
                con,
                send_attempt_id=1,
                draft_id=1,
                checked_at="2026-09-10T00:00:03+00:00",
                room="lab",
                our_seq=101,
                target_agent="did:key:target",
                classification="WINDOW_TRUNCATED",
                coverage="PARTIAL",
                responder_did="did:key:other",
                responder_seq=500,
                overlap=0,
                foreign_posts=200,
            )
            con.commit()
            result = evaluate_shadow_row(con, row)
            self.assertEqual(result["state"], "UNRESOLVED")
            con.close()

    def test_sync_persists_and_only_counts_real_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            self._decision(con)
            self._sent(con)
            upsert_reaction_memory(
                con,
                send_attempt_id=1,
                draft_id=1,
                checked_at="2026-09-10T00:00:03+00:00",
                room="lab",
                our_seq=101,
                target_agent="did:key:target",
                classification="ROOM_ACTIVITY",
                coverage="OBSERVED",
                responder_did="did:key:other",
                responder_seq=102,
                overlap=0,
                foreign_posts=2,
            )
            con.commit()

            first = sync_shadow_evaluations(con, limit=10)
            second = sync_shadow_evaluations(con, limit=10)

            self.assertEqual(first["changed"], 1)
            self.assertEqual(first["actual_no_reply"], 1)
            self.assertEqual(second["changed"], 0)

            counts = collaboration_shadow_evaluation_counts(con)
            self.assertEqual(counts["ACTUAL_NO_REPLY"], 1)
            rows = collaboration_shadow_evaluation_rows(con, 10)
            self.assertEqual(rows[0]["state"], "ACTUAL_NO_REPLY")
            con.close()


if __name__ == "__main__":
    unittest.main()
