import sqlite3
import unittest

from job_attest_watcher import ensure_attest_schema, scan_attest_notifications


class JobAttestWatcherTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        ensure_attest_schema(self.con)
        self.job_id = "kabcdef0123"
        self.digest = "digest"
        self.issuer = "did:key:z6MkIssuer"
        self.worker = "did:key:z6MkWorker"
        self.con.execute(
            """
            INSERT INTO job_claim_trials(
              room,job_id,content_hash,job_seq,issuer_did,job_type,refined_at,
              refined_relevance,refined_fit,refined_confidence,prepared_at,
              prepare_expires_at,approved_at,permit_expires_at,consumed_at,
              sender_did,claim_text_hash,status,sent_seq,detail
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "kibble", self.job_id, self.digest, 100, self.issuer, "explain",
                "2026-09-12T00:00:00+00:00", 80, 90, 95,
                1.0, 2.0, 1.1, 2.1, 1.2, self.worker,
                "claimhash", "SENT", 150, "",
            ),
        )
        self.con.execute(
            """
            INSERT INTO job_delivery_trials(
              room,job_id,content_hash,claim_seq,claim_sender_did,
              quality_reviewed_at,quality_decision,quality_confidence,
              answer_hash,prepared_at,prepare_expires_at,approved_at,
              permit_expires_at,consumed_at,sender_did,deliver_text_hash,
              status,sent_seq,detail
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "kibble", self.job_id, self.digest, 150, self.worker,
                "2026-09-12T00:01:00+00:00", "REVISED", 100,
                "answerhash", 10.0, 20.0, 11.0, 21.0, 12.0,
                self.worker, "deliverhash", "SENT", 200, "",
            ),
        )
        self.con.commit()
        self.cfg = {
            "job_shadow_room": "kibble",
            "job_ready_ntfy_url": "http://127.0.0.1:2586/topic",
        }

    def tearDown(self):
        self.con.close()

    def test_issuer_attest_after_delivery_notifies_once(self):
        notices = []

        def fetcher(cfg, path, query):
            if int(query.get("since", 0) or 0) >= 250:
                return []
            return [
                {
                    "seq": 250,
                    "from": self.issuer,
                    "text": f"ATTEST v1 | {self.job_id} | useful | rh:abc",
                }
            ]

        def notifier(cfg, job_id, result):
            notices.append((job_id, result))
            return {"state": "PUBLISHED_LOCAL", "detail": "ok"}

        first = scan_attest_notifications(
            self.con, self.cfg, fetcher=fetcher, notifier=notifier
        )
        second = scan_attest_notifications(
            self.con, self.cfg, fetcher=fetcher, notifier=notifier
        )
        self.assertEqual(first["observed"], 1)
        self.assertEqual(first["published"], 1)
        self.assertEqual(second["published"], 0)
        self.assertEqual(notices, [(self.job_id, "useful")])

    def test_foreign_attest_is_ignored(self):
        def fetcher(cfg, path, query):
            return [
                {
                    "seq": 250,
                    "from": "did:key:z6MkOther",
                    "text": f"ATTEST v1 | {self.job_id} | useful | rh:abc",
                }
            ]

        result = scan_attest_notifications(
            self.con,
            self.cfg,
            fetcher=fetcher,
            notifier=lambda *args: self.fail("foreign ATTEST must not notify"),
        )
        self.assertEqual(result["published"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_failed_notification_retries_without_refetching_attest(self):
        calls = {"notify": 0}

        def first_fetch(cfg, path, query):
            return [
                {
                    "seq": 250,
                    "from": self.issuer,
                    "text": f"ATTEST v1 | {self.job_id} | useful | rh:abc",
                }
            ]

        def failing_notifier(cfg, job_id, result):
            calls["notify"] += 1
            return {"state": "FAILED", "detail": "offline"}

        first = scan_attest_notifications(
            self.con, self.cfg, fetcher=first_fetch, notifier=failing_notifier
        )
        self.assertEqual(first["failed"], 1)

        def empty_fetch(cfg, path, query):
            return []

        def success_notifier(cfg, job_id, result):
            calls["notify"] += 1
            return {"state": "PUBLISHED_LOCAL", "detail": "ok"}

        second = scan_attest_notifications(
            self.con, self.cfg, fetcher=empty_fetch, notifier=success_notifier
        )
        self.assertEqual(second["published"], 1)
        self.assertEqual(calls["notify"], 2)


if __name__ == "__main__":
    unittest.main()
