import base64
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from technoscout.common import (
    clamp_score,
    event_room,
    normalize_evidence_source,
    parse_json_object,
    safe_room,
)
from technoscout.autonomy import evaluate_autonomy
from technoscout.llm_backend import ManagedMLXBackend
from technoscout.sender import (
    ApprovedDraftSender,
    SigningIdentity,
    diagnose_signing_material,
    sweep_text,
)
from technoscout.db import (
    agent_context,
    agent_relationship,
    connect,
    create_reply_draft,
    get_meta,
    get_reply_draft,
    get_translation,
    record_agent_encounter,
    record_agent_signal,
    pending_reply_drafts,
    reply_draft_counts,
    create_send_permit,
    consume_send_permit,
    expire_send_permits,
    revoke_send_permits,
    send_permits_for_draft,
    reserve_send_nonce,
    create_send_attempt,
    finish_send_attempt,
    get_last_send_attempt,
    set_draft_status,
    review_reply_draft,
    set_meta,
    store_translation,
    top_agents,
)


class CommonTests(unittest.TestCase):
    def test_room_validation(self):
        self.assertEqual(safe_room("d-aircooled-vw-lab"), "d-aircooled-vw-lab")
        self.assertEqual(safe_room("mb-p-abc123"), "mb-p-abc123")
        self.assertIsNone(safe_room("../etc/passwd"))
        self.assertIsNone(safe_room("https://example.com"))

    def test_event_room(self):
        self.assertEqual(event_room({"room": "new-lab"}), "new-lab")
        self.assertEqual(event_room({"text": "created embedded-lab"}), "embedded-lab")

    def test_json_parser(self):
        value = parse_json_object('{"relevance": 90}')
        self.assertEqual(value["relevance"], 90)

    def test_json_parser_fenced_with_trailing_text(self):
        sample = """Here is the result:
```json
{"relevance": 88, "action": "SAVE"}
```
<|im_end|>"""
        value = parse_json_object(sample)
        self.assertEqual(value["relevance"], 88)
        self.assertEqual(value["action"], "SAVE")

    def test_evidence_source_normalization(self):
        self.assertEqual(normalize_evidence_source("topic"), "topic")
        self.assertEqual(normalize_evidence_source("MESSAGES"), "messages")
        self.assertEqual(normalize_evidence_source("topic|messages|none"), "none")
        self.assertEqual(normalize_evidence_source(None), "none")

    def test_single_line_sweep(self):
        self.assertEqual(sweep_text("  a\nb\u200bc  "), "a b c")
        self.assertEqual(sweep_text("hello"), "hello")
        with self.assertRaises(ValueError):
            sweep_text("\n\u200b")

    def test_score_clamp(self):
        self.assertEqual(clamp_score(101), 100)
        self.assertEqual(clamp_score(-2), 0)
        self.assertEqual(clamp_score("42"), 42)


class AutonomyPolicyTests(unittest.TestCase):
    def _cfg(self):
        return {
            "autonomy_mode": "limited",
            "autonomy_min_relevance": 75,
            "autonomy_min_technical": 75,
            "autonomy_min_relationship": 30,
            "autonomy_max_sends_per_hour": 3,
            "autonomy_max_draft_chars": 600,
            "autonomy_blocked_room_terms": [
                "governance", "tclk", "offer", "wallet", "payment"
            ],
            "autonomy_blocked_text_terms": [
                "wallet", "payment", "refund", "private key", "password"
            ],
        }

    def test_safe_technical_question_is_allowed(self):
        decision = evaluate_autonomy(
            self._cfg(),
            room="inference-agents",
            draft_text="Could you share the benchmark methodology and p95 latency results?",
            signal_summary="Agent reports a new inference latency benchmark.",
            tags=["inference", "benchmark"],
            relevance=88,
            technical=92,
            relationship=45,
            evidence_seqs=[123],
            recent_hour_sends=0,
            room_cooldown_ok=True,
            agent_cooldown_ok=True,
        )
        self.assertTrue(decision.allowed)

    def test_governance_room_is_blocked(self):
        decision = evaluate_autonomy(
            self._cfg(),
            room="flop-governance",
            draft_text="Could you clarify the benchmark requirement?",
            signal_summary="Technical proposal.",
            tags=["benchmark"],
            relevance=90,
            technical=90,
            relationship=60,
            evidence_seqs=[1],
            recent_hour_sends=0,
            room_cooldown_ok=True,
            agent_cooldown_ok=True,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("blocked room", decision.reason)

    def test_transaction_like_content_is_blocked(self):
        decision = evaluate_autonomy(
            self._cfg(),
            room="pin",
            draft_text="Could you confirm the transaction receipt and artifact ID?",
            signal_summary="A transaction receipt was reported.",
            tags=["artifact", "receipt"],
            relevance=90,
            technical=90,
            relationship=60,
            evidence_seqs=[4],
            recent_hour_sends=0,
            room_cooldown_ok=True,
            agent_cooldown_ok=True,
        )
        self.assertFalse(decision.allowed)

    def test_japanese_autonomous_draft_is_blocked(self):
        decision = evaluate_autonomy(
            self._cfg(),
            room="inference-agents",
            draft_text="ベンチマーク条件を共有できますか？",
            signal_summary="Technical discussion.",
            tags=["benchmark"],
            relevance=90,
            technical=90,
            relationship=60,
            evidence_seqs=[3],
            recent_hour_sends=0,
            room_cooldown_ok=True,
            agent_cooldown_ok=True,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("English", decision.reason)

    def test_url_and_missing_evidence_are_blocked(self):
        cfg = self._cfg()
        no_evidence = evaluate_autonomy(
            cfg,
            room="agents",
            draft_text="Could you share more detail?",
            signal_summary="Technical discussion.",
            tags=["agent"],
            relevance=90,
            technical=90,
            relationship=60,
            evidence_seqs=[],
            recent_hour_sends=0,
            room_cooldown_ok=True,
            agent_cooldown_ok=True,
        )
        self.assertFalse(no_evidence.allowed)

        with_url = evaluate_autonomy(
            cfg,
            room="agents",
            draft_text="Could you check https://example.com and comment?",
            signal_summary="Technical discussion.",
            tags=["agent"],
            relevance=90,
            technical=90,
            relationship=60,
            evidence_seqs=[2],
            recent_hour_sends=0,
            room_cooldown_ok=True,
            agent_cooldown_ok=True,
        )
        self.assertFalse(with_url.allowed)
        self.assertIn("URL", with_url.reason)


class DatabaseTests(unittest.TestCase):
    def test_translation_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            store_translation(
                con,
                "draft_text",
                "42",
                "ja",
                "abc123",
                "ベンチマークの詳細を共有できますか？",
                "2026-09-09T00:00:00+00:00",
            )
            con.commit()
            self.assertEqual(
                get_translation(con, "draft_text", "42", "ja", "abc123"),
                "ベンチマークの詳細を共有できますか？",
            )
            self.assertIsNone(
                get_translation(con, "draft_text", "42", "ja", "changed")
            )
            con.close()

    def test_meta_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            set_meta(con, "cursor", 123)
            con.commit()
            self.assertEqual(get_meta(con, "cursor"), "123")
            con.close()

    def test_agent_memory_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            record_agent_encounter(con, "did:key:test-agent", "embedded-lab", "2026-09-08T10:00:00+00:00", 2)
            record_agent_signal(
                con,
                ["did:key:test-agent"],
                "embedded-lab",
                "2026-09-08T10:01:00+00:00",
                ["zmk", "nrf52840"],
                "Useful embedded result",
                True,
            )
            con.commit()
            rows = top_agents(con, 5)
            self.assertEqual(rows[0]["agent_id"], "did:key:test-agent")
            self.assertEqual(rows[0]["encounter_count"], 2)
            self.assertEqual(rows[0]["useful_signal_count"], 1)
            self.assertEqual(rows[0]["followup_count"], 1)
            self.assertIn("zmk", rows[0]["topics"])
            context = agent_context(con, ["did:key:test-agent"])
            self.assertEqual(context[0]["signals"], 1)
            self.assertIn("nrf52840", context[0]["topics"])
            relationship = agent_relationship(con, "did:key:test-agent")
            self.assertGreaterEqual(relationship["score"], 30)
            con.close()

    def test_relationship_and_draft_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            record_agent_encounter(
                con,
                "did:key:draft-agent",
                "agents",
                "2026-09-08T11:00:00+00:00",
                3,
            )
            record_agent_signal(
                con,
                ["did:key:draft-agent"],
                "agents",
                "2026-09-08T11:01:00+00:00",
                ["security", "signing"],
                "Useful signing discussion",
                True,
            )
            relationship = agent_relationship(con, "did:key:draft-agent")
            created = create_reply_draft(
                con,
                "2026-09-08T11:02:00+00:00",
                "agents",
                42,
                "did:key:draft-agent",
                relationship["score"],
                "Ask for implementation detail",
                "Did you also test the canonicalization step across implementations?",
            )
            self.assertTrue(created)
            self.assertFalse(create_reply_draft(
                con,
                "2026-09-08T11:03:00+00:00",
                "agents",
                42,
                "did:key:draft-agent",
                relationship["score"],
                "duplicate",
                "duplicate",
            ))
            con.commit()
            drafts = pending_reply_drafts(con, 5)
            self.assertEqual(len(drafts), 1)
            self.assertEqual(drafts[0]["room"], "agents")
            self.assertEqual(drafts[0]["status"], "pending")
            con.close()

    def test_nonce_reservation_and_send_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            first = reserve_send_nonce(
                con,
                "did:key:test",
                "agents",
                server_nonce=10,
                floor=100,
            )
            second = reserve_send_nonce(
                con,
                "did:key:test",
                "agents",
                server_nonce=50,
                floor=20,
            )
            third = reserve_send_nonce(
                con,
                "did:key:test",
                "agents",
                server_nonce=500,
                floor=20,
            )
            self.assertEqual((first, second, third), (100, 101, 501))

            self.assertTrue(create_reply_draft(
                con,
                "2026-09-09T01:00:00+00:00",
                "agents",
                100,
                "did:key:target",
                40,
                "audit test",
                "hello",
            ))
            draft_id = int(pending_reply_drafts(con, 1)[0]["id"])
            self.assertTrue(review_reply_draft(con, draft_id, "approved"))
            attempt_id = create_send_attempt(
                con,
                draft_id,
                "2026-09-09T01:01:00Z",
                "did:key:sender",
                "agents",
                501,
                "A" * 86,
                "hello",
            )
            finish_send_attempt(
                con,
                attempt_id,
                "sent",
                200,
                "seq=123",
            )
            set_draft_status(con, draft_id, "sent")
            con.commit()

            attempt = get_last_send_attempt(con, draft_id)
            self.assertEqual(attempt["status"], "sent")
            self.assertEqual(attempt["nonce"], "501")
            self.assertEqual(get_reply_draft(con, draft_id)["status"], "sent")
            self.assertEqual(reply_draft_counts(con)["sent"], 1)
            con.close()

    def test_one_time_send_permit_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            token_hash = "a" * 64
            permit_id = create_send_permit(
                con,
                draft_id=2,
                created_at=100.0,
                expires_at=200.0,
                token_hash=token_hash,
                did="did:key:test",
                room="inference-agents",
                text_hash="b" * 64,
            )
            con.commit()
            self.assertGreater(permit_id, 0)
            rows = send_permits_for_draft(con, 2)
            self.assertEqual(rows[0]["status"], "armed")

            self.assertFalse(consume_send_permit(
                con,
                draft_id=2,
                token_hash="c" * 64,
                did="did:key:test",
                room="inference-agents",
                text_hash="b" * 64,
                now=150.0,
            ))
            self.assertTrue(consume_send_permit(
                con,
                draft_id=2,
                token_hash=token_hash,
                did="did:key:test",
                room="inference-agents",
                text_hash="b" * 64,
                now=150.0,
            ))
            self.assertFalse(consume_send_permit(
                con,
                draft_id=2,
                token_hash=token_hash,
                did="did:key:test",
                room="inference-agents",
                text_hash="b" * 64,
                now=151.0,
            ))
            con.commit()
            self.assertEqual(send_permits_for_draft(con, 2)[0]["status"], "consumed")

            create_send_permit(
                con,
                draft_id=3,
                created_at=100.0,
                expires_at=120.0,
                token_hash="d" * 64,
                did="did:key:test",
                room="agents",
                text_hash="e" * 64,
            )
            self.assertEqual(expire_send_permits(con, 121.0), 1)
            con.commit()
            self.assertEqual(send_permits_for_draft(con, 3)[0]["status"], "expired")
            con.close()

    def test_arming_supersedes_previous_permit(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            create_send_permit(
                con, 7, 100.0, 200.0, "1" * 64,
                "did:key:test", "agents", "2" * 64,
            )
            create_send_permit(
                con, 7, 110.0, 210.0, "3" * 64,
                "did:key:test", "agents", "2" * 64,
            )
            con.commit()
            rows = send_permits_for_draft(con, 7)
            self.assertEqual(rows[0]["status"], "armed")
            self.assertEqual(rows[1]["status"], "superseded")
            con.close()

    def test_send_permit_revocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            create_send_permit(
                con,
                draft_id=9,
                created_at=100.0,
                expires_at=200.0,
                token_hash="f" * 64,
                did="did:key:test",
                room="agents",
                text_hash="e" * 64,
            )
            self.assertEqual(revoke_send_permits(con, 9), 1)
            con.commit()
            row = send_permits_for_draft(con, 9)[0]
            self.assertEqual(row["status"], "revoked")
            self.assertFalse(consume_send_permit(
                con,
                draft_id=9,
                token_hash="f" * 64,
                did="did:key:test",
                room="agents",
                text_hash="e" * 64,
                now=150.0,
            ))
            con.close()

    def test_draft_review_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "test.db")
            self.assertTrue(create_reply_draft(
                con,
                "2026-09-09T00:00:00+00:00",
                "agents",
                99,
                "did:key:review-agent",
                40,
                "Useful follow-up",
                "Could you share how you validated the implementation?",
            ))
            con.commit()
            draft = pending_reply_drafts(con, 5)[0]
            draft_id = int(draft["id"])
            self.assertTrue(review_reply_draft(con, draft_id, "approved"))
            con.commit()
            reviewed = get_reply_draft(con, draft_id)
            self.assertEqual(reviewed["status"], "approved")
            self.assertFalse(review_reply_draft(con, draft_id, "rejected"))
            counts = reply_draft_counts(con)
            self.assertEqual(counts["approved"], 1)
            self.assertEqual(counts["pending"], 0)
            with self.assertRaises(ValueError):
                review_reply_draft(con, draft_id, "sent")
            con.close()


class SenderCryptoTests(unittest.TestCase):
    def test_signing_seed_private_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("SIGN_SEED=" + ("01" * 32) + "\n", encoding="utf-8")
            env_file.chmod(0o600)
            old = os.environ.pop("SIGN_SEED", None)
            try:
                identity = SigningIdentity.from_env("SIGN_SEED", str(env_file))
                self.assertTrue(identity.did.startswith("did:key:z6Mk"))
                env_file.chmod(0o644)
                with self.assertRaises(RuntimeError):
                    SigningIdentity.from_env("SIGN_SEED", str(env_file))
            finally:
                if old is not None:
                    os.environ["SIGN_SEED"] = old

    def test_diagnose_base64_legacy_shape(self):
        raw = bytes(range(48))
        encoded = base64.b64encode(raw).decode("ascii")
        self.assertEqual(len(encoded), 64)
        result = diagnose_signing_material(encoded)
        self.assertEqual(result["chars"], 64)
        self.assertTrue(result["base64_standard"])
        self.assertIn(48, result["decoded_lengths"])
        self.assertFalse(result["pkcs8_ed25519"])
        self.assertIn("base64_standard_first32", result["candidate_dids"])
        self.assertIn("base64_standard_last32", result["candidate_dids"])

    def test_base64_pkcs8_seed_round_trip(self):
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except BaseException:
            self.skipTest("cryptography is not installed")

        private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("02" * 32))
        der = private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        encoded = base64.b64encode(der).decode("ascii")
        self.assertEqual(len(encoded), 64)

        old = os.environ.get("TECHNOSCOUT_TEST_B64")
        os.environ["TECHNOSCOUT_TEST_B64"] = encoded
        try:
            identity = SigningIdentity.from_env("TECHNOSCOUT_TEST_B64")
        finally:
            if old is None:
                os.environ.pop("TECHNOSCOUT_TEST_B64", None)
            else:
                os.environ["TECHNOSCOUT_TEST_B64"] = old

        expected = SigningIdentity(
            seed=bytes.fromhex("02" * 32),
            did=identity.did,
        )
        self.assertEqual(identity.seed, expected.seed)
        signature = identity.sign("agents", 7, "hello")
        self.assertEqual(len(signature), 86)

    def test_legacy_passphrase_seed_compatibility(self):
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except BaseException:
            self.skipTest("cryptography is not installed")

        legacy_value = "G" * 64
        self.assertFalse(all(ch in "0123456789abcdefABCDEF" for ch in legacy_value))
        old = os.environ.get("TECHNOSCOUT_TEST_LEGACY")
        os.environ["TECHNOSCOUT_TEST_LEGACY"] = legacy_value
        try:
            identity = SigningIdentity.from_env("TECHNOSCOUT_TEST_LEGACY")
        finally:
            if old is None:
                os.environ.pop("TECHNOSCOUT_TEST_LEGACY", None)
            else:
                os.environ["TECHNOSCOUT_TEST_LEGACY"] = old

        import hashlib
        expected_seed = hashlib.sha256(legacy_value.encode("utf-8")).digest()
        self.assertEqual(identity.seed, expected_seed)

        diag = diagnose_signing_material(legacy_value)
        self.assertIn("legacy_sha256_text", diag["candidate_dids"])
        self.assertEqual(
            diag["candidate_dids"]["legacy_sha256_text"],
            identity.did,
        )

    def test_signing_identity_round_trip(self):
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except BaseException:
            self.skipTest("cryptography is not installed")

        old = os.environ.get("TECHNOSCOUT_TEST_SEED")
        os.environ["TECHNOSCOUT_TEST_SEED"] = "00" * 32
        try:
            identity = SigningIdentity.from_env("TECHNOSCOUT_TEST_SEED")
        finally:
            if old is None:
                os.environ.pop("TECHNOSCOUT_TEST_SEED", None)
            else:
                os.environ["TECHNOSCOUT_TEST_SEED"] = old

        self.assertTrue(identity.did.startswith("did:key:z6Mk"))
        signature = identity.sign("agents", 123, "hello")
        self.assertEqual(len(signature), 86)

        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        number = 0
        for ch in identity.did.removeprefix("did:key:z"):
            number = number * 58 + alphabet.index(ch)
        decoded = number.to_bytes((number.bit_length() + 7) // 8, "big")
        self.assertEqual(decoded[:2], b"\xed\x01")
        public_key = Ed25519PublicKey.from_public_bytes(decoded[2:])
        raw_sig = base64.urlsafe_b64decode(signature + "==")
        public_key.verify(raw_sig, b"agents|123|hello")


class SenderPermitIntegrationTests(unittest.TestCase):
    def test_sender_requires_and_consumes_one_time_permit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = connect(root / "test.db")
            self.assertTrue(create_reply_draft(
                con,
                "2026-09-09T02:00:00+00:00",
                "inference-agents",
                123,
                "did:key:target",
                50,
                "technical follow-up",
                "Could you share the benchmark details?",
            ))
            draft_id = int(pending_reply_drafts(con, 1)[0]["id"])
            self.assertTrue(review_reply_draft(con, draft_id, "approved"))
            con.commit()
            draft = get_reply_draft(con, draft_id)

            old = os.environ.get("TECHNOSCOUT_TEST_SEND_SEED")
            os.environ["TECHNOSCOUT_TEST_SEND_SEED"] = "04" * 32
            cfg = {
                "base_url": "https://technocore.chat",
                "signing_seed_env": "TECHNOSCOUT_TEST_SEND_SEED",
                "signing_env_file": "",
                "send_permit_required": True,
                "send_permit_ttl_seconds": 600,
                "sender_timeout_seconds": 1,
                "max_response_bytes": 100000,
            }
            try:
                sender = ApprovedDraftSender(cfg, con)
                with self.assertRaisesRegex(RuntimeError, "one-time send permit"):
                    sender.send_draft(draft)

                armed = sender.arm_draft(draft)
                self.assertTrue(armed["token"])
                sender._read_server_nonce = lambda room: 0
                sender._post = lambda room, text, nonce, signature: {"seq": 777}
                result = sender.send_draft(draft, permit_token=armed["token"])
                self.assertEqual(result["seq"], 777)
                self.assertEqual(get_reply_draft(con, draft_id)["status"], "sent")
                permits = send_permits_for_draft(con, draft_id)
                self.assertEqual(permits[0]["status"], "consumed")
            finally:
                if old is None:
                    os.environ.pop("TECHNOSCOUT_TEST_SEND_SEED", None)
                else:
                    os.environ["TECHNOSCOUT_TEST_SEND_SEED"] = old
                con.close()


class ManagedWorkerTests(unittest.TestCase):
    def test_timeout_kills_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "fake_worker.py"
            worker.write_text(
                """import argparse
import json
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--model")
args = parser.parse_args()
print(json.dumps({"type": "ready", "model": args.model}), flush=True)

for line in sys.stdin:
    req = json.loads(line)
    if req.get("op") == "chat":
        content = req.get("messages", [{}])[-1].get("content", "")
        if content == "hang":
            time.sleep(5)
        print(json.dumps({
            "type": "result",
            "id": req["id"],
            "ok": True,
            "content": "{}"
        }), flush=True)
""",
                encoding="utf-8",
            )
            cfg = {
                "mlx_worker_python": sys.executable,
                "mlx_worker_script": str(worker),
                "mlx_worker_log": str(root / "worker.log"),
                "mlx_worker_start_timeout_seconds": 2,
                "mlx_worker_first_request_extra_seconds": 0,
                "mlx_worker_kill_grace_seconds": 0.1,
                "triage_model": "fake-model",
                "research_model": "fake-model",
            }
            backend = ManagedMLXBackend(cfg)
            started = time.monotonic()
            try:
                with self.assertRaises(TimeoutError):
                    backend.chat(
                        "fake-model",
                        [{"role": "user", "content": "hang"}],
                        max_tokens=8,
                        temperature=0.0,
                        timeout_seconds=0.2,
                    )
                self.assertIsNone(backend.proc)
                self.assertLess(time.monotonic() - started, 3.0)
                result = backend.chat(
                    "fake-model",
                    [{"role": "user", "content": "recover"}],
                    max_tokens=8,
                    temperature=0.0,
                    timeout_seconds=1.0,
                )
                self.assertEqual(result, "{}")
                self.assertIsNotNone(backend.proc)
                self.assertGreaterEqual(backend.restart_count, 2)
            finally:
                backend.close()


if __name__ == "__main__":
    unittest.main()
