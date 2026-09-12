import unittest
from unittest.mock import patch

import job_action


JOB = "k632d57232a"


class JobActionTests(unittest.TestCase):
    def test_invalid_job_id_stops_before_state_lookup(self):
        with patch("job_action._latest_auto") as latest:
            state = job_action.run_action(None, {}, "k123;bad")
        self.assertEqual(state, "INVALID_JOB_ID")
        latest.assert_not_called()

    def test_untracked_job_stops(self):
        with patch("job_action._latest_auto", return_value=None):
            state = job_action.run_action(None, {}, JOB)
        self.assertEqual(state, "UNTRACKED")

    def test_claim_to_pipeline_to_delivery_is_one_routed_flow(self):
        tracked_before = {"pipeline_state": "WAITING_FOR_HUMAN_CLAIM", "detail": ""}
        tracked_after = {"pipeline_state": "WAITING_POSTCLAIM", "detail": ""}
        with (
            patch("job_action._latest_auto", side_effect=[tracked_before, tracked_after]),
            patch("job_action._latest_status", side_effect=["PREPARED", ""]),
            patch("job_action._claim_flow", return_value="SENT") as claim,
            patch("job_action._run_local_pipeline", return_value="DELIVERY_READY") as pipeline,
            patch("job_action._delivery_flow", return_value="SENT") as delivery,
        ):
            state = job_action.run_action(None, {}, JOB)
        self.assertEqual(state, "SENT")
        claim.assert_called_once()
        pipeline.assert_called_once()
        delivery.assert_called_once()

    def test_delivery_ready_resumes_without_claim_or_pipeline(self):
        tracked = {"pipeline_state": "DELIVERY_READY", "detail": ""}
        with (
            patch("job_action._latest_auto", return_value=tracked),
            patch("job_action._latest_status", side_effect=["SENT", "PREPARED"]),
            patch("job_action._claim_flow") as claim,
            patch("job_action._run_local_pipeline") as pipeline,
            patch("job_action._delivery_flow", return_value="SENT") as delivery,
        ):
            state = job_action.run_action(None, {}, JOB)
        self.assertEqual(state, "SENT")
        claim.assert_not_called()
        pipeline.assert_not_called()
        delivery.assert_called_once()

    def test_blocked_pipeline_never_enters_human_send_flows(self):
        tracked = {"pipeline_state": "BLOCKED", "detail": "quality gate failed"}
        with (
            patch("job_action._latest_auto", return_value=tracked),
            patch("job_action._latest_status", side_effect=["SENT", ""]),
            patch("job_action._claim_flow") as claim,
            patch("job_action._delivery_flow") as delivery,
        ):
            state = job_action.run_action(None, {}, JOB)
        self.assertEqual(state, "BLOCKED")
        claim.assert_not_called()
        delivery.assert_not_called()

    def test_grounding_only_success_block_retries_local_pipeline_then_delivery(self):
        detail = (
            "success: generic Success gate blocked: structured exact-quote evidence "
            "does not satisfy frozen contract; requirements=[] grounding=['G1']; "
            "semantic fallback unavailable: JOB does not match a supported deterministic semantic repair"
        )
        tracked = {
            "pipeline_state": "BLOCKED",
            "detail": detail,
            "room": "kibble",
            "job_id": JOB,
            "content_hash": "digest",
        }
        with (
            patch("job_action._latest_auto", return_value=tracked),
            patch("job_action._latest_status", side_effect=["SENT", ""]),
            patch("job_action._retry_grounding_only_local_block", return_value="DELIVERY_READY") as retry,
            patch("job_action._claim_flow") as claim,
            patch("job_action._delivery_flow", return_value="SENT") as delivery,
        ):
            state = job_action.run_action(None, {}, JOB)
        self.assertEqual(state, "SENT")
        retry.assert_called_once()
        claim.assert_not_called()
        delivery.assert_called_once()

    def test_confirmation_requires_job_id_and_distinct_send_phrase(self):
        with patch("builtins.input", side_effect=[JOB, "SEND CLAIM"]):
            self.assertTrue(job_action._confirm("CLAIM", JOB, "SEND CLAIM"))
        with patch("builtins.input", side_effect=[JOB, "SEND"]):
            self.assertFalse(job_action._confirm("CLAIM", JOB, "SEND CLAIM"))
        with patch("builtins.input", side_effect=["wrong"]):
            self.assertFalse(job_action._confirm("DELIVER", JOB, "SEND DELIVER"))


if __name__ == "__main__":
    unittest.main()
