import unittest

from job_live_revalidator import live_revalidate_job_export_aware
from job_shadow import content_hash, parse_kibble_message


class JobLiveRevalidatorTests(unittest.TestCase):
    def setUp(self):
        self.issuer = "did:key:z6MkIssuerA"
        self.text = "JOB v1 | kabcdef0123 | explain | title | body"
        parsed = parse_kibble_message(self.text)
        self.assertIsNotNone(parsed)
        self.candidate = {
            "room": "kibble",
            "job_id": "kabcdef0123",
            "job_seq": 100,
            "issuer_did": self.issuer,
            "content_hash": content_hash(parsed),
        }

    def export_with_job(self, cfg, room):
        return [
            {"seq": 99, "from": "did:key:z6MkOther", "text": "noise"},
            {"seq": 100, "from": self.issuer, "text": self.text},
            {"seq": 101, "from": "did:key:z6MkOther", "text": "noise"},
        ]

    def test_export_exact_job_and_empty_catchup_confirms_open(self):
        def fetcher(cfg, path, query):
            return {"messages": []}

        result = live_revalidate_job_export_aware(
            {},
            self.candidate,
            fetcher=fetcher,
            export_fetcher=self.export_with_job,
        )
        self.assertEqual(result["state"], "OPEN_CONFIRMED")
        self.assertEqual(result["source"], "export+catchup")

    def test_claim_in_catchup_closes_job(self):
        def fetcher(cfg, path, query):
            return {
                "messages": [
                    {
                        "seq": 102,
                        "from": "did:key:z6MkWorker",
                        "text": "CLAIM v1 | kabcdef0123 | worker",
                    }
                ]
            }

        result = live_revalidate_job_export_aware(
            {},
            self.candidate,
            fetcher=fetcher,
            export_fetcher=self.export_with_job,
        )
        self.assertEqual(result["state"], "NOT_OPEN")
        self.assertEqual(result["lifecycle"], "CLAIMED")

    def test_missing_original_in_export_fails_closed(self):
        def export_fetcher(cfg, room):
            return [{"seq": 500, "from": "did:key:z6MkOther", "text": "noise"}]

        result = live_revalidate_job_export_aware(
            {},
            self.candidate,
            fetcher=lambda cfg, path, query: {"messages": []},
            export_fetcher=export_fetcher,
        )
        self.assertEqual(result["state"], "JOB_NOT_RETAINED")

    def test_gap_after_export_is_inconclusive(self):
        def fetcher(cfg, path, query):
            return {
                "messages": [
                    {"seq": 105, "from": "did:key:z6MkOther", "text": "noise"}
                ]
            }

        result = live_revalidate_job_export_aware(
            {},
            self.candidate,
            fetcher=fetcher,
            export_fetcher=self.export_with_job,
        )
        self.assertEqual(result["state"], "INCONCLUSIVE_GAP")


if __name__ == "__main__":
    unittest.main()
