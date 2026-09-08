#!/usr/bin/env python3
"""One-time-permit Technocore sender for TechnoScout v0.7."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import room_messages, safe_room
from .db import (
    consume_send_permit,
    create_send_attempt,
    create_send_permit,
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


def _did_from_seed(seed: bytes) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return "did:key:z" + _base58btc(DID_PREFIX + public_key)


def diagnose_signing_material(value: str) -> dict[str, Any]:
    """Return non-secret structural diagnostics and candidate public DIDs."""
    value = str(value or "").strip()
    result: dict[str, Any] = {
        "chars": len(value),
        "hex64": bool(SEED_RE.fullmatch(value)),
        "base64_standard": False,
        "base64_urlsafe": False,
        "decoded_lengths": [],
        "pkcs8_ed25519": False,
        "candidate_dids": {},
    }

    if SEED_RE.fullmatch(value):
        try:
            result["candidate_dids"]["hex_raw32"] = _did_from_seed(bytes.fromhex(value))
        except Exception:
            pass
    else:
        # Compatibility with the repository's original sign.py:
        # every non-hex SIGN_SEED was treated as a passphrase and SHA-256'd.
        try:
            legacy_seed = hashlib.sha256(value.encode("utf-8")).digest()
            result["candidate_dids"]["legacy_sha256_text"] = _did_from_seed(legacy_seed)
        except Exception:
            pass

    seen: set[bytes] = set()
    padded = value + ("=" * ((4 - len(value) % 4) % 4))
    for name, altchars in (("standard", None), ("urlsafe", b"-_")):
        try:
            decoded = base64.b64decode(
                padded.encode("ascii"),
                altchars=altchars,
                validate=True,
            )
        except Exception:
            continue

        result[f"base64_{name}"] = True
        result["decoded_lengths"].append(len(decoded))
        if decoded in seen:
            continue
        seen.add(decoded)

        if len(decoded) == 32:
            try:
                result["candidate_dids"][f"base64_{name}_raw32"] = _did_from_seed(decoded)
            except Exception:
                pass

        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            loaded = serialization.load_der_private_key(decoded, password=None)
        except Exception:
            loaded = None
        if isinstance(loaded, Ed25519PrivateKey):
            result["pkcs8_ed25519"] = True
            seed = loaded.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )
            result["candidate_dids"][f"base64_{name}_pkcs8"] = _did_from_seed(seed)

        # Diagnostic-only candidates for legacy containers that embed a 32-byte
        # Ed25519 seed inside a larger decoded value. These are never accepted
        # automatically for signing.
        if len(decoded) > 32:
            for label, seed in (
                ("first32", decoded[:32]),
                ("last32", decoded[-32:]),
            ):
                try:
                    result["candidate_dids"][f"base64_{name}_{label}"] = _did_from_seed(seed)
                except Exception:
                    pass

    result["decoded_lengths"] = sorted(set(result["decoded_lengths"]))
    return result


@dataclass(frozen=True)
class SigningIdentity:
    seed: bytes
    did: str

    @staticmethod
    def _seed_from_file(env_name: str, env_file: str) -> str:
        path = Path(env_file).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            return ""

        info = path.stat()
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise RuntimeError(f"{path} is not owned by the current user")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077:
            raise RuntimeError(
                f"{path} permissions are too broad ({oct(mode)}); run chmod 600 {path}"
            )

        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() != env_name:
                continue
            value = value.strip()
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {"'", '"'}
            ):
                value = value[1:-1]
            return value.strip()
        return ""

    @classmethod
    def from_env(
        cls,
        env_name: str,
        env_file: str = "",
    ) -> "SigningIdentity":
        value = os.environ.get(env_name, "").strip()
        if not value and env_file:
            value = cls._seed_from_file(env_name, env_file)
        if not value:
            suffix = f" or {env_file}" if env_file else ""
            raise RuntimeError(f"{env_name} is not set{suffix}")
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except BaseException as exc:
            raise RuntimeError(
                "cryptography with Ed25519 support is required for signed sending"
            ) from exc

        seed: bytes | None = None
        if SEED_RE.fullmatch(value):
            seed = bytes.fromhex(value)
        else:
            decoded: bytes | None = None
            padded = value + ("=" * ((4 - len(value) % 4) % 4))
            for altchars in (None, b"-_"):
                try:
                    decoded = base64.b64decode(
                        padded.encode("ascii"),
                        altchars=altchars,
                        validate=True,
                    )
                    break
                except (ValueError, UnicodeEncodeError):
                    continue
                except Exception:
                    continue

            if decoded is not None:
                if len(decoded) == 32:
                    seed = decoded
                else:
                    try:
                        loaded = serialization.load_der_private_key(
                            decoded,
                            password=None,
                        )
                    except (ValueError, TypeError):
                        loaded = None
                    if isinstance(loaded, Ed25519PrivateKey):
                        seed = loaded.private_bytes(
                            encoding=serialization.Encoding.Raw,
                            format=serialization.PrivateFormat.Raw,
                            encryption_algorithm=serialization.NoEncryption(),
                        )

        if seed is None:
            # Backward compatibility with the original repository sign.py.
            # It accepts any non-hex SIGN_SEED as a passphrase and derives
            # the Ed25519 seed as SHA-256(UTF-8 text).
            seed = hashlib.sha256(value.encode("utf-8")).digest()

        if len(seed) != 32:
            raise ValueError(
                f"{env_name} could not be converted to a 32-byte Ed25519 seed"
            )

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


def _permit_hash(token: str) -> str:
    return hashlib.sha256(
        ("technoscout-send-permit-v1:" + str(token)).encode("utf-8")
    ).hexdigest()


def _draft_text_hash(room: str, text: str) -> str:
    return hashlib.sha256(
        ("technoscout-draft-v1\0" + room + "\0" + text).encode("utf-8")
    ).hexdigest()


class ApprovedDraftSender:
    def __init__(self, cfg: dict[str, Any], db: Any) -> None:
        self.cfg = cfg
        self.db = db
        self.identity = SigningIdentity.from_env(
            str(cfg.get("signing_seed_env", "SIGN_SEED")),
            str(cfg.get("signing_env_file", ".env")),
        )

    def _read_server_nonce(self, room: str) -> int:
        query = urllib.parse.urlencode({"format": "json", "limit": 200})
        request = urllib.request.Request(
            f"{self.cfg['base_url']}/r/{room}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "technoscout-sender/0.7",
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
                "User-Agent": "technoscout-sender/0.7",
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

    def arm_draft(self, draft: Any) -> dict[str, Any]:
        if str(draft["status"]) != "approved":
            raise RuntimeError(
                f"draft #{draft['id']} must be approved before arming "
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
            "reserved",
        }:
            raise RuntimeError(
                f"draft #{draft['id']} already has a {previous['status']} send attempt; "
                "arming is blocked"
            )

        token = secrets.token_urlsafe(18)
        now = time.time()
        ttl = max(30, min(
            3600,
            int(self.cfg.get("send_permit_ttl_seconds", 600)),
        ))
        expires_at = now + ttl
        create_send_permit(
            self.db,
            draft_id=int(draft["id"]),
            created_at=now,
            expires_at=expires_at,
            token_hash=_permit_hash(token),
            did=self.identity.did,
            room=room,
            text_hash=_draft_text_hash(room, text),
        )
        self.db.commit()
        return {
            "draft_id": int(draft["id"]),
            "room": room,
            "did": self.identity.did,
            "token": token,
            "ttl_seconds": ttl,
            "expires_at": expires_at,
        }

    def send_draft(
        self,
        draft: Any,
        permit_token: str | None = None,
    ) -> dict[str, Any]:
        permit_required = bool(self.cfg.get("send_permit_required", True))
        if permit_required and not str(permit_token or "").strip():
            raise RuntimeError(
                "one-time send permit is required; run --arm-send ID first"
            )
        if not permit_required and not bool(self.cfg.get("sending_enabled", False)):
            raise RuntimeError(
                "sending is disabled; enable the legacy gate or use one-time permits"
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
            "reserved",
        }:
            raise RuntimeError(
                f"draft #{draft['id']} already has a {previous['status']} send attempt; "
                "automatic re-send is blocked"
            )

        server_nonce = self._read_server_nonce(room)

        if permit_required:
            now = time.time()
            consumed = consume_send_permit(
                self.db,
                draft_id=int(draft["id"]),
                token_hash=_permit_hash(str(permit_token).strip()),
                did=self.identity.did,
                room=room,
                text_hash=_draft_text_hash(room, text),
                now=now,
            )
            if not consumed:
                self.db.rollback()
                raise RuntimeError(
                    "send permit is invalid, expired, already used, superseded, "
                    "or bound to different draft content"
                )

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
