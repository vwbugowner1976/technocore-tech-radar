import unittest

from technoscout_notify import notify_job_attest


class TechnoScoutAttestNotifyTests(unittest.TestCase):
    def test_reuses_ready_topic(self):
        captured = {}

        class Response:
            status = 200

            def close(self):
                captured["closed"] = True

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = request.data.decode("utf-8")
            captured["tags"] = request.get_header("Tags")
            return Response()

        result = notify_job_attest(
            {
                "job_ready_ntfy_url": "http://127.0.0.1:2586/technoscout-ready",
                "job_ready_ntfy_title": "TechnoScout",
            },
            "kabcdef0123",
            "useful",
            opener=opener,
        )
        self.assertEqual(result["state"], "PUBLISHED_LOCAL")
        self.assertEqual(captured["url"], "http://127.0.0.1:2586/technoscout-ready")
        self.assertEqual(captured["body"], "ATTEST job=kabcdef0123 result=useful")
        self.assertEqual(captured["tags"], "white_check_mark")
        self.assertTrue(captured["closed"])

    def test_result_is_sanitized(self):
        captured = {}

        class Response:
            status = 200

            def close(self):
                pass

        def opener(request, timeout):
            captured["body"] = request.data.decode("utf-8")
            return Response()

        notify_job_attest(
            {"job_ready_ntfy_url": "http://127.0.0.1:2586/topic"},
            "kabcdef0123",
            "Useful result with spaces",
            opener=opener,
        )
        self.assertEqual(
            captured["body"],
            "ATTEST job=kabcdef0123 result=useful-result-with-spaces",
        )


if __name__ == "__main__":
    unittest.main()
