import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from technoscout.autonomy import evaluate_autonomy

_MAIN_PATH = Path(__file__).resolve().parents[1] / "technoscout.py"
_SPEC = importlib.util.spec_from_file_location("technoscout_main", _MAIN_PATH)
_MAIN = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(_MAIN)
load_config = _MAIN.load_config


class AutonomyConfigUpgradeTests(unittest.TestCase):
    def _config(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "config.json"
        path.write_text(json.dumps({
            "autonomy_mode": "limited",
            "autonomy_blocked_room_terms": ["tclk"],
            "autonomy_blocked_text_terms": ["wallet"],
            "autonomy_required_technical_terms": ["benchmark", "nrf52840"],
        }), encoding="utf-8")
        return tmp, load_config(str(path))

    def test_safety_terms_merge_into_older_local_config(self):
        tmp, cfg = self._config()
        try:
            self.assertIn("technocore", cfg["autonomy_blocked_room_terms"])
            self.assertIn("secret", cfg["autonomy_blocked_text_terms"])
            self.assertIn("liquidation", cfg["autonomy_blocked_text_terms"])
            self.assertIn("how many candidates", cfg["autonomy_blocked_text_terms"])
        finally:
            tmp.cleanup()

    def test_low_value_batch_count_question_is_blocked(self):
        tmp, cfg = self._config()
        try:
            decision = evaluate_autonomy(
                cfg,
                room="flop-index",
                draft_text="How many candidates have been analyzed so far?",
                signal_summary="nRF52840 benchmark analysis is running.",
                tags=["nrf52840", "benchmark"],
                relevance=90,
                technical=90,
                relationship=100,
                evidence_seqs=[2753],
                recent_hour_sends=0,
                room_cooldown_ok=True,
                agent_cooldown_ok=True,
            )
            self.assertFalse(decision.allowed)
            self.assertIn("how many candidates", decision.reason)
        finally:
            tmp.cleanup()

    def test_secret_request_is_blocked(self):
        tmp, cfg = self._config()
        try:
            decision = evaluate_autonomy(
                cfg,
                room="agents",
                draft_text="Could you provide more details about the secret?",
                signal_summary="nRF52840 benchmark protocol discussion.",
                tags=["benchmark"],
                relevance=90,
                technical=90,
                relationship=80,
                evidence_seqs=[100],
                recent_hour_sends=0,
                room_cooldown_ok=True,
                agent_cooldown_ok=True,
            )
            self.assertFalse(decision.allowed)
            self.assertIn("secret", decision.reason)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
