import unittest

from collaboration_rank import rank_collaboration


class CollaborationRankTests(unittest.TestCase):
    def setUp(self):
        self.self_dids = {"did:key:self"}

    def test_direct_reply_scores_target_and_responder(self):
        rows = [
            {
                "detail": "seq=100",
                "room": "agents",
                "target_agent": "did:key:target",
                "text": "Could you share the nRF52840 benchmark?",
            }
        ]

        def fetcher(room, seq, limit):
            return [
                {
                    "seq": 101,
                    "from": "did:key:target",
                    "text": "Re: seq 100 — nRF52840 benchmark is 1.2 ms/token.",
                }
            ]

        responders, targets, totals = rank_collaboration(
            rows,
            self_dids=self.self_dids,
            message_limit=200,
            fetcher=fetcher,
        )

        self.assertEqual(totals["DIRECT_REPLY"], 1)
        self.assertEqual(responders["did:key:target"].direct, 1)
        self.assertEqual(responders["did:key:target"].target_matches, 1)
        self.assertEqual(targets["did:key:target"].direct, 1)
        self.assertEqual(targets["did:key:target"].score, 100)

    def test_likely_reaction_from_other_agent_does_not_credit_target(self):
        rows = [
            {
                "detail": "seq=200",
                "room": "lab",
                "target_agent": "did:key:target",
                "text": "Please share ZK Proof Compression algorithm details.",
            }
        ]

        def fetcher(room, seq, limit):
            return [
                {
                    "seq": 204,
                    "from": "did:key:responder",
                    "text": "ZK Proof Compression proof validator result.",
                }
            ]

        responders, targets, totals = rank_collaboration(
            rows,
            self_dids=self.self_dids,
            message_limit=200,
            fetcher=fetcher,
        )

        self.assertEqual(totals["LIKELY_REACTION"], 1)
        self.assertEqual(responders["did:key:responder"].likely, 1)
        self.assertEqual(responders["did:key:responder"].target_matches, 0)
        self.assertEqual(targets["did:key:target"].likely, 0)
        self.assertEqual(targets["did:key:target"].observed, 1)
        self.assertEqual(targets["did:key:target"].score, 0)

    def test_partial_window_is_not_counted_as_target_failure(self):
        rows = [
            {
                "detail": "seq=100",
                "room": "busy",
                "target_agent": "did:key:target",
                "text": "What are the inference latency benchmarks?",
            }
        ]

        def fetcher(room, seq, limit):
            return [
                {
                    "seq": 500 + index,
                    "from": f"did:key:other{index}",
                    "text": "room telemetry update",
                }
                for index in range(200)
            ]

        responders, targets, totals = rank_collaboration(
            rows,
            self_dids=self.self_dids,
            message_limit=200,
            fetcher=fetcher,
        )

        self.assertEqual(totals["WINDOW_TRUNCATED"], 1)
        self.assertEqual(responders, {})
        target = targets["did:key:target"]
        self.assertEqual(target.partial, 1)
        self.assertEqual(target.observed, 0)
        self.assertEqual(target.score, 0)

    def test_room_activity_does_not_create_responder(self):
        rows = [
            {
                "detail": "seq=50",
                "room": "agents",
                "target_agent": "did:key:target",
                "text": "Could you share BLE HID timing data?",
            }
        ]

        def fetcher(room, seq, limit):
            return [
                {
                    "seq": 80,
                    "from": "did:key:other",
                    "text": "network is online",
                }
            ]

        responders, targets, totals = rank_collaboration(
            rows,
            self_dids=self.self_dids,
            message_limit=200,
            fetcher=fetcher,
        )

        self.assertEqual(totals["ROOM_ACTIVITY"], 1)
        self.assertEqual(responders, {})
        self.assertEqual(targets["did:key:target"].room_activity, 1)


if __name__ == "__main__":
    unittest.main()
