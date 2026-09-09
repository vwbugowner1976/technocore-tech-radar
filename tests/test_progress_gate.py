import importlib.util
import unittest
from pathlib import Path

_MAIN_PATH = Path(__file__).resolve().parents[1] / "technoscout.py"
_SPEC = importlib.util.spec_from_file_location("technoscout_main_progress", _MAIN_PATH)
_MAIN = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(_MAIN)
progress_only_batch_followup = _MAIN.progress_only_batch_followup


class ProgressGateTests(unittest.TestCase):
    def test_flop_index_progress_only_is_deferred(self):
        messages = [
            {
                "seq": 2753,
                "text": "read kibble seq 3443310…3447413 · 1897 candidates · analysing",
            }
        ]
        self.assertTrue(
            progress_only_batch_followup("flop-index", messages, [2753])
        )

    def test_flop_index_findings_are_not_deferred(self):
        messages = [
            {
                "seq": 2800,
                "text": "analysis completed; top candidate findings: Zephyr timing fix",
            }
        ]
        self.assertFalse(
            progress_only_batch_followup("flop-index", messages, [2800])
        )

    def test_other_room_is_not_affected(self):
        messages = [{"seq": 1, "text": "1897 candidates analysing"}]
        self.assertFalse(
            progress_only_batch_followup("agents", messages, [1])
        )


if __name__ == "__main__":
    unittest.main()
