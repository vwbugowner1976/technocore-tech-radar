import tempfile
import unittest
from pathlib import Path

from collaboration_shadow_report import shadow_outcome
from technoscout.db import (
    collaboration_shadow_counts,
    collaboration_shadow_rows,
    connect,
    record_collaboration_shadow_decision,
    upsert_reaction_memory,
)


class CollaborationShadowHistoryTests(unittest.TestCase):
    def test_record_and_count_shadow_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            created = record_collaboration_shadow_decision(
                con,
                observed_at="2026-09-10T00:00:00+00:00",
                room="lab",
                through_seq=100,
                marker="WOULD_PREFER",
                actual_agent="did:key:a",
                shadow_agent="did:key:b",
                actual_relationship=90,
                shadow_relationship=80,
                shadow_collaboration=60,
                shadow_combined=73,
                candidate_count=2,
                responder_direct=1,
                responder_likely=1,
                target_direct=0,
                target_likely=0,
            )
            con.commit()
            self.assertTrue(created)
            counts = collaboration_shadow_counts(con)
            self.assertEqual(counts["WOULD_PREFER"], 1)
            rows = collaboration_shadow_rows(con, 10)
            self.assertEqual(rows[0]["shadow_agent"], "did:key:b")
            con.close()

    def test_duplicate_room_seq_is_not_recorded_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            kwargs = dict(
                observed_at="2026-09-10T00:00:00+00:00",
                room="lab",
                through_seq=100,
                marker="SAME",
                actual_agent="did:key:a",
                shadow_agent="did:key:a",
                actual_relationship=90,
                shadow_relationship=90,
                shadow_collaboration=20,
                shadow_combined=66,
                candidate_count=1,
                responder_direct=0,
                responder_likely=1,
                target_direct=0,
                target_likely=0,
            )
            self.assertTrue(record_collaboration_shadow_decision(con, **kwargs))
            self.assertFalse(record_collaboration_shadow_decision(con, **kwargs))
            con.commit()
            self.assertEqual(sum(collaboration_shadow_counts(con).values()), 1)
            con.close()

    def test_report_links_actual_sent_reaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            record_collaboration_shadow_decision(
                con,
                observed_at="2026-09-10T00:00:00+00:00",
                room="lab",
                through_seq=100,
                marker="SAME",
                actual_agent="did:key:target",
                shadow_agent="did:key:target",
                actual_relationship=80,
                shadow_relationship=80,
                shadow_collaboration=60,
                shadow_combined=73,
                candidate_count=2,
                responder_direct=1,
                responder_likely=0,
                target_direct=1,
                target_likely=0,
            )
            con.execute(
                """
                INSERT INTO reply_drafts(
                  id,created_at,room,through_seq,target_agent,
                  relationship_score,reason,draft_text,status
                ) VALUES(1,'2026-09-10T00:00:01+00:00','lab',100,
                         'did:key:target',80,'reason','question','sent')
                """
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
            row = collaboration_shadow_rows(con, 1)[0]
            outcome = shadow_outcome(con, row)
            self.assertTrue(outcome["sent"])
            self.assertTrue(outcome["actual_target_replied"])
            self.assertEqual(outcome["classification"], "DIRECT_REPLY")
            self.assertEqual(outcome["shadow_counterfactual"], "same-as-actual")
            con.close()

    def test_would_prefer_shadow_outcome_is_not_inferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            record_collaboration_shadow_decision(
                con,
                observed_at="2026-09-10T00:00:00+00:00",
                room="lab",
                through_seq=100,
                marker="WOULD_PREFER",
                actual_agent="did:key:a",
                shadow_agent="did:key:b",
                actual_relationship=90,
                shadow_relationship=80,
                shadow_collaboration=60,
                shadow_combined=73,
                candidate_count=2,
                responder_direct=1,
                responder_likely=0,
                target_direct=0,
                target_likely=0,
            )
            con.execute(
                """
                INSERT INTO reply_drafts(
                  id,created_at,room,through_seq,target_agent,
                  relationship_score,reason,draft_text,status
                ) VALUES(1,'2026-09-10T00:00:01+00:00','lab',100,
                         'did:key:a',90,'reason','question','pending')
                """
            )
            con.commit()
            row = collaboration_shadow_rows(con, 1)[0]
            outcome = shadow_outcome(con, row)
            self.assertFalse(outcome["sent"])
            self.assertEqual(outcome["shadow_counterfactual"], "not-tested")
            con.close()


if __name__ == "__main__":
    unittest.main()
