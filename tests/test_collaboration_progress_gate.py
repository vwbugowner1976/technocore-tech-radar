import unittest

from collaboration_progress_gate import evaluate_collaboration_gate


class CollaborationProgressGateTests(unittest.TestCase):
    def thresholds(self):
        return {
            "verified_sends": 30,
            "reaction_total": 25,
            "observed_windows": 15,
            "direct_replies": 2,
            "qualifying_reactions": 5,
            "shadow_decisions": 10,
            "shadow_resolved": 8,
            "shadow_would_prefer": 3,
            "would_prefer_resolved": 2,
            "opportunity_rate_percent": 60,
        }

    def base_metrics(self):
        return {
            "verified_sends": 35,
            "reaction_total": 32,
            "observed_windows": 22,
            "direct_replies": 3,
            "likely_reactions": 5,
            "qualifying_reactions": 8,
            "shadow_decisions": 15,
            "shadow_same": 9,
            "shadow_would_prefer": 6,
            "shadow_resolved": 12,
            "shadow_unresolved": 3,
            "would_prefer_resolved": 4,
            "would_prefer_actual_no_reply": 3,
            "would_prefer_actual_replied": 1,
        }

    def test_collecting_when_baseline_is_small(self):
        metrics = self.base_metrics()
        metrics["verified_sends"] = 12
        result = evaluate_collaboration_gate(metrics, self.thresholds())
        self.assertEqual(result.state, "COLLECTING")
        self.assertFalse(result.ready_for_controlled_trial)

    def test_no_material_difference_when_shadow_rarely_disagrees(self):
        metrics = self.base_metrics()
        metrics["shadow_would_prefer"] = 1
        result = evaluate_collaboration_gate(metrics, self.thresholds())
        self.assertEqual(result.state, "NO_MATERIAL_DIFFERENCE")
        self.assertFalse(result.ready_for_controlled_trial)

    def test_waits_for_disagreement_outcomes(self):
        metrics = self.base_metrics()
        metrics["would_prefer_resolved"] = 1
        result = evaluate_collaboration_gate(metrics, self.thresholds())
        self.assertEqual(result.state, "WAITING_FOR_DISAGREEMENT_OUTCOMES")
        self.assertFalse(result.ready_for_controlled_trial)

    def test_hold_when_existing_target_often_replies_despite_disagreement(self):
        metrics = self.base_metrics()
        metrics["would_prefer_resolved"] = 4
        metrics["would_prefer_actual_no_reply"] = 1
        metrics["would_prefer_actual_replied"] = 3
        result = evaluate_collaboration_gate(metrics, self.thresholds())
        self.assertEqual(result.state, "HOLD")
        self.assertFalse(result.ready_for_controlled_trial)

    def test_ready_means_controlled_trial_only(self):
        result = evaluate_collaboration_gate(
            self.base_metrics(),
            self.thresholds(),
        )
        self.assertEqual(result.state, "READY_FOR_CONTROLLED_TRIAL")
        self.assertTrue(result.ready_for_controlled_trial)


if __name__ == "__main__":
    unittest.main()
