import unittest
from unittest.mock import patch

import job_auto_orchestrator
import job_postclaim_pipeline_live
from job_gpu_semantic_repair import repair_gpu_shared_or_known


class JobLivePipelineCompositionTests(unittest.TestCase):
    def test_live_wrapper_injects_proven_components_and_timeout_floors(self):
        seen = {}

        def core(con, cfg, job_id, **kwargs):
            seen["cfg"] = dict(cfg)
            seen.update(kwargs)
            return {"state": "READY_FOR_HUMAN_DELIVERY"}

        with patch("job_postclaim_pipeline_live._run_core", side_effect=core):
            result = job_postclaim_pipeline_live.run_postclaim_pipeline(
                object(), {}, "kabcdef0123", room="kibble"
            )

        self.assertEqual(result["state"], "READY_FOR_HUMAN_DELIVERY")
        self.assertIs(
            seen["quality_runner"],
            job_postclaim_pipeline_live.quality_review_live,
        )
        self.assertIs(
            seen["success_runner"],
            job_postclaim_pipeline_live.validate_success_live,
        )
        self.assertIs(seen["semantic_repair_runner"], repair_gpu_shared_or_known)
        self.assertEqual(seen["cfg"]["job_execution_quality_timeout_seconds"], 180.0)
        self.assertEqual(seen["cfg"]["job_success_verify_timeout_seconds"], 180.0)

    def test_explicit_larger_timeout_is_not_reduced(self):
        seen = {}

        def core(con, cfg, job_id, **kwargs):
            seen.update(cfg)
            return {"state": "READY_FOR_HUMAN_DELIVERY"}

        with patch("job_postclaim_pipeline_live._run_core", side_effect=core):
            job_postclaim_pipeline_live.run_postclaim_pipeline(
                object(),
                {"job_execution_quality_timeout_seconds": 300},
                "kabcdef0123",
            )

        self.assertEqual(seen["job_execution_quality_timeout_seconds"], 300)

    def test_timeout_retries_local_pipeline_once_with_extended_deadlines(self):
        calls = []

        def core(con, cfg, job_id, **kwargs):
            calls.append(dict(cfg))
            if len(calls) == 1:
                raise TimeoutError("fixture timeout")
            return {"state": "READY_FOR_HUMAN_DELIVERY"}

        with patch("job_postclaim_pipeline_live._run_core", side_effect=core):
            result = job_postclaim_pipeline_live.run_postclaim_pipeline(
                object(), {}, "kabcdef0123"
            )

        self.assertEqual(result["state"], "READY_FOR_HUMAN_DELIVERY")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["job_execution_quality_timeout_seconds"], 180.0)
        self.assertEqual(calls[1]["job_execution_quality_timeout_seconds"], 240.0)
        self.assertEqual(calls[1]["job_success_verify_timeout_seconds"], 240.0)

    def test_second_timeout_is_not_retried_again(self):
        calls = {"n": 0}

        def core(*args, **kwargs):
            calls["n"] += 1
            raise TimeoutError("still slow")

        with patch("job_postclaim_pipeline_live._run_core", side_effect=core):
            with self.assertRaises(TimeoutError):
                job_postclaim_pipeline_live.run_postclaim_pipeline(
                    object(), {}, "kabcdef0123"
                )

        self.assertEqual(calls["n"], 2)

    def test_auto_orchestrator_defaults_to_live_wrapper(self):
        live = job_postclaim_pipeline_live.run_postclaim_pipeline
        self.assertIs(
            job_auto_orchestrator.process_sent_claims.__kwdefaults__["pipeline_runner"],
            live,
        )
        self.assertIs(
            job_auto_orchestrator.run_once.__kwdefaults__["pipeline_runner"],
            live,
        )


if __name__ == "__main__":
    unittest.main()
