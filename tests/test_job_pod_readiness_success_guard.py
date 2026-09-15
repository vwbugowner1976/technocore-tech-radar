import unittest

from job_execution_quality_gate import deterministic_quality_flags
from job_execution_semantic_repair import _known_repair


class PodReadinessSuccessGuardTests(unittest.TestCase):
    @staticmethod
    def job():
        return {
            "verb": "JOB",
            "job_id": "kabcdef0123",
            "job_type": "coordinate",
            "title": "What is worth recording around a pod that has no readiness probe",
            "body": (
                "Decide what to log or measure around a pod that has no readiness probe "
                "so a later failure can be explained without guessing. Traffic arrives "
                "before the application has finished starting. Success: names one field "
                "worth keeping and one that is noise."
            ),
        }

    def test_unlabeled_list_is_rejected(self):
        flags = deterministic_quality_flags(
            self.job(),
            "pod_start_time, container_started_at, app_log_timestamp",
        )
        self.assertTrue(any("field to keep" in flag for flag in flags))

    def test_known_repair_is_explicit_and_guard_clean(self):
        repair = _known_repair(self.job())
        self.assertIsNotNone(repair)
        answer, critique = repair
        self.assertIn("Keep an app_ready_at field", answer)
        self.assertIn("Noise is", answer)
        self.assertIn("Running or start timestamp", answer)
        self.assertIn("readiness invariant", critique)
        self.assertEqual(deterministic_quality_flags(self.job(), answer), [])

    def test_unrelated_pod_job_has_no_deterministic_repair(self):
        job = {
            "verb": "JOB",
            "job_id": "kabcdef0123",
            "job_type": "explain",
            "title": "Explain pod CPU requests",
            "body": "Explain what a Kubernetes pod CPU request means.",
        }
        self.assertIsNone(_known_repair(job))


if __name__ == "__main__":
    unittest.main()
