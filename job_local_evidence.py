#!/usr/bin/env python3
"""Local immutable evidence for already-reviewed Kibble JOB/CLAIM state.

This module never performs network I/O and never sends anything. It stores the
exact JOB snapshot that was already verified during human CLAIM review, and can
cryptographically validate a locally retained signed CLAIM receipt after the
remote room retention ring has aged that CLAIM out.

The local CLAIM receipt is deliberately NOT a substitute for DELIVER readiness.
DELIVER must still perform its normal live lifecycle/conflict check.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

from job_shadow import content_hash
from technoscout.common import utc_now
from technoscout.sender import DID_PREFIX, sweep_text


EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_exact_snapshots (
    room TEXT NOT NULL,
    job_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    job_seq INTEGER NOT NULL,
    issuer_did TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    job_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY(room, job_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_job_exact_snapshots_job
    ON job_exact_snapshots(room, job_id);
"""

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {char: index for index, char in enumerate(_B58)}
_NONCE_RE = re.compile(r"^[0-9]{1,19}$")


def ensure_evidence_schema(con: Any) -> None:
    con.executescript(EVIDENCE_SCHEMA)


def _snapshot_payload(job: dict[str, Any]) -> str:
    return json.dumps(job, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _snapshot_hash(payload: str) -> str:
    return hashlib.sha256(
        ("technoscout-job-snapshot-v1\0" + payload).encode("utf-8")
    ).hexdigest()


def _claim_text(job_id: str) -> str:
    return sweep_text(f"CLAIM v1 | {job_id} | worker")


def _claim_text_hash(text: str) -> str:
    return hashlib.sha256(
        ("technoscout-job-claim-v1\0" + text).encode("utf-8")
    ).hexdigest()


def store_exact_job_snapshot(
    con: Any,
    candidate: dict[str, Any],
    job: dict[str, Any],
) -> dict[str, Any]:
    """Persist one exact already-verified JOB snapshot, fail-closed on mismatch."""
    ensure_evidence_schema(con)

    if str(job.get("verb", "")) != "JOB":
        return {"state": "BLOCKED", "reason": "snapshot payload is not a JOB"}
    if str(job.get("job_id", "")) != str(candidate.get("job_id", "")):
        return {"state": "BLOCKED", "reason": "snapshot job_id mismatch"}
    if str(job.get("job_type", "")) != str(candidate.get("job_type", "")):
        return {"state": "BLOCKED", "reason": "snapshot job_type mismatch"}
    if content_hash(job) != str(candidate.get("content_hash", "")):
        return {"state": "BLOCKED", "reason": "snapshot content hash mismatch"}

    payload = _snapshot_payload(job)
    payload_hash = _snapshot_hash(payload)
    key = (
        str(candidate["room"]),
        str(candidate["job_id"]),
        str(candidate["content_hash"]),
    )
    existing = con.execute(
        """
        SELECT job_seq,issuer_did,payload_hash,job_json
        FROM job_exact_snapshots
        WHERE room=? AND job_id=? AND content_hash=?
        """,
        key,
    ).fetchone()

    if existing is not None:
        if (
            int(existing["job_seq"]) != int(candidate["job_seq"])
            or str(existing["issuer_did"]) != str(candidate["issuer_did"])
            or str(existing["payload_hash"]) != payload_hash
            or str(existing["job_json"]) != payload
        ):
            return {"state": "BLOCKED", "reason": "existing immutable JOB snapshot mismatch"}
        return {"state": "SNAPSHOT_VERIFIED", "payload_hash": payload_hash}

    con.execute(
        """
        INSERT INTO job_exact_snapshots(
          room,job_id,content_hash,job_seq,issuer_did,captured_at,job_json,payload_hash
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            key[0],
            key[1],
            key[2],
            int(candidate["job_seq"]),
            str(candidate["issuer_did"]),
            utc_now(),
            payload,
            payload_hash,
        ),
    )
    con.commit()
    return {"state": "SNAPSHOT_STORED", "payload_hash": payload_hash}


def load_exact_job_snapshot(
    con: Any,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Load and re-verify an immutable local JOB snapshot."""
    ensure_evidence_schema(con)
    row = con.execute(
        """
        SELECT room,job_id,content_hash,job_seq,issuer_did,job_json,payload_hash
        FROM job_exact_snapshots
        WHERE room=? AND job_id=? AND content_hash=?
        """,
        (
            str(candidate["room"]),
            str(candidate["job_id"]),
            str(candidate["content_hash"]),
        ),
    ).fetchone()
    if row is None:
        return {"state": "SNAPSHOT_NOT_FOUND"}

    if int(row["job_seq"]) != int(candidate["job_seq"]):
        return {"state": "SNAPSHOT_MISMATCH", "reason": "job_seq mismatch"}
    if str(row["issuer_did"]) != str(candidate["issuer_did"]):
        return {"state": "SNAPSHOT_MISMATCH", "reason": "issuer DID mismatch"}

    payload = str(row["job_json"])
    if _snapshot_hash(payload) != str(row["payload_hash"]):
        return {"state": "SNAPSHOT_MISMATCH", "reason": "snapshot payload hash mismatch"}
    try:
        job = json.loads(payload)
    except json.JSONDecodeError:
        return {"state": "SNAPSHOT_MISMATCH", "reason": "snapshot JSON is invalid"}
    if not isinstance(job, dict):
        return {"state": "SNAPSHOT_MISMATCH", "reason": "snapshot JSON is not an object"}
    if str(job.get("verb", "")) != "JOB":
        return {"state": "SNAPSHOT_MISMATCH", "reason": "snapshot is not a JOB"}
    if str(job.get("job_id", "")) != str(candidate["job_id"]):
        return {"state": "SNAPSHOT_MISMATCH", "reason": "snapshot job_id mismatch"}
    if content_hash(job) != str(candidate["content_hash"]):
        return {"state": "SNAPSHOT_MISMATCH", "reason": "snapshot content hash mismatch"}

    return {
        "state": "EXACT",
        "job": job,
        "source": "local-immutable-claim-snapshot",
    }


def _base58btc_decode(value: str) -> bytes:
    if not value:
        raise ValueError("empty base58 value")
    number = 0
    for char in value:
        if char not in _B58_INDEX:
            raise ValueError("invalid base58 character")
        number = number * 58 + _B58_INDEX[char]
    body = b"" if number == 0 else number.to_bytes((number.bit_length() + 7) // 8, "big")
    zeros = len(value) - len(value.lstrip("1"))
    return (b"\x00" * zeros) + body


def _public_key_from_did(did: str) -> bytes:
    prefix = "did:key:z"
    if not str(did).startswith(prefix):
        raise ValueError("unsupported DID")
    decoded = _base58btc_decode(str(did)[len(prefix):])
    if len(decoded) != len(DID_PREFIX) + 32 or not decoded.startswith(DID_PREFIX):
        raise ValueError("DID is not an Ed25519 did:key")
    return decoded[len(DID_PREFIX):]


def _verify_signature(*, did: str, room: str, nonce: str, text: str, signature: str) -> bool:
    if not _NONCE_RE.fullmatch(str(nonce)):
        return False
    try:
        padded = str(signature) + ("=" * ((4 - len(str(signature)) % 4) % 4))
        raw_sig = base64.urlsafe_b64decode(padded.encode("ascii"))
        if len(raw_sig) != 64:
            return False
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        key = Ed25519PublicKey.from_public_bytes(_public_key_from_did(did))
        key.verify(raw_sig, f"{room}|{nonce}|{text}".encode("utf-8"))
        return True
    except Exception:
        return False


def verify_local_claim_receipt(con: Any, trial: dict[str, Any]) -> dict[str, Any]:
    """Verify a signed HTTP-200 CLAIM receipt already persisted by send_claim()."""
    if str(trial.get("status", "")) != "SENT" or trial.get("sent_seq") is None:
        return {"state": "CLAIM_LOCAL_RECEIPT_INVALID", "reason": "claim trial is not SENT"}

    expected_text = _claim_text(str(trial["job_id"]))
    expected_hash = _claim_text_hash(expected_text)
    trial_hash = str(trial.get("claim_text_hash", "") or "")
    if trial_hash and trial_hash != expected_hash:
        return {"state": "CLAIM_LOCAL_RECEIPT_INVALID", "reason": "claim text hash mismatch"}

    rows = con.execute(
        """
        SELECT attempted_at,did,nonce,sig,text,status,http_status,detail
        FROM job_claim_attempts
        WHERE room=? AND job_id=? AND content_hash=?
        ORDER BY id DESC
        LIMIT 20
        """,
        (
            str(trial["room"]),
            str(trial["job_id"]),
            str(trial["content_hash"]),
        ),
    ).fetchall()
    if not rows:
        return {"state": "CLAIM_LOCAL_RECEIPT_NOT_FOUND"}

    expected_detail = f"seq={int(trial['sent_seq'])}"
    expected_did = str(trial["sender_did"])
    for row in rows:
        if str(row["status"]) != "sent" or int(row["http_status"] or 0) != 200:
            continue
        if str(row["detail"]) != expected_detail:
            continue
        if str(row["did"]) != expected_did:
            continue
        if str(row["text"]) != expected_text:
            continue
        nonce = str(row["nonce"])
        signature = str(row["sig"])
        if not _verify_signature(
            did=expected_did,
            room=str(trial["room"]),
            nonce=nonce,
            text=expected_text,
            signature=signature,
        ):
            continue
        return {
            "state": "CLAIM_CONFIRMED",
            "seq": int(trial["sent_seq"]),
            "source": "local-signed-http200-receipt",
        }

    return {
        "state": "CLAIM_LOCAL_RECEIPT_INVALID",
        "reason": "no signed HTTP-200 receipt matches the SENT claim binding",
    }
