import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from technoscout.db import (
    connect,
    create_reply_draft,
    get_autonomy_halt,
    get_meta,
    get_reply_draft,
    pending_reply_drafts,
)
from technoscout.sender import SendRefused, is_room_acl_refusal

_MAIN_PATH = Path(__file__).resolve().parents[1] / "technoscout.py"
_SPEC = importlib.util.spec_from_file_location("technoscout_main_acl", _MAIN_PATH)
_MAIN = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(_MAIN)


class RoomAclTests(unittest.TestCase):
    def test_room_acl_refusal_classifier(self):
        body = (
            "403 z6Mk…UGKz is not listed for /r/d-ton. "
            "The owner adds keys with a signed write to /kv/room-allow/d-ton."
        )
        self.assertTrue(is_room_acl_refusal(403, body))
        self.assertFalse(is_room_acl_refusal(403, "forbidden"))
        self.assertFalse(is_room_acl_refusal(429, body))

    def test_existing_acl_refusal_is_learned(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            con.execute(
                """
                INSERT INTO send_attempts(
                  draft_id,attempted_at,did,room,nonce,sig,text,status,http_status,detail
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    1,
                    "2026-09-09T12:24:45Z",
                    "did:key:test",
                    "d-ton",
                    "1",
                    "sig",
                    "hello",
                    "refused",
                    403,
                    "403 test is not listed for /r/d-ton; /kv/room-allow/d-ton",
                ),
            )
            con.commit()
            scout = object.__new__(_MAIN.TechnoScout)
            scout.db = con
            learned = scout._learn_room_acl_blocks()
            self.assertEqual(learned, 1)
            self.assertIn(
                "HTTP 403 room ACL refusal",
                get_meta(con, "autonomy_room_acl_block:d-ton"),
            )
            self.assertEqual(scout._learn_room_acl_blocks(), 0)
            con.close()

    def test_acl_refusal_does_not_engage_global_halt(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            self.assertTrue(create_reply_draft(
                con,
                "2026-09-09T12:30:00Z",
                "d-new",
                100,
                "did:key:target",
                100,
                "technical question",
                "Could you share the nRF52840 benchmark methodology?",
            ))
            con.commit()
            draft_id = int(pending_reply_drafts(con, 1)[0]["id"])

            scout = object.__new__(_MAIN.TechnoScout)
            scout.db = con
            scout.cfg = {
                "autonomy_mode": "limited",
                "autonomy_min_relevance": 75,
                "autonomy_min_technical": 75,
                "autonomy_min_relationship": 30,
                "autonomy_max_sends_per_hour": 3,
                "autonomy_room_cooldown_seconds": 3600,
                "autonomy_agent_cooldown_seconds": 3600,
                "autonomy_max_draft_chars": 600,
                "autonomy_blocked_room_terms": [],
                "autonomy_blocked_text_terms": [],
                "autonomy_required_technical_terms": ["nrf52840", "benchmark"],
                "signing_seed_env": "TECHNOSCOUT_TEST_MISSING",
                "signing_env_file": "",
            }

            class FakeSender:
                def __init__(self, cfg, db):
                    pass

                def arm_draft(self, draft):
                    return {"token": "fake"}

                def send_draft(self, draft, permit_token=None):
                    raise SendRefused(
                        403,
                        "403 test is not listed for /r/d-new; "
                        "/kv/room-allow/d-new",
                    )

            with patch.object(_MAIN, "ApprovedDraftSender", FakeSender):
                scout._autonomy_handle_draft(
                    draft_id,
                    {
                        "summary": "nRF52840 benchmark discussion",
                        "relevance": 90,
                        "technical": 90,
                    },
                    [100],
                    ["nrf52840", "benchmark"],
                )

            self.assertEqual(get_autonomy_halt(con), "")
            self.assertTrue(get_meta(con, "autonomy_room_acl_block:d-new"))
            con.close()


if __name__ == "__main__":
    unittest.main()
