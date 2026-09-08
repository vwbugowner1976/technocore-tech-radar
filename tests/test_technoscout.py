import tempfile
import unittest
from pathlib import Path

from technoscout.common import clamp_score, event_room, parse_json_object, safe_room
from technoscout.db import (
    agent_context,
    agent_relationship,
    connect,
    create_reply_draft,
    get_meta,
    record_agent_encounter,
    record_agent_signal,
    pending_reply_drafts,
    set_meta,
    top_agents,
)


class CommonTests(unittest.TestCase):
    def test_room_validation(self):
        self.assertEqual(safe_room("d-aircooled-vw-lab"), "d-aircooled-vw-lab")
        self.assertEqual(safe_room("mb-p-abc123"), "mb-p-abc123")
        self.assertIsNone(safe_room("../etc/passwd"))
        self.assertIsNone(safe_room("https://example.com"))

    def test_event_room(self):
        self.assertEqual(event_room({"room": "new-lab"}), "new-lab")
        self.assertEqual(event_room({"text": "created embedded-lab"}), "embedded-lab")

    def test_json_parser(self):
        value = parse_json_object('{"relevance": 90}')
        self.assertEqual(value["relevance"], 90)

    def test_json_parser_fenced_with_trailing_text(self):
        sample = """Here is the result:
```json
{"relevance": 88, "action": "SAVE"}
```
<|im_end|>"""
        value = parse_json_object(sample)
        self.assertEqual(value["relevance"], 88)
        self.assertEqual(value["action"], "SAVE")

    def test_score_clamp(self):
        self.assertEqual(clamp_score(101), 100)
        self.assertEqual(clamp_score(-2), 0)
        self.assertEqual(clamp_score("42"), 42)


class DatabaseTests(unittest.TestCase):
    def test_meta_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            set_meta(con, "cursor", 123)
            con.commit()
            self.assertEqual(get_meta(con, "cursor"), "123")
            con.close()

    def test_agent_memory_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            record_agent_encounter(con, "did:key:test-agent", "embedded-lab", "2026-09-08T10:00:00+00:00", 2)
            record_agent_signal(
                con,
                ["did:key:test-agent"],
                "embedded-lab",
                "2026-09-08T10:01:00+00:00",
                ["zmk", "nrf52840"],
                "Useful embedded result",
                True,
            )
            con.commit()
            rows = top_agents(con, 5)
            self.assertEqual(rows[0]["agent_id"], "did:key:test-agent")
            self.assertEqual(rows[0]["encounter_count"], 2)
            self.assertEqual(rows[0]["useful_signal_count"], 1)
            self.assertEqual(rows[0]["followup_count"], 1)
            self.assertIn("zmk", rows[0]["topics"])
            context = agent_context(con, ["did:key:test-agent"])
            self.assertEqual(context[0]["signals"], 1)
            self.assertIn("nrf52840", context[0]["topics"])
            relationship = agent_relationship(con, "did:key:test-agent")
            self.assertGreaterEqual(relationship["score"], 30)
            con.close()

    def test_relationship_and_draft_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            record_agent_encounter(
                con,
                "did:key:draft-agent",
                "agents",
                "2026-09-08T11:00:00+00:00",
                3,
            )
            record_agent_signal(
                con,
                ["did:key:draft-agent"],
                "agents",
                "2026-09-08T11:01:00+00:00",
                ["security", "signing"],
                "Useful signing discussion",
                True,
            )
            relationship = agent_relationship(con, "did:key:draft-agent")
            created = create_reply_draft(
                con,
                "2026-09-08T11:02:00+00:00",
                "agents",
                42,
                "did:key:draft-agent",
                relationship["score"],
                "Ask for implementation detail",
                "Did you also test the canonicalization step across implementations?",
            )
            self.assertTrue(created)
            self.assertFalse(create_reply_draft(
                con,
                "2026-09-08T11:03:00+00:00",
                "agents",
                42,
                "did:key:draft-agent",
                relationship["score"],
                "duplicate",
                "duplicate",
            ))
            con.commit()
            drafts = pending_reply_drafts(con, 5)
            self.assertEqual(len(drafts), 1)
            self.assertEqual(drafts[0]["room"], "agents")
            self.assertEqual(drafts[0]["status"], "pending")
            con.close()


if __name__ == "__main__":
    unittest.main()
