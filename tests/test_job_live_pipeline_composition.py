import unittest
from unittest.mock import patch

import job_auto_orchestrator
import job_postclaim_pipeline_live
from job_gpu_semantic_repair import repair_gpu_shared_or_known
from job_success_named_proof import validate_success_criterion as validate_success_named


class JobLivePipelineCompositionTests(unittest.TestCase):
    def test_live_wrapper_injects_proven_components(self):
        seen = {}

        def core(con, cfg, job_id, **kwargs):
            seen.update(kwargs)
            return {"state": "READY_FOR_HUMAN_DELIVERY"}

        with patch("job_postclaim_pipeline_live._run_core", side_effect=core):
            result = job_postclaim_pipeline_live.run_postclaim_pipeline(
                object(), {}, "kabcdef0123", room="kibble"
            )

        self.assertEqual(result["state"], "READY_FOR_HUMAN_DELIVERY")
        self.assertIs(seen["success_runner"], validate_success_named)
        self.assertIs(seen["semantic_repair_runner"], repair_gpu_shared_or_known)

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
