import unittest

from reaction_tracker import classify_reaction, explicit_reply


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

    def test_immediate_related_post_is_likely_reaction(self):
        classification, item, overlap, count = classify_reaction(
            our_seq=1229,
            our_text=(
                "Could you provide more details on the tclk1 message "
                "and lock contract?"
            ),
            target_agent="did:key:target",
            messages=[
                {
                    "seq": 1230,
                    "from": "did:key:other",
                    "text": (
                        "contract 0xb0ec · ZK Proof Compression · "
                        "escrow locked"
                    ),
                }
            ],
            self_dids=self.self_dids,
        )
        self.assertEqual(classification, "LIKELY_REACTION")
        self.assertIsNotNone(item)
        self.assertGreaterEqual(overlap, 1)
        self.assertEqual(count, 1)

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
