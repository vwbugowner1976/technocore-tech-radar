import tempfile
import unittest
from pathlib import Path

from technoscout.common import clamp_score, event_room, parse_json_object, safe_room
from technoscout.db import connect, get_meta, set_meta


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


if __name__ == "__main__":
    unittest.main()
