#!/usr/bin/env python3
"""Explicit, approved-draft-only Technocore sender for TechnoScout v0.6."""

from __future__ import annotations

import base64
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .common import room_messages, safe_room
from .db import (
    create_send_attempt,
    finish_send_attempt,
    get_last_send_attempt,
    reserve_send_nonce,
    set_draft_status,
)

DID_PREFIX = b"\xed\x01"
SEED_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def sweep_text(value: str) -> str:
    """Match Technocore's single-line sweep before signing."""
    if not isinstance(value, str):
        raise TypeError("message text must be a string")
    result = []
    for char in value:
        category = unicodedata.category(char)
        if category in {"Cc", "Cf", "Cs", "Co", "Zl", "Zp"}:
            result.append(" ")
        else:
            result.append(char)
    swept = "".join(result).strip()
    if not swept:
        raise ValueError("message is empty after Technocore single-line sweep")
    if len(swept) > 4096:
        raise ValueError("message exceeds Technocore 4096-character limit")
    return swept


def _base58btc(data: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    zeros = len(data) - len(data.lstrip(b"\x00"))
    number = int.from_bytes(data, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = alphabet[remainder] + encoded
    return ("1" * zeros) + (encoded or ("" if zeros else "1"))


@dataclass(frozen=True)
class SigningIdentity:
    seed: bytes
    did: str

    @classmethod
    def from_env(cls, env_name: str) -> "SigningIdentity":
        value = os.environ.get(env_name, "").strip()
        if not value:
            raise RuntimeError(f"{env_name} is not set")
        if not SEED_RE.fullmatch(value):
            raise ValueError(f"{env_name} must be exactly 64 hexadecimal characters")
        seed = bytes.fromhex(value)
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except BaseException as exc:
            raise RuntimeError(
                "cryptography with Ed25519 support is required for signed sending"
            ) from exc

        private_key = Ed25519PrivateKey.from_private_bytes(seed)
        public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        did = "did:key:z" + _base58btc(DID_PREFIX + public_key)
        return cls(seed=seed, did=did)

    def sign(self, room: str, nonce: int, text: str) -> str:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except BaseException as exc:
            raise RuntimeError(
                "cryptography with Ed25519 support is required for signed sending"
            ) from exc
        private_key = Ed25519PrivateKey.from_private_bytes(self.seed)
        canonical = f"{room}|{nonce}|{text}".encode("utf-8")
        signature = private_key.sign(canonical)
        return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


class SendRefused(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Technocore refused send with HTTP {status}: {body[:300]}")
        self.status = int(status)
        self.body = body


class SendUncertain(RuntimeError):
    pass


class ApprovedDraftSender:
    def __init__(self, cfg: dict[str, Any], db: Any) -> None:
        self.cfg = cfg
        self.db = db
        self.identity = SigningIdentity.from_env(
            str(cfg.get("signing_seed_env", "SIGN_SEED"))
        )

    def _read_server_nonce(self, room: str) -> int:
        query = urllib.parse.urlencode({"format": "json", "limit": 200})
        request = urllib.request.Request(
            f"{self.cfg['base_url']}/r/{room}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "technoscout-sender/0.6",
            },
            method="GET",
        )
        timeout = float(self.cfg.get("sender_timeout_seconds", 20))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = json.loads(
                response.read(int(self.cfg["max_response_bytes"])).decode("utf-8")
            )
        maximum = 0
        for item in room_messages(raw):
            if str(item.get("from", "")) != self.identity.did:
                continue
            try:
                maximum = max(maximum, int(item.get("nonce", 0)))
            except (TypeError, ValueError):
                continue
        return maximum

    def _post(
        self,
        room: str,
        text: str,
        nonce: int,
        signature: str,
    ) -> dict[str, Any]:
        url = f"{self.cfg['base_url']}/r/{room}?format=json"
        body = {
            "text": text,
            "did": self.identity.did,
            "sig": signature,
            "nonce": str(nonce),
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "technoscout-sender/0.6",
            },
            method="POST",
        )
        timeout = float(self.cfg.get("sender_timeout_seconds", 20))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read(
                    int(self.cfg["max_response_bytes"])
                ).decode("utf-8")
                status = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as exc:
            response_body = exc.read(16384).decode("utf-8", "replace")
            raise SendRefused(exc.code, response_body) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SendUncertain(
                f"transport failed after nonce reservation: {type(exc).__name__}: {exc}"
            ) from exc

        if status != 200:
            raise SendUncertain(f"unexpected successful transport status {status}")
        try:
            payload = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise SendUncertain(
                "Technocore returned HTTP 200 but response was not JSON; "
                "do not retry automatically"
            ) from exc

        messages = room_messages(payload)
        matches = [
            item
            for item in messages
            if str(item.get("from", "")) == self.identity.did
            and str(item.get("text", "")) == text
            and str(item.get("sig", "")) == signature
            and str(item.get("nonce", "")) == str(nonce)
        ]
        if not matches:
            raise SendUncertain(
                "HTTP 200 response did not contain the exact signed record; "
                "do not retry automatically"
            )
        return matches[-1]

    def send_draft(self, draft: Any) -> dict[str, Any]:
        if not bool(self.cfg.get("sending_enabled", False)):
            raise RuntimeError(
                "sending is disabled; set sending_enabled=true in the local config"
            )
        if str(draft["status"]) != "approved":
            raise RuntimeError(
                f"draft #{draft['id']} must be approved before sending "
                f"(current={draft['status']})"
            )

        room = safe_room(draft["room"])
        if not room:
            raise ValueError("draft room is invalid")
        text = sweep_text(str(draft["draft_text"]))

        previous = get_last_send_attempt(self.db, int(draft["id"]))
        if previous is not None and str(previous["status"]) in {
            "sent",
            "uncertain",
        }:
            raise RuntimeError(
                f"draft #{draft['id']} already has a {previous['status']} send attempt; "
                "automatic re-send is blocked"
            )

        server_nonce = self._read_server_nonce(room)
        nonce = reserve_send_nonce(
            self.db,
            self.identity.did,
            room,
            server_nonce=server_nonce,
            floor=time.time_ns(),
        )
        signature = self.identity.sign(room, nonce, text)
        attempt_id = create_send_attempt(
            self.db,
            draft_id=int(draft["id"]),
            attempted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            did=self.identity.did,
            room=room,
            nonce=nonce,
            signature=signature,
            text=text,
        )
        self.db.commit()

        try:
            record = self._post(room, text, nonce, signature)
        except SendRefused as exc:
            if exc.status == 429:
                finish_send_attempt(
                    self.db,
                    attempt_id,
                    status="rate_limited",
                    http_status=exc.status,
                    detail=exc.body[:1000],
                )
                # Rate-limit refusal is a documented non-write. Keep approved so
                # a human may explicitly retry later with a fresh nonce.
                self.db.commit()
                raise

            finish_send_attempt(
                self.db,
                attempt_id,
                status="refused",
                http_status=exc.status,
                detail=exc.body[:1000],
            )
            set_draft_status(self.db, int(draft["id"]), "send_blocked")
            self.db.commit()
            raise
        except SendUncertain as exc:
            finish_send_attempt(
                self.db,
                attempt_id,
                status="uncertain",
                http_status=None,
                detail=str(exc)[:1000],
            )
            set_draft_status(self.db, int(draft["id"]), "send_uncertain")
            self.db.commit()
            raise

        finish_send_attempt(
            self.db,
            attempt_id,
            status="sent",
            http_status=200,
            detail=f"seq={record.get('seq', '')}",
        )
        set_draft_status(self.db, int(draft["id"]), "sent")
        self.db.commit()
        return {
            "draft_id": int(draft["id"]),
            "room": room,
            "did": self.identity.did,
            "nonce": nonce,
            "seq": record.get("seq"),
            "text": text,
        }
