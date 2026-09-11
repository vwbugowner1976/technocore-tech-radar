import unittest
from types import SimpleNamespace

from technoscout_notify import notify_ready_candidate


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

    def test_publishes_only_job_id(self):
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

        result = notify_ready_candidate(
            {
                "job_ready_ntfy_url": "http://127.0.0.1:2586/technoscout-ready",
                "job_ready_ntfy_title": "TechnoScout",
            },
            "kabcdef0123",
            opener=opener,
        )
        self.assertEqual(result["state"], "PUBLISHED_LOCAL")
        self.assertEqual(captured["body"], "READY candidate=kabcdef0123")
        self.assertEqual(captured["title"], "TechnoScout")
        self.assertTrue(captured["closed"])

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
