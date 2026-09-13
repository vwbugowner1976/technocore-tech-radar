import unittest
from unittest.mock import patch

from job_answer_fidelity import fidelity_flags
import job_postclaim_pipeline_live as live


JOB = {
    "title": "Recording why an alert threshold set by gut feeling was chosen",
    "body": (
        "Write down what a future maintainer needs in order to keep or reverse the decision "
        "to use an alert threshold set by gut feeling, without re-deriving it. "
        "It fires every night when load dips and nobody looks anymore. "
        "Success: names one constraint worth recording and one alternative that was rejected and why."
    ),
}

BAD = (
    "A future maintainer should record the decision context. "
    "Leading indicator: The alert threshold was set based on historical load data "
    "from the previous quarter, which showed a consistent pattern of dips."
)


class AnswerFidelityTests(unittest.TestCase):
    def test_rejects_observed_bad_delivery(self):
        flags = fidelity_flags(JOB, BAD)
        self.assertTrue(any("template label" in flag for flag in flags))
        self.assertTrue(any("gut-feeling" in flag for flag in flags))
        self.assertTrue(any("previous quarter" in flag for flag in flags))

    def test_live_quality_wrapper_blocks_observed_bad_delivery(self):
        with patch(
            "job_postclaim_pipeline_live.quality_review",
            return_value={"state": "QUALITY_REVIEWED", "answer": BAD},
        ):
            result = live.quality_review_live(
                object(),
                {},
                "k5d24498c45",
                exact_fetcher=lambda cfg, candidate: {"state": "EXACT", "job": JOB},
            )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("answer fidelity guard", result["reason"])

    def test_allows_grounded_constraint_and_rejected_alternative(self):
        answer = (
            "Constraint worth recording: the alert fires every night when load dips and nobody "
            "looks anymore, so the threshold must remain actionable during that known low-load period. "
            "Rejected alternative: keep the existing gut-feeling threshold unchanged; reject it because "
            "the JOB already shows it produces nightly alerts that are ignored."
        )
        self.assertEqual(fidelity_flags(JOB, answer), [])

    def test_allows_historical_data_as_explicit_rejected_alternative(self):
        answer = (
            "Constraint worth recording: avoid the nightly low-load alerts that are already being ignored. "
            "A rejected alternative could be a historical-data-derived threshold; if that was considered, "
            "record the actual reason it was rejected rather than inventing one."
        )
        self.assertEqual(fidelity_flags(JOB, answer), [])


if __name__ == "__main__":
    unittest.main()
