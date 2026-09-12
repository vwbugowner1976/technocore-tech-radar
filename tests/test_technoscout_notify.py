import unittest

from technoscout_notify import (
    notify_claim_ready,
    notify_delivery_ready,
    notify_ready_candidate,
)


class TechnoScoutNotifyTests(unittest.TestCase):
    def test_disabled_without_url(self):
        result = notify_ready_candidate({}, "kabcdef0123")
        self.assertEqual(result["state"], "DISABLED")

    def test_rejects_invalid_job_id_without_network(self):
        calls = []

        def opener(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("network must not be called")

        result = notify_ready_candidate(
            {"job_ready_ntfy_url": "http://127.0.0.1:2586/ready"},
            "not-a-job",
            opener=opener,
        )
        self.assertEqual(result["state"], "SKIPPED")
        self.assertEqual(calls, [])

    def test_rejects_credentials_in_url(self):
        result = notify_ready_candidate(
            {"job_ready_ntfy_url": "http://user:secret@127.0.0.1:2586/ready"},
            "kabcdef0123",
        )
        self.assertEqual(result["state"], "FAILED")
        self.assertIn("credentials", result["detail"])

    @staticmethod
    def _capture_notice(notifier, job_id="kabcdef0123"):
        captured = {}

        class Response:
            status = 200

            def close(self):
                captured["closed"] = True

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = request.data.decode("utf-8")
            captured["title"] = request.get_header("Title")
            captured["timeout"] = timeout
            return Response()

        result = notifier(
            {
                "job_ready_ntfy_url": "http://127.0.0.1:2586/technoscout-ready",
                "job_ready_ntfy_title": "TechnoScout",
            },
            job_id,
            opener=opener,
        )
        return result, captured

    def test_publishes_only_job_id(self):
        result, captured = self._capture_notice(notify_ready_candidate)
        self.assertEqual(result["state"], "PUBLISHED_LOCAL")
        self.assertEqual(captured["body"], "READY candidate=kabcdef0123")
        self.assertEqual(captured["title"], "TechnoScout")
        self.assertTrue(captured["closed"])

    def test_claim_ready_contains_copy_paste_workflow_with_two_human_gates(self):
        result, captured = self._capture_notice(notify_claim_ready, "k632d57232a")
        self.assertEqual(result["state"], "PUBLISHED_LOCAL")
        lines = captured["body"].splitlines()
        self.assertEqual(lines[0], "# CLAIM_READY job=k632d57232a")
        self.assertTrue(lines[1].startswith("# "))
        self.assertEqual(len(lines), 3)
        command = lines[2]
        self.assertIn('cd "$HOME/technocore-tech-radar"', command)
        self.assertIn("job_claim_trial.py prepare k632d57232a", command)
        self.assertIn("job_claim_trial.py approve k632d57232a", command)
        self.assertIn("job_claim_trial.py send k632d57232a", command)
        self.assertIn('read -r TS_JOB </dev/tty', command)
        self.assertIn('read -r TS_SEND </dev/tty', command)
        self.assertIn('[ "$TS_JOB" = "k632d57232a" ]', command)
        self.assertIn('[ "$TS_SEND" = "SEND" ]', command)
        self.assertNotIn("curl", command)
        self.assertNotIn("http://", command)
        self.assertNotIn("https://", command)

    def test_delivery_ready_contains_copy_paste_workflow_with_two_human_gates(self):
        result, captured = self._capture_notice(notify_delivery_ready, "k632d57232a")
        self.assertEqual(result["state"], "PUBLISHED_LOCAL")
        lines = captured["body"].splitlines()
        self.assertEqual(lines[0], "# DELIVERY_READY job=k632d57232a")
        self.assertEqual(len(lines), 3)
        command = lines[2]
        self.assertIn("job_delivery_trial.py prepare k632d57232a", command)
        self.assertIn("job_delivery_trial.py approve k632d57232a", command)
        self.assertIn("job_delivery_trial.py send k632d57232a", command)
        self.assertIn('[ "$TS_JOB" = "k632d57232a" ]', command)
        self.assertIn('[ "$TS_SEND" = "SEND" ]', command)

    def test_action_notifications_reject_invalid_job_id_before_network(self):
        calls = []

        def opener(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("network must not be called")

        cfg = {"job_ready_ntfy_url": "http://127.0.0.1:2586/ready"}
        for notifier in (notify_claim_ready, notify_delivery_ready):
            result = notifier(cfg, "k123;rm-rf", opener=opener)
            self.assertEqual(result["state"], "SKIPPED")
        self.assertEqual(calls, [])

    def test_network_failure_is_fail_soft(self):
        def opener(*args, **kwargs):
            raise OSError("offline")

        result = notify_ready_candidate(
            {"job_ready_ntfy_url": "http://127.0.0.1:2586/ready"},
            "kabcdef0123",
            opener=opener,
        )
        self.assertEqual(result["state"], "FAILED")
        self.assertIn("offline", result["detail"])


if __name__ == "__main__":
    unittest.main()
