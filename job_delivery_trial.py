#!/usr/bin/env python3
"""Human-approved, one-job Kibble DELIVER trial.

This is deliberately separate from chat autonomy and Job Shadow. It only delivers
an answer for a job that this agent already CLAIMed and that passed the local
execution + adversarial quality gates. Delivery requires three explicit local
steps: prepare -> approve -> send.

Safety properties:
- exact claimed job/content hash/claim seq/sender DID binding
- only QUALITY_REVIEWED PASS/REVISED answers above a confidence threshold
- deterministic quality guard is re-run at prepare
- exact retained CLAIM and no later DELIVER/RESULT/ATTEST/WITNESS/conflicting CLAIM
- fresh snapshot retry on busy-room catch-up gaps; persistent ambiguity fails closed
- short prepared and armed TTLs
- one-use DB permit consumed before POST
- existing hardened Ed25519/nonce/POST transport reused from technoscout.sender
- uncertain POST outcome is terminal; never retry automatically
- raw JOB text is not persisted here; outgoing answer already exists in local quality-review storage
"""

from __future__ import annotations

import argparse
import hashlib
import time
from typing import Any, Callable

from job_candidate_refiner import fetch_exact_job
from job_claim_trial import ensure_claim_schema
from job_execution_draft import claimed_trial
from job_execution_quality_gate import deterministic_quality_flags, ensure_quality_schema
from job_live_revalidator import retained_export_messages
from job_shadow import parse_kibble_message, sender_of
from technoscout.common import room_messages, seq_of, technocore_json
from technoscout.db import connect, reserve_send_nonce
from technoscout.sender import ApprovedDraftSender, SendRefused, SendUncertain, SigningIdentity, sweep_text
from technoscout_cli import database_path, load_config


DELIVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_delivery_trials (
    room TEXT NOT NULL,
    job_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    claim_seq INTEGER NOT NULL,
    claim_sender_did TEXT NOT NULL,
    quality_reviewed_at TEXT NOT NULL,
    quality_decision TEXT NOT NULL,
    quality_confidence INTEGER NOT NULL,
    answer_hash TEXT NOT NULL,
    prepared_at REAL NOT NULL,
    prepare_expires_at REAL NOT NULL,
    approved_at REAL,
    permit_expires_at REAL,
    consumed_at REAL,
    sender_did TEXT NOT NULL DEFAULT '',
    deliver_text_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    sent_seq INTEGER,
    detail TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(room, job_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_job_delivery_trials_status
    ON job_delivery_trials(status, prepared_at DESC);

CREATE TABLE IF NOT EXISTS job_delivery_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempted_at REAL NOT NULL,
    room TEXT NOT NULL,
    job_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    did TEXT NOT NULL,
    nonce TEXT NOT NULL,
    sig TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_job_delivery_attempts_job
    ON job_delivery_attempts(room, job_id, id DESC);
"""

TERMINAL_TRIAL_STATES = {"SENT", "UNCERTAIN", "RESERVED"}
RETRYABLE_READY_STATES = {"INCONCLUSIVE_GAP", "INCONCLUSIVE_TRUNCATED", "INCONCLUSIVE"}
DELIVERY_BLOCKED_TERMS = (
    "private key", "seed phrase", "api key", "password", "credential",
    "wallet", "transfer funds", "send funds", "payment", "escrow",
    "faucet", "airdrop", "stake tokens", "staking", "bridge assets",
    "claim reward", "claim tokens", "htlc",
)


def ensure_delivery_schema(con: Any) -> None:
    con.executescript(DELIVERY_SCHEMA)


def _text_hash(text: str) -> str:
    return hashlib.sha256(("technoscout-job-deliver-v1\0" + text).encode("utf-8")).hexdigest()


def _answer_hash(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _deliver_text(job_id: str, answer: str) -> str:
    return sweep_text(f"DELIVER v1 | {job_id} | {_one_line(answer)}")


def _quality_row(con: Any, room: str, job_id: str) -> dict[str, Any] | None:
    ensure_quality_schema(con)
    row = con.execute(
        """
        SELECT room,job_id,content_hash,reviewed_at,model,decision,confidence,
               deterministic_flags,critique,answer_hash,answer_text,status
        FROM job_execution_quality_reviews
        WHERE room=? AND job_id=?
        ORDER BY reviewed_at DESC
        LIMIT 1
        """,
        (str(room), str(job_id)),
    ).fetchone()
    return {key: row[key] for key in row.keys()} if row is not None else None


def eligible_delivery(con: Any, cfg: dict[str, Any], job_id: str, *, room: str = "kibble") -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    ensure_claim_schema(con)
    ensure_quality_schema(con)
    trial, reason = claimed_trial(con, job_id, room=room)
    if trial is None:
        return None, None, reason
    quality = _quality_row(con, room, job_id)
    if quality is None:
        return None, None, "no adversarial quality-reviewed answer exists"
    if str(quality["status"]) != "QUALITY_REVIEWED":
        return None, None, f"quality status is {quality['status']}, not QUALITY_REVIEWED"
    if str(quality["decision"]) not in {"PASS", "REVISED"}:
        return None, None, f"quality decision is {quality['decision']}"
    if str(quality["content_hash"]) != str(trial["content_hash"]):
        return None, None, "quality answer content binding does not match claimed JOB"
    minimum = max(50, min(100, int(cfg.get("job_delivery_min_quality_confidence", 80))))
    if int(quality["confidence"] or 0) < minimum:
        return None, None, f"quality confidence is below {minimum}"
    answer = str(quality["answer_text"] or "").strip()
    if not answer:
        return None, None, "quality answer is empty"
    if _answer_hash(answer) != str(quality["answer_hash"]):
        return None, None, "quality answer hash mismatch"
    lowered = answer.lower()
    for term in DELIVERY_BLOCKED_TERMS:
        if term in lowered:
            return None, None, f"quality answer contains blocked delivery term: {term}"
    if "http://" in lowered or "https://" in lowered:
        return None, None, "quality answer contains a URL"
    max_chars = max(200, min(3500, int(cfg.get("job_delivery_max_text_chars", 1800))))
    text = _deliver_text(job_id, answer)
    if len(text) > max_chars:
        return None, None, f"DELIVER text exceeds configured {max_chars}-character limit"
    return trial, quality, "eligible"


def _scan_delivery_state(trial: dict[str, Any], messages: list[dict[str, Any]], *, require_claim: bool) -> dict[str, Any]:
    job_id = str(trial["job_id"])
    claim_seq = int(trial["sent_seq"])
    claim_did = str(trial["sender_did"])
    saw_claim = False
    highest = 0
    for message in sorted(messages, key=seq_of):
        message_seq = seq_of(message)
        highest = max(highest, message_seq)
        parsed = parse_kibble_message(message.get("text", message.get("message", "")))
        if not parsed or parsed.get("job_id") != job_id:
            continue
        verb = str(parsed.get("verb", "")).upper()
        if verb == "CLAIM":
            if message_seq == claim_seq:
                if sender_of(message) != claim_did:
                    return {"state": "CLAIM_MISMATCH", "highest_seq": highest}
                saw_claim = True
            elif message_seq > claim_seq and sender_of(message) != claim_did:
                return {"state": "CLAIM_CONFLICT", "highest_seq": highest}
            continue
        if message_seq <= claim_seq:
            continue
        if verb in {"DELIVER", "RESULT"}:
            return {"state": "ALREADY_DELIVERED", "highest_seq": highest, "verb": verb}
        if verb in {"ATTEST", "WITNESS"}:
            return {"state": "ALREADY_CLOSED", "highest_seq": highest, "verb": verb}
    if require_claim and not saw_claim:
        return {"state": "CLAIM_NOT_RETAINED", "highest_seq": highest}
    return {"state": "READY", "highest_seq": highest}


def _check_delivery_snapshot(
    cfg: dict[str, Any],
    trial: dict[str, Any],
    *,
    fetcher: Callable[[dict[str, Any], str, dict[str, Any]], Any],
    export_fetcher: Callable[[dict[str, Any], str], list[dict[str, Any]]],
    attempt: int,
) -> dict[str, Any]:
    room = str(trial["room"])
    page_limit = max(20, min(200, int(cfg.get("job_gate_live_page_limit", 200))))
    max_pages = max(1, min(20, int(cfg.get("job_gate_live_max_pages", 6))))
    snapshot = export_fetcher(cfg, room)
    base = _scan_delivery_state(trial, snapshot, require_claim=True)
    seen = len(snapshot)
    if base["state"] != "READY":
        return {**base, "messages": seen, "pages": 0, "source": "export", "snapshot_attempts": attempt}
    cursor = int(base["highest_seq"])
    if cursor <= 0:
        return {"state": "INCONCLUSIVE", "messages": seen, "pages": 0, "source": "export", "snapshot_attempts": attempt}

    pages = 0
    while pages < max_pages:
        payload = fetcher(cfg, f"/r/{room}", {"format": "json", "since": cursor, "limit": page_limit})
        messages = sorted(room_messages(payload), key=seq_of)
        pages += 1
        seen += len(messages)
        if not messages:
            return {"state": "READY_CONFIRMED", "messages": seen, "pages": pages, "source": "export+catchup", "snapshot_attempts": attempt}
        first_seq = seq_of(messages[0])
        if first_seq > cursor + 1:
            return {
                "state": "INCONCLUSIVE_GAP", "messages": seen, "pages": pages,
                "source": "export+catchup", "snapshot_attempts": attempt,
                "gap_from": cursor + 1, "gap_to": first_seq - 1,
            }
        check = _scan_delivery_state(trial, messages, require_claim=False)
        if check["state"] != "READY":
            return {**check, "messages": seen, "pages": pages, "source": "export+catchup", "snapshot_attempts": attempt}
        new_cursor = max(cursor, int(check["highest_seq"]))
        if new_cursor <= cursor:
            return {"state": "INCONCLUSIVE", "messages": seen, "pages": pages, "source": "export+catchup", "snapshot_attempts": attempt}
        cursor = new_cursor
        if len(messages) < page_limit:
            return {"state": "READY_CONFIRMED", "messages": seen, "pages": pages, "source": "export+catchup", "snapshot_attempts": attempt}
    return {"state": "INCONCLUSIVE_TRUNCATED", "messages": seen, "pages": pages, "source": "export+catchup", "snapshot_attempts": attempt}


def live_delivery_ready(
    cfg: dict[str, Any],
    trial: dict[str, Any],
    *,
    fetcher: Callable[[dict[str, Any], str, dict[str, Any]], Any] | None = None,
    export_fetcher: Callable[[dict[str, Any], str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    read = fetcher or technocore_json
    export_read = export_fetcher or retained_export_messages
    retries = max(0, min(4, int(cfg.get("job_delivery_snapshot_retries", 2))))
    last: dict[str, Any] | None = None
    for attempt in range(1, retries + 2):
        result = _check_delivery_snapshot(cfg, trial, fetcher=read, export_fetcher=export_read, attempt=attempt)
        if result["state"] not in RETRYABLE_READY_STATES:
            return result
        last = result
    assert last is not None
    return last


def prepare_delivery(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    *,
    room: str = "kibble",
    readiness_checker: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    exact_fetcher: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    ensure_delivery_schema(con)
    claim, quality, reason = eligible_delivery(con, cfg, job_id, room=room)
    if claim is None or quality is None:
        return {"state": "BLOCKED", "reason": reason}

    candidate = {
        "room": claim["room"], "job_id": claim["job_id"], "job_seq": claim["job_seq"],
        "issuer_did": claim["issuer_did"], "content_hash": claim["content_hash"],
    }
    exact = exact_fetcher(cfg, candidate) if exact_fetcher is not None else fetch_exact_job(cfg, candidate)
    if exact.get("state") != "EXACT":
        return {"state": "BLOCKED", "reason": f"exact JOB fetch failed: {exact.get('state','UNKNOWN')}"}
    flags = deterministic_quality_flags(exact["job"], str(quality["answer_text"]))
    if flags:
        return {"state": "BLOCKED", "reason": "quality answer fails deterministic guard at delivery: " + "; ".join(flags)}

    check = readiness_checker or live_delivery_ready
    live = check(cfg, claim)
    if live.get("state") != "READY_CONFIRMED":
        return {"state": "BLOCKED", "reason": f"live delivery check failed: {live.get('state','UNKNOWN')}", "live": live}

    existing = con.execute(
        "SELECT status FROM job_delivery_trials WHERE room=? AND job_id=? AND content_hash=?",
        (claim["room"], claim["job_id"], claim["content_hash"]),
    ).fetchone()
    if existing is not None and str(existing["status"]) in TERMINAL_TRIAL_STATES:
        return {"state": "BLOCKED", "reason": f"existing delivery trial is {existing['status']}; automatic re-arm is forbidden"}

    answer = str(quality["answer_text"])
    text = _deliver_text(job_id, answer)
    clock = time.time() if now is None else float(now)
    ttl = max(60, min(1800, int(cfg.get("job_delivery_prepare_ttl_seconds", 600))))
    con.execute(
        """
        INSERT INTO job_delivery_trials(
          room,job_id,content_hash,claim_seq,claim_sender_did,quality_reviewed_at,
          quality_decision,quality_confidence,answer_hash,prepared_at,prepare_expires_at,
          sender_did,deliver_text_hash,status,detail
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(room,job_id,content_hash) DO UPDATE SET
          claim_seq=excluded.claim_seq,
          claim_sender_did=excluded.claim_sender_did,
          quality_reviewed_at=excluded.quality_reviewed_at,
          quality_decision=excluded.quality_decision,
          quality_confidence=excluded.quality_confidence,
          answer_hash=excluded.answer_hash,
          prepared_at=excluded.prepared_at,
          prepare_expires_at=excluded.prepare_expires_at,
          approved_at=NULL,
          permit_expires_at=NULL,
          consumed_at=NULL,
          sender_did='',
          deliver_text_hash=excluded.deliver_text_hash,
          status='PREPARED',
          sent_seq=NULL,
          detail=''
        """,
        (
            claim["room"], claim["job_id"], claim["content_hash"], int(claim["sent_seq"]),
            claim["sender_did"], quality["reviewed_at"], quality["decision"],
            int(quality["confidence"]), quality["answer_hash"], clock, clock + ttl,
            "", _text_hash(text), "PREPARED", "",
        ),
    )
    con.commit()
    return {"state": "PREPARED", "job_id": job_id, "answer": answer, "text": text, "quality": quality, "live": live, "ttl_seconds": ttl}


def approve_delivery(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    *,
    room: str = "kibble",
    readiness_checker: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    identity_factory: Callable[[dict[str, Any]], Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    ensure_delivery_schema(con)
    clock = time.time() if now is None else float(now)
    row = con.execute(
        "SELECT * FROM job_delivery_trials WHERE room=? AND job_id=? ORDER BY prepared_at DESC LIMIT 1",
        (str(room), str(job_id)),
    ).fetchone()
    if row is None or str(row["status"]) != "PREPARED":
        return {"state": "BLOCKED", "reason": "job is not in PREPARED delivery state"}
    if float(row["prepare_expires_at"]) < clock:
        con.execute("UPDATE job_delivery_trials SET status='EXPIRED',detail='prepare TTL expired' WHERE room=? AND job_id=? AND content_hash=?", (row["room"], row["job_id"], row["content_hash"]))
        con.commit()
        return {"state": "BLOCKED", "reason": "prepared delivery review expired; prepare again"}

    claim, quality, reason = eligible_delivery(con, cfg, job_id, room=room)
    if claim is None or quality is None:
        return {"state": "BLOCKED", "reason": reason}
    if str(claim["content_hash"]) != str(row["content_hash"]) or int(claim["sent_seq"]) != int(row["claim_seq"]):
        return {"state": "BLOCKED", "reason": "claim binding changed since delivery prepare"}
    if str(quality["answer_hash"]) != str(row["answer_hash"]):
        return {"state": "BLOCKED", "reason": "quality answer changed since delivery prepare"}

    check = readiness_checker or live_delivery_ready
    live = check(cfg, claim)
    if live.get("state") != "READY_CONFIRMED":
        return {"state": "BLOCKED", "reason": f"live delivery check failed: {live.get('state','UNKNOWN')}", "live": live}

    make_identity = identity_factory or (
        lambda config: SigningIdentity.from_env(
            str(config.get("signing_seed_env", "SIGN_SEED")),
            str(config.get("signing_env_file", ".env")),
        )
    )
    identity = make_identity(cfg)
    if str(identity.did) != str(claim["sender_did"]):
        return {"state": "BLOCKED", "reason": "delivery signer is not the DID that sent the CLAIM"}

    text = _deliver_text(job_id, str(quality["answer_text"]))
    if _text_hash(text) != str(row["deliver_text_hash"]):
        return {"state": "BLOCKED", "reason": "DELIVER text binding changed since prepare"}

    ttl = max(30, min(600, int(cfg.get("job_delivery_permit_ttl_seconds", 300))))
    con.execute(
        """
        UPDATE job_delivery_trials
        SET status='ARMED',approved_at=?,permit_expires_at=?,sender_did=?,detail=''
        WHERE room=? AND job_id=? AND content_hash=? AND status='PREPARED'
        """,
        (clock, clock + ttl, str(identity.did), row["room"], row["job_id"], row["content_hash"]),
    )
    con.commit()
    return {"state": "ARMED", "job_id": job_id, "did": str(identity.did), "text": text, "live": live, "ttl_seconds": ttl}


def _attempt(con: Any, *, room: str, job_id: str, content_hash: str, did: str, nonce: int, sig: str, text: str, status: str, detail: str = "", http_status: int | None = None, now: float | None = None) -> int:
    cur = con.execute(
        """
        INSERT INTO job_delivery_attempts(
          attempted_at,room,job_id,content_hash,did,nonce,sig,text,status,http_status,detail
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (time.time() if now is None else float(now), room, job_id, content_hash, did, str(nonce), sig, text, status, http_status, detail[:1000]),
    )
    return int(cur.lastrowid)


def _finish_attempt(con: Any, attempt_id: int, status: str, *, detail: str = "", http_status: int | None = None) -> None:
    con.execute("UPDATE job_delivery_attempts SET status=?,http_status=?,detail=? WHERE id=?", (status, http_status, detail[:1000], int(attempt_id)))


def send_delivery(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    *,
    room: str = "kibble",
    readiness_checker: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    sender_factory: Callable[[dict[str, Any], Any], Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    ensure_delivery_schema(con)
    clock = time.time() if now is None else float(now)
    row = con.execute(
        "SELECT * FROM job_delivery_trials WHERE room=? AND job_id=? ORDER BY prepared_at DESC LIMIT 1",
        (str(room), str(job_id)),
    ).fetchone()
    if row is None or str(row["status"]) != "ARMED":
        return {"state": "BLOCKED", "reason": "job does not have an ARMED delivery permit"}
    if row["permit_expires_at"] is None or float(row["permit_expires_at"]) < clock:
        con.execute("UPDATE job_delivery_trials SET status='EXPIRED',detail='permit TTL expired' WHERE room=? AND job_id=? AND content_hash=?", (row["room"], row["job_id"], row["content_hash"]))
        con.commit()
        return {"state": "BLOCKED", "reason": "one-use delivery permit expired; prepare and approve again"}

    claim, quality, reason = eligible_delivery(con, cfg, job_id, room=room)
    if claim is None or quality is None:
        return {"state": "BLOCKED", "reason": reason}
    if str(claim["content_hash"]) != str(row["content_hash"]) or int(claim["sent_seq"]) != int(row["claim_seq"]):
        return {"state": "BLOCKED", "reason": "claim binding changed since delivery approval"}
    if str(quality["answer_hash"]) != str(row["answer_hash"]):
        return {"state": "BLOCKED", "reason": "quality answer changed since delivery approval"}

    check = readiness_checker or live_delivery_ready
    live = check(cfg, claim)
    if live.get("state") != "READY_CONFIRMED":
        con.execute("UPDATE job_delivery_trials SET status='BLOCKED',detail=? WHERE room=? AND job_id=? AND content_hash=?", (f"send-time live check: {live.get('state','UNKNOWN')}", row["room"], row["job_id"], row["content_hash"]))
        con.commit()
        return {"state": "BLOCKED", "reason": f"send-time live delivery check failed: {live.get('state','UNKNOWN')}", "live": live}

    make_sender = sender_factory or (lambda config, db: ApprovedDraftSender(config, db))
    sender = make_sender(cfg, con)
    if str(sender.identity.did) != str(row["sender_did"]) or str(sender.identity.did) != str(claim["sender_did"]):
        return {"state": "BLOCKED", "reason": "signing identity changed since delivery approval"}

    text = _deliver_text(job_id, str(quality["answer_text"]))
    if _text_hash(text) != str(row["deliver_text_hash"]):
        return {"state": "BLOCKED", "reason": "DELIVER text binding changed since delivery approval"}

    server_nonce = int(sender._read_server_nonce(str(row["room"])))
    consumed = con.execute(
        """
        UPDATE job_delivery_trials
        SET status='RESERVED',consumed_at=?,detail='permit consumed before POST'
        WHERE room=? AND job_id=? AND content_hash=?
          AND status='ARMED' AND permit_expires_at>=?
        """,
        (clock, row["room"], row["job_id"], row["content_hash"], clock),
    )
    if consumed.rowcount != 1:
        con.rollback()
        return {"state": "BLOCKED", "reason": "one-use delivery permit was already consumed or expired"}

    nonce = reserve_send_nonce(con, str(sender.identity.did), str(row["room"]), server_nonce=server_nonce, floor=time.time_ns())
    signature = sender.identity.sign(str(row["room"]), nonce, text)
    attempt_id = _attempt(
        con, room=str(row["room"]), job_id=str(row["job_id"]), content_hash=str(row["content_hash"]),
        did=str(sender.identity.did), nonce=nonce, sig=signature, text=text, status="reserved", now=clock,
    )
    con.commit()

    try:
        record = sender._post(str(row["room"]), text, nonce, signature)
    except SendRefused as exc:
        _finish_attempt(con, attempt_id, "refused", detail=exc.body, http_status=exc.status)
        con.execute("UPDATE job_delivery_trials SET status='REFUSED',detail=? WHERE room=? AND job_id=? AND content_hash=?", (f"HTTP {exc.status}: {exc.body[:500]}", row["room"], row["job_id"], row["content_hash"]))
        con.commit()
        raise
    except SendUncertain as exc:
        _finish_attempt(con, attempt_id, "uncertain", detail=str(exc))
        con.execute("UPDATE job_delivery_trials SET status='UNCERTAIN',detail=? WHERE room=? AND job_id=? AND content_hash=?", (str(exc)[:500], row["room"], row["job_id"], row["content_hash"]))
        con.commit()
        raise

    seq = record.get("seq")
    _finish_attempt(con, attempt_id, "sent", detail=f"seq={seq}", http_status=200)
    con.execute("UPDATE job_delivery_trials SET status='SENT',sent_seq=?,detail='signed DELIVER confirmed in HTTP 200 response' WHERE room=? AND job_id=? AND content_hash=?", (seq, row["room"], row["job_id"], row["content_hash"]))
    con.commit()
    return {"state": "SENT", "job_id": job_id, "seq": seq, "room": str(row["room"]), "did": str(sender.identity.did), "text": text, "live": live}


def cancel_delivery(con: Any, job_id: str, *, room: str = "kibble") -> bool:
    ensure_delivery_schema(con)
    cur = con.execute(
        """
        UPDATE job_delivery_trials SET status='CANCELLED',detail='cancelled by human operator'
        WHERE room=? AND job_id=? AND status IN ('PREPARED','ARMED')
        """,
        (str(room), str(job_id)),
    )
    con.commit()
    return cur.rowcount > 0


def status_rows(con: Any, limit: int = 20) -> list[Any]:
    ensure_delivery_schema(con)
    return con.execute("SELECT * FROM job_delivery_trials ORDER BY prepared_at DESC LIMIT ?", (max(1, min(100, int(limit))),)).fetchall()


def main() -> None:
    parser = argparse.ArgumentParser(description="Human-approved one-job Kibble DELIVER trial")
    parser.add_argument("--config", default="technoscout.config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "approve", "send", "cancel"):
        p = sub.add_parser(name)
        p.add_argument("job_id")
        p.add_argument("--room", default="kibble")
    p_status = sub.add_parser("status")
    p_status.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    cfg = load_config(args.config)
    con = connect(database_path(cfg))
    ensure_delivery_schema(con)
    try:
        if args.command == "prepare":
            result = prepare_delivery(con, cfg, args.job_id, room=args.room)
            print(f"Job Delivery Trial | state={result['state']} job={args.job_id}")
            if result["state"] == "PREPARED":
                print(f"quality={result['quality']['decision']} confidence={result['quality']['confidence']}")
                print("DELIVER PREVIEW — exact one-line message; nothing has been sent")
                print(result["text"])
                print(f"prepared_ttl={result['ttl_seconds']}s")
                print(f"Next, only if you personally approve this exact DELIVER: .venv/bin/python job_delivery_trial.py approve {args.job_id}")
            else:
                print(f"reason={result['reason']}")
            return

        if args.command == "approve":
            result = approve_delivery(con, cfg, args.job_id, room=args.room)
            print(f"Job Delivery Trial | state={result['state']} job={args.job_id}")
            if result["state"] == "ARMED":
                print(f"DELIVER ARMED: {result['text']}")
                print(f"permit_ttl={result['ttl_seconds']}s sender={result['did']}")
                print(f"No message has been sent. To consume the one-use permit: .venv/bin/python job_delivery_trial.py send {args.job_id}")
            else:
                print(f"reason={result['reason']}")
            return

        if args.command == "send":
            result = send_delivery(con, cfg, args.job_id, room=args.room)
            print(f"Job Delivery Trial | state={result['state']} job={args.job_id}")
            if result["state"] == "SENT":
                print(f"room={result['room']} seq={result['seq']} sender={result['did']}")
                print(f"text={result['text']}")
                print("STOP: DELIVER sent. Do not send another DELIVER. Wait for lifecycle/ATTEST observation.")
            else:
                print(f"reason={result['reason']}")
            return

        if args.command == "cancel":
            changed = cancel_delivery(con, args.job_id, room=args.room)
            print(f"Job Delivery Trial | {'CANCELLED' if changed else 'NO_ACTIVE_PERMIT'} job={args.job_id}")
            return

        rows = status_rows(con, args.limit)
        print(f"Job Delivery Trial | rows={len(rows)}")
        for row in rows:
            print(
                f"  {row['job_id']} status={row['status']} quality={row['quality_decision']} "
                f"conf={row['quality_confidence']} claim_seq={row['claim_seq']} "
                f"sender={row['sender_did'] or '-'} sent_seq={row['sent_seq'] if row['sent_seq'] is not None else '-'}"
            )
            if row["detail"]:
                print(f"    detail={row['detail']}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
