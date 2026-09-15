import unittest
from unittest.mock import patch

from job_shadow_runner import latest_then_since_fetcher


class JobShadowRunnerTests(unittest.TestCase):
    def test_first_read_omits_since_to_get_latest_window(self):
        with patch("job_shadow_runner.technocore_json") as fetch:
            fetch.return_value = {"messages": []}
            latest_then_since_fetcher(
                {},
                "/r/kibble",
                {"format": "json", "since": 0, "limit": 200},
            )
            fetch.assert_called_once_with(
                {},
                "/r/kibble",
                {"format": "json", "limit": 200},
            )

    def test_later_read_keeps_positive_since_cursor(self):
        with patch("job_shadow_runner.technocore_json") as fetch:
            fetch.return_value = {"messages": []}
            latest_then_since_fetcher(
                {},
                "/r/kibble",
                {"format": "json", "since": 123, "limit": 200},
            )
            fetch.assert_called_once_with(
                {},
                "/r/kibble",
                {"format": "json", "since": 123, "limit": 200},
            )


if __name__ == "__main__":
    unittest.main()
