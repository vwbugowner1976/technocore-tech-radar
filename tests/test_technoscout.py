import sys
import tempfile
import time
import unittest
from pathlib import Path

from technoscout.common import (
    clamp_score,
    event_room,
    normalize_evidence_source,
    parse_json_object,
    safe_room,
)
from technoscout.llm_backend import ManagedMLXBackend
from technoscout.db import (
    agent_context,
    agent_relationship,
    connect,
    create_reply_draft,
    get_meta,
    get_reply_draft,
    record_agent_encounter,
    record_agent_signal,
    pending_reply_drafts,
    reply_draft_counts,
    review_reply_draft,
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

    def test_evidence_source_normalization(self):
        self.assertEqual(normalize_evidence_source("topic"), "topic")
        self.assertEqual(normalize_evidence_source("MESSAGES"), "messages")
        self.assertEqual(normalize_evidence_source("topic|messages|none"), "none")
        self.assertEqual(normalize_evidence_source(None), "none")

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

    def test_draft_review_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            self.assertTrue(create_reply_draft(
                con,
                "2026-09-09T00:00:00+00:00",
                "agents",
                99,
                "did:key:review-agent",
                40,
                "Useful follow-up",
                "Could you share how you validated the implementation?",
            ))
            con.commit()
            draft = pending_reply_drafts(con, 5)[0]
            draft_id = int(draft["id"])
            self.assertTrue(review_reply_draft(con, draft_id, "approved"))
            con.commit()
            reviewed = get_reply_draft(con, draft_id)
            self.assertEqual(reviewed["status"], "approved")
            self.assertFalse(review_reply_draft(con, draft_id, "rejected"))
            counts = reply_draft_counts(con)
            self.assertEqual(counts["approved"], 1)
            self.assertEqual(counts["pending"], 0)
            with self.assertRaises(ValueError):
                review_reply_draft(con, draft_id, "sent")
            con.close()


class ManagedWorkerTests(unittest.TestCase):
    def test_timeout_kills_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "fake_worker.py"
            worker.write_text(
                """import argparse
import json
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--model")
args = parser.parse_args()
print(json.dumps({"type": "ready", "model": args.model}), flush=True)

for line in sys.stdin:
    req = json.loads(line)
    if req.get("op") == "chat":
        content = req.get("messages", [{}])[-1].get("content", "")
        if content == "hang":
            time.sleep(5)
        print(json.dumps({
            "type": "result",
            "id": req["id"],
            "ok": True,
            "content": "{}"
        }), flush=True)
""",
                encoding="utf-8",
            )
            cfg = {
                "mlx_worker_python": sys.executable,
                "mlx_worker_script": str(worker),
                "mlx_worker_log": str(root / "worker.log"),
                "mlx_worker_start_timeout_seconds": 2,
                "mlx_worker_first_request_extra_seconds": 0,
                "mlx_worker_kill_grace_seconds": 0.1,
                "triage_model": "fake-model",
                "research_model": "fake-model",
            }
            backend = ManagedMLXBackend(cfg)
            started = time.monotonic()
            try:
                with self.assertRaises(TimeoutError):
                    backend.chat(
                        "fake-model",
                        [{"role": "user", "content": "hang"}],
                        max_tokens=8,
                        temperature=0.0,
                        timeout_seconds=0.2,
                    )
                self.assertIsNone(backend.proc)
                self.assertLess(time.monotonic() - started, 3.0)
                result = backend.chat(
                    "fake-model",
                    [{"role": "user", "content": "recover"}],
                    max_tokens=8,
                    temperature=0.0,
                    timeout_seconds=1.0,
                )
                self.assertEqual(result, "{}")
                self.assertIsNotNone(backend.proc)
                self.assertGreaterEqual(backend.restart_count, 2)
            finally:
                backend.close()


if __name__ == "__main__":
    unittest.main()
