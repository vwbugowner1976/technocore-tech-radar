import importlib.util
import unittest
from pathlib import Path

_MAIN_PATH = Path(__file__).resolve().parents[1] / "technoscout.py"
_SPEC = importlib.util.spec_from_file_location("technoscout_main_progress", _MAIN_PATH)
_MAIN = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(_MAIN)
progress_only_batch_followup = _MAIN.progress_only_batch_followup
opaque_flop_index_reference_batch = _MAIN.opaque_flop_index_reference_batch


class ProgressGateTests(unittest.TestCase):
    def test_flop_index_opaque_kibble_rows_are_blocked_from_research(self):
        messages = [
            {
                "seq": 2837,
                "text": "read kibble seq 2956236 from z6Mk...XN6w · prose · deal · analysing",
            },
            {
                "seq": 2836,
                "text": "read kibble seq 2954835 from z6Mk...NhbC · prose · deal · analysing",
            },
        ]
        self.assertTrue(
            opaque_flop_index_reference_batch("flop-index", messages)
        )

    def test_flop_index_explicit_findings_are_allowed_through(self):
        messages = [
            {
                "seq": 2900,
                "text": "read kibble seq 3000000 · analysis completed · findings: Zephyr timing regression",
            }
        ]
        self.assertFalse(
            opaque_flop_index_reference_batch("flop-index", messages)
        )

    def test_opaque_gate_does_not_apply_to_other_rooms(self):
        messages = [
            {
                "seq": 1,
                "text": "read kibble seq 123 · prose · job · analysing",
            }
        ]
        self.assertFalse(
            opaque_flop_index_reference_batch("agents", messages)
        )

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
