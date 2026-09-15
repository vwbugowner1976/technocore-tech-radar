import unittest

from job_execution_quality_gate import deterministic_quality_flags
from job_execution_semantic_repair import _known_repair


class PipelineExitSuccessGuardTests(unittest.TestCase):
    @staticmethod
    def job():
        return {
            "verb": "JOB",
            "job_id": "kabcdef0123",
            "job_type": "coordinate",
            "title": "What is worth recording around an exit code discarded in a pipeline",
            "body": (
                "Decide what to log or measure around an exit code discarded in a pipeline "
                "so a later failure can be explained without guessing. The pipeline reports "
                "the last command, not the failed one. Success: names one field worth keeping "
                "and one that is noise."
            ),
        }

    def test_unlabeled_list_is_rejected(self):
        flags = deterministic_quality_flags(
            self.job(),
            "exit_code, command, timestamp, command_args, env_vars",
        )
        self.assertTrue(any("field to keep" in flag for flag in flags))
        self.assertTrue(any("per-stage status" in flag for flag in flags))
        self.assertTrue(any("last-command status" in flag for flag in flags))

    def test_wrong_command_as_noise_is_rejected(self):
        flags = deterministic_quality_flags(
            self.job(),
            "Keep the exit code. Noise is command identity.",
        )
        self.assertTrue(any("command identity itself as noise" in flag for flag in flags))

    def test_explicit_keep_and_noise_answer_passes(self):
        answer = (
            "Keep a per_stage_status field containing each stage/command identity and its exit code; "
            "it identifies the command that actually failed. Noise is the final/last-command exit code "
            "by itself, because it can be 0 even when an earlier stage failed."
        )
        self.assertEqual(deterministic_quality_flags(self.job(), answer), [])

    def test_known_repair_produces_guard_clean_answer(self):
        repair = _known_repair(self.job())
        self.assertIsNotNone(repair)
        answer, critique = repair
        self.assertIn("per_stage_status", answer)
        self.assertIn("Noise is", answer)
        self.assertIn("unlabeled list", critique)
        self.assertEqual(deterministic_quality_flags(self.job(), answer), [])


if __name__ == "__main__":
    unittest.main()
