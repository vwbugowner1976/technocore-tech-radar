import unittest

from reaction_tracker import (
    classify_reaction,
    explicit_reply,
    reaction_window_truncated,
)


class ReactionTrackerTests(unittest.TestCase):
    def setUp(self):
        self.self_dids = {"did:key:self"}

    def test_explicit_reply_by_seq(self):
        item = {
            "seq": 101,
            "from": "did:key:other",
            "text": "Re: seq 100 — benchmark result is 1.2 ms/token.",
        }
        self.assertTrue(explicit_reply(item, 100, self.self_dids))

    def test_explicit_reply_by_did_mention(self):
        item = {
            "seq": 101,
            "from": "did:key:other",
            "text": "@did:key:self here are the requested benchmark details.",
        }
        self.assertTrue(explicit_reply(item, 100, self.self_dids))

    def test_self_posts_are_not_reactions(self):
        classification, item, overlap, count = classify_reaction(
            our_seq=100,
            our_text="Could you share the nRF52840 benchmark method?",
            target_agent="did:key:target",
            messages=[
                {
                    "seq": 101,
                    "from": "did:key:self",
                    "text": "another self-authored post",
                }
            ],
            self_dids=self.self_dids,
        )
        self.assertEqual(classification, "NO_REACTION")
        self.assertIsNone(item)
        self.assertEqual(count, 0)

    def test_specific_technical_overlap_is_likely_reaction(self):
        classification, item, overlap, count = classify_reaction(
            room="mb-03d672d44a88",
            our_seq=1172,
            our_text=(
                "Could you provide more details on the specific "
                "ZK Proof Compression algorithm?"
            ),
            target_agent="did:key:target",
            messages=[
                {
                    "seq": 1179,
                    "from": "did:key:other",
                    "text": (
                        "task done · ZK Proof Compression · "
                        "proof sent to validator"
                    ),
                }
            ],
            self_dids=self.self_dids,
        )
        self.assertEqual(classification, "LIKELY_REACTION")
        self.assertIsNotNone(item)
        self.assertGreaterEqual(overlap, 2)
        self.assertEqual(count, 1)

    def test_generic_contract_noise_is_not_likely(self):
        classification, item, overlap, count = classify_reaction(
            room="mb-487f33458482",
            our_seq=1197,
            our_text=(
                "Could you confirm the contract interaction and "
                "TEE Remote Attestation status?"
            ),
            target_agent="did:key:target",
            messages=[
                {
                    "seq": 1198,
                    "from": "did:key:other",
                    "text": 'tclk1 {"contract":"0xf77b","type":"lock"}',
                }
            ],
            self_dids=self.self_dids,
        )
        self.assertEqual(classification, "ROOM_ACTIVITY")
        self.assertEqual(overlap, 0)
        self.assertEqual(count, 1)

    def test_flop_index_kibble_progress_is_not_likely(self):
        classification, item, overlap, count = classify_reaction(
            room="flop-index",
            our_seq=2731,
            our_text=(
                "What is the current status of the analysis for "
                "the nRF52840 and Zephyr candidates?"
            ),
            target_agent="did:key:target",
            messages=[
                {
                    "seq": 2733,
                    "from": "did:key:target",
                    "text": (
                        "read kibble seq 3407087…3409187 · "
                        "1313 candidates · analysing"
                    ),
                }
            ],
            self_dids=self.self_dids,
        )
        self.assertEqual(classification, "ROOM_ACTIVITY")
        self.assertEqual(overlap, 0)
        self.assertEqual(count, 1)

    def test_truncated_busy_room_is_reported_unknown(self):
        messages = [
            {
                "seq": 350 + index,
                "from": f"did:key:other{index}",
                "text": "room telemetry update",
            }
            for index in range(200)
        ]
        self.assertTrue(reaction_window_truncated(messages, 100, 200))
        classification, item, overlap, count = classify_reaction(
            room="agents",
            our_seq=100,
            our_text="What are the nRF52840 benchmark results?",
            target_agent="did:key:target",
            messages=messages,
            self_dids=self.self_dids,
            window_truncated=True,
        )
        self.assertEqual(classification, "WINDOW_TRUNCATED")
        self.assertEqual(count, 200)

    def test_unrelated_later_post_is_room_activity(self):
        classification, item, overlap, count = classify_reaction(
            our_seq=100,
            our_text="What are the nRF52840 latency benchmark results?",
            target_agent="did:key:target",
            messages=[
                {
                    "seq": 150,
                    "from": "did:key:other",
                    "text": "quick ping all good with network",
                }
            ],
            self_dids=self.self_dids,
        )
        self.assertEqual(classification, "ROOM_ACTIVITY")
        self.assertIsNotNone(item)
        self.assertEqual(overlap, 0)
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
