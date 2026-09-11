#!/usr/bin/env python3
"""Human-approved, one-job Kibble CLAIM trial.

This is deliberately separate from TechnoScout chat autonomy and Job Shadow.
Nothing here auto-selects, auto-claims, executes a job, delivers work, spends
FLOP/tokens, or touches wallets. A CLAIM requires three explicit local steps:
prepare -> approve -> send.

Safety properties:
- exact job_id / seq / issuer DID / content hash binding
- recent SAFE_FIT semantic refinement and issuer-evidence thresholds
- export-aware live OPEN revalidation at prepare, approve, and send
- short prepared and armed TTLs
- one-use DB permit consumed before the POST
- existing hardened Ed25519/nonce/POST transport reused from technoscout.sender
- uncertain POST outcome is terminal; never retry automatically
- raw JOB title/body is printed transiently for human review but not stored here
"""

from __future__ import annotations

import argparse
import hashlib
import time
from datetime import datetime, timezone
from typing import Any, Callable

from issuer_reputation import issuer_reputation
from job_candidate_refiner import ensure_refiner_schema, fetch_exact_job
from job_live_revalidator import live_revalidate_job_export_aware
from job_progress_gate import _issuer_meets_gate, _thresholds
from job_refined_gate import _refined_thresholds
from job_shadow import ensure_job_shadow_schema
from technoscout.db import connect, reserve_send_nonce
from technoscout.sender import (
    ApprovedDraftSender,
    SendRefused,
    SendUncertain,
    SigningIdentity,
    sweep_text,
)
from technoscout_cli import database_path, load_config


CLAIM_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_claim_trials (
    room TEXT NOT NULL,
    job_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    job_seq INTEGER NOT NULL,
    issuer_did TEXT NOT NULL,
    job_type TEXT NOT NULL,
    refined_at TEXT NOT NULL,
    refined_relevance INTEGER NOT NULL,
    refined_fit INTEGER NOT NULL,
    refined_confidence INTEGER NOT NULL,
    prepared_at REAL NOT NULL,
    prepare_expires_at REAL NOT NULL,
    approved_at REAL,
    permit_expires_at REAL,
    consumed_at REAL,
    sender_did TEXT NOT NULL DEFAULT '',
    claim_text_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    sent_seq INTEGER,
    detail TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(room, job_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_job_claim_trials_status
    ON job_claim_trials(status, prepared_at DESC);

CREATE TABLE IF NOT EXISTS job_claim_attempts (
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
CREATE INDEX IF NOT EXISTS idx_job_claim_attempts_job
    ON job_claim_attempts(room, job_id, id DESC);
"""

TERMINAL_TRIAL_STATES = {"SENT", "UNCERTAIN", "RESERVED"}


def ensure_claim_schema(con: Any) -> None:
    con.executescript(CLAIM_SCHEMA)


def _iso_to_epoch(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _claim_text(job_id: str) -> str:
    return sweep_text(f"CLAIM v1 | {job_id} | worker")


def _text_hash(text: str) -> str:
    return hashlib.sha256(("technoscout-job-claim-v1\0" + text).encode("utf-8")).hexdigest()


def candidate_for_claim(con: Any, cfg: dict[str, Any], job_id: str, *, room: str = "kibble") -> tuple[dict[str, Any] | None, str]:
    """Return a fresh SAFE_FIT candidate that still passes all local evidence gates."""
    ensure_job_shadow_schema(con)
    ensure_refiner_schema(con)
    row = con.execute(
        """
        SELECT j.room,j.job_id,j.job_seq,j.issuer_did,j.signed_identity,
               j.job_type,j.content_hash,j.lifecycle,j.fit_class AS deterministic_class,
               r.refined_at,r.decision,r.relevance AS refined_relevance,
               r.technical_fit AS refined_fit,r.confidence AS refined_confidence,
               r.effort AS refined_effort,r.reason AS refined_reason
        FROM job_shadow_candidates AS j
        JOIN job_candidate_refinements AS r
          ON r.room=j.room AND r.job_id=j.job_id AND r.content_hash=j.content_hash
        WHERE j.room=? AND j.job_id=?
        LIMIT 1
        """,
        (str(room), str(job_id)),
    ).fetchone()
    if row is None:
        return None, "no exact Job Shadow + refinement row exists"

    item = {key: row[key] for key in row.keys()}
    if str(item["lifecycle"]) != "OPEN":
        return None, f"persisted lifecycle is {item['lifecycle']}, not OPEN"
    if int(item["signed_identity"] or 0) != 1 or not str(item["issuer_did"]):
        return None, "issuer is not a persisted signed DID"
    if str(item["deterministic_class"]) not in {"FIT", "NOT_RELEVANT"}:
        return None, f"deterministic class {item['deterministic_class']} is not claim-trial eligible"
    if str(item["decision"]) != "SAFE_FIT":
        return None, f"semantic refinement is {item['decision']}, not SAFE_FIT"

    refined = _refined_thresholds(cfg)
    if int(item["refined_relevance"]) < refined["relevance"]:
        return None, "refined relevance is below threshold"
    if int(item["refined_fit"]) < refined["technical_fit"]:
        return None, "refined technical fit is below threshold"
    if int(item["refined_confidence"]) < refined["confidence"]:
        return None, "refined confidence is below threshold"

    max_age = max(60, min(3600, int(cfg.get("job_claim_max_refinement_age_seconds", 900))))
    refined_epoch = _iso_to_epoch(str(item["refined_at"]))
    if refined_epoch <= 0 or time.time() - refined_epoch > max_age:
        return None, f"SAFE_FIT refinement is older than {max_age}s"

    rep = issuer_reputation(con, str(item["issuer_did"]))
    if not _issuer_meets_gate(rep, _thresholds(cfg)):
        return None, "issuer no longer meets Job Progress Gate evidence thresholds"
    item["issuer_reputation"] = rep
    return item, "eligible"


def prepare_claim(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    *,
    room: str = "kibble",
    revalidator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    exact_fetcher: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Prepare one exact candidate for human review; never sends anything."""
    ensure_claim_schema(con)
    candidate, reason = candidate_for_claim(con, cfg, job_id, room=room)
    if candidate is None:
        return {"state": "BLOCKED", "reason": reason}

    check_live = revalidator or live_revalidate_job_export_aware
    live = check_live(cfg, candidate)
    if live.get("state") != "OPEN_CONFIRMED":
        return {"state": "BLOCKED", "reason": f"live OPEN check failed: {live.get('state','UNKNOWN')}", "live": live}

    exact = exact_fetcher(cfg, candidate) if exact_fetcher is not None else fetch_exact_job(cfg, candidate)
    if exact.get("state") != "EXACT":
        return {"state": "BLOCKED", "reason": f"exact JOB fetch failed: {exact.get('state','UNKNOWN')}", "live": live}

    existing = con.execute(
        "SELECT status FROM job_claim_trials WHERE room=? AND job_id=? AND content_hash=?",
        (candidate["room"], candidate["job_id"], candidate["content_hash"]),
    ).fetchone()
    if existing is not None and str(existing["status"]) in TERMINAL_TRIAL_STATES:
        return {"state": "BLOCKED", "reason": f"existing claim trial is {existing['status']}; automatic re-arm is forbidden", "live": live}

    clock = time.time() if now is None else float(now)
    ttl = max(60, min(1800, int(cfg.get("job_claim_prepare_ttl_seconds", 600))))
    claim_text = _claim_text(candidate["job_id"])
    con.execute(
        """
        INSERT INTO job_claim_trials(
          room,job_id,content_hash,job_seq,issuer_did,job_type,refined_at,
          refined_relevance,refined_fit,refined_confidence,prepared_at,
          prepare_expires_at,sender_did,claim_text_hash,status,detail
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(room,job_id,content_hash) DO UPDATE SET
          job_seq=excluded.job_seq,
          issuer_did=excluded.issuer_did,
          job_type=excluded.job_type,
          refined_at=excluded.refined_at,
          refined_relevance=excluded.refined_relevance,
          refined_fit=excluded.refined_fit,
          refined_confidence=excluded.refined_confidence,
          prepared_at=excluded.prepared_at,
          prepare_expires_at=excluded.prepare_expires_at,
          approved_at=NULL,
          permit_expires_at=NULL,
          consumed_at=NULL,
          sender_did='',
          claim_text_hash=excluded.claim_text_hash,
          status='PREPARED',
          sent_seq=NULL,
          detail=''
        """,
        (
            candidate["room"], candidate["job_id"], candidate["content_hash"],
            int(candidate["job_seq"]), candidate["issuer_did"], candidate["job_type"],
            candidate["refined_at"], int(candidate["refined_relevance"]),
            int(candidate["refined_fit"]), int(candidate["refined_confidence"]),
            clock, clock + ttl, "", _text_hash(claim_text), "PREPARED", "",
        ),
    )
    con.commit()
    return {
        "state": "PREPARED",
        "candidate": candidate,
        "job": exact["job"],
        "live": live,
        "claim_text": claim_text,
        "ttl_seconds": ttl,
        "expires_at": clock + ttl,
    }


def approve_claim(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    *,
    room: str = "kibble",
    revalidator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    identity_factory: Callable[[dict[str, Any]], Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Arm one short-lived DB permit after another exact live OPEN check."""
    ensure_claim_schema(con)
    clock = time.time() if now is None else float(now)
    trial = con.execute(
        "SELECT * FROM job_claim_trials WHERE room=? AND job_id=? ORDER BY prepared_at DESC LIMIT 1",
        (str(room), str(job_id)),
    ).fetchone()
    if trial is None or str(trial["status"]) != "PREPARED":
        return {"state": "BLOCKED", "reason": "job is not in PREPARED state"}
    if float(trial["prepare_expires_at"]) < clock:
        con.execute("UPDATE job_claim_trials SET status='EXPIRED',detail='prepare TTL expired' WHERE room=? AND job_id=? AND content_hash=?", (trial["room"], trial["job_id"], trial["content_hash"]))
        con.commit()
        return {"state": "BLOCKED", "reason": "prepared review expired; prepare again"}

    candidate, reason = candidate_for_claim(con, cfg, job_id, room=room)
    if candidate is None:
        return {"state": "BLOCKED", "reason": reason}
    if str(candidate["content_hash"]) != str(trial["content_hash"]) or int(candidate["job_seq"]) != int(trial["job_seq"]):
        return {"state": "BLOCKED", "reason": "candidate binding changed since prepare"}

    check_live = revalidator or live_revalidate_job_export_aware
    live = check_live(cfg, candidate)
    if live.get("state") != "OPEN_CONFIRMED":
        return {"state": "BLOCKED", "reason": f"live OPEN check failed: {live.get('state','UNKNOWN')}", "live": live}

    make_identity = identity_factory or (
        lambda config: SigningIdentity.from_env(
            str(config.get("signing_seed_env", "SIGN_SEED")),
            str(config.get("signing_env_file", ".env")),
        )
    )
    identity = make_identity(cfg)
    if str(identity.did) == str(candidate["issuer_did"]):
        return {"state": "BLOCKED", "reason": "refusing to claim our own issued job"}

    permit_ttl = max(30, min(600, int(cfg.get("job_claim_permit_ttl_seconds", 300))))
    claim_text = _claim_text(job_id)
    con.execute(
        """
        UPDATE job_claim_trials
        SET status='ARMED',approved_at=?,permit_expires_at=?,sender_did=?,claim_text_hash=?,detail=''
        WHERE room=? AND job_id=? AND content_hash=? AND status='PREPARED'
        """,
        (
            clock, clock + permit_ttl, str(identity.did), _text_hash(claim_text),
            trial["room"], trial["job_id"], trial["content_hash"],
        ),
    )
    con.commit()
    return {"state": "ARMED", "job_id": job_id, "did": str(identity.did), "live": live, "claim_text": claim_text, "ttl_seconds": permit_ttl, "expires_at": clock + permit_ttl}


def _attempt(con: Any, *, room: str, job_id: str, content_hash: str, did: str, nonce: int, sig: str, text: str, status: str, detail: str = "", http_status: int | None = None, now: float | None = None) -> int:
    cur = con.execute(
        """
        INSERT INTO job_claim_attempts(
          attempted_at,room,job_id,content_hash,did,nonce,sig,text,status,http_status,detail
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (time.time() if now is None else float(now), room, job_id, content_hash, did, str(nonce), sig, text, status, http_status, detail[:1000]),
    )
    return int(cur.lastrowid)


def _finish_attempt(con: Any, attempt_id: int, status: str, *, detail: str = "", http_status: int | None = None) -> None:
    con.execute(
        "UPDATE job_claim_attempts SET status=?,http_status=?,detail=? WHERE id=?",
        (status, http_status, detail[:1000], int(attempt_id)),
    )


def send_claim(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    *,
    room: str = "kibble",
    revalidator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    sender_factory: Callable[[dict[str, Any], Any], Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Consume one permit and POST exactly one signed CLAIM. Never auto-retries."""
    ensure_claim_schema(con)
    clock = time.time() if now is None else float(now)
    trial = con.execute(
        "SELECT * FROM job_claim_trials WHERE room=? AND job_id=? ORDER BY prepared_at DESC LIMIT 1",
        (str(room), str(job_id)),
    ).fetchone()
    if trial is None or str(trial["status"]) != "ARMED":
        return {"state": "BLOCKED", "reason": "job does not have an ARMED one-use permit"}
    if trial["permit_expires_at"] is None or float(trial["permit_expires_at"]) < clock:
        con.execute("UPDATE job_claim_trials SET status='EXPIRED',detail='permit TTL expired' WHERE room=? AND job_id=? AND content_hash=?", (trial["room"], trial["job_id"], trial["content_hash"]))
        con.commit()
        return {"state": "BLOCKED", "reason": "one-use permit expired; prepare and approve again"}

    candidate, reason = candidate_for_claim(con, cfg, job_id, room=room)
    if candidate is None:
        return {"state": "BLOCKED", "reason": reason}
    if str(candidate["content_hash"]) != str(trial["content_hash"]) or int(candidate["job_seq"]) != int(trial["job_seq"]):
        return {"state": "BLOCKED", "reason": "candidate binding changed since approval"}

    check_live = revalidator or live_revalidate_job_export_aware
    live = check_live(cfg, candidate)
    if live.get("state") != "OPEN_CONFIRMED":
        con.execute("UPDATE job_claim_trials SET status='BLOCKED',detail=? WHERE room=? AND job_id=? AND content_hash=?", (f"send-time live check: {live.get('state','UNKNOWN')}", trial["room"], trial["job_id"], trial["content_hash"]))
        con.commit()
        return {"state": "BLOCKED", "reason": f"send-time live OPEN check failed: {live.get('state','UNKNOWN')}", "live": live}

    make_sender = sender_factory or (lambda config, db: ApprovedDraftSender(config, db))
    sender = make_sender(cfg, con)
    if str(sender.identity.did) != str(trial["sender_did"]):
        return {"state": "BLOCKED", "reason": "signing identity changed since approval"}

    text = _claim_text(job_id)
    if _text_hash(text) != str(trial["claim_text_hash"]):
        return {"state": "BLOCKED", "reason": "claim text binding changed since approval"}

    # GET-only nonce lookup happens before consuming the permit. After this point,
    # the permit is consumed before the POST so a crash/timeout cannot double-send.
    server_nonce = int(sender._read_server_nonce(str(trial["room"])))
    consumed = con.execute(
        """
        UPDATE job_claim_trials
        SET status='RESERVED',consumed_at=?,detail='permit consumed before POST'
        WHERE room=? AND job_id=? AND content_hash=?
          AND status='ARMED' AND permit_expires_at>=?
        """,
        (clock, trial["room"], trial["job_id"], trial["content_hash"], clock),
    )
    if consumed.rowcount != 1:
        con.rollback()
        return {"state": "BLOCKED", "reason": "one-use permit was already consumed or expired"}

    nonce = reserve_send_nonce(
        con,
        str(sender.identity.did),
        str(trial["room"]),
        server_nonce=server_nonce,
        floor=time.time_ns(),
    )
    signature = sender.identity.sign(str(trial["room"]), nonce, text)
    attempt_id = _attempt(
        con,
        room=str(trial["room"]),
        job_id=str(trial["job_id"]),
        content_hash=str(trial["content_hash"]),
        did=str(sender.identity.did),
        nonce=nonce,
        sig=signature,
        text=text,
        status="reserved",
        now=clock,
    )
    con.commit()

    try:
        record = sender._post(str(trial["room"]), text, nonce, signature)
    except SendRefused as exc:
        _finish_attempt(con, attempt_id, "refused", detail=exc.body, http_status=exc.status)
        con.execute("UPDATE job_claim_trials SET status='REFUSED',detail=? WHERE room=? AND job_id=? AND content_hash=?", (f"HTTP {exc.status}: {exc.body[:500]}", trial["room"], trial["job_id"], trial["content_hash"]))
        con.commit()
        raise
    except SendUncertain as exc:
        _finish_attempt(con, attempt_id, "uncertain", detail=str(exc))
        con.execute("UPDATE job_claim_trials SET status='UNCERTAIN',detail=? WHERE room=? AND job_id=? AND content_hash=?", (str(exc)[:500], trial["room"], trial["job_id"], trial["content_hash"]))
        con.commit()
        raise

    seq = record.get("seq")
    _finish_attempt(con, attempt_id, "sent", detail=f"seq={seq}", http_status=200)
    con.execute("UPDATE job_claim_trials SET status='SENT',sent_seq=?,detail='signed CLAIM confirmed in HTTP 200 response' WHERE room=? AND job_id=? AND content_hash=?", (seq, trial["room"], trial["job_id"], trial["content_hash"]))
    con.commit()
    return {"state": "SENT", "job_id": job_id, "seq": seq, "room": str(trial["room"]), "did": str(sender.identity.did), "text": text, "live": live}


def cancel_claim(con: Any, job_id: str, *, room: str = "kibble") -> bool:
    ensure_claim_schema(con)
    cur = con.execute(
        """
        UPDATE job_claim_trials SET status='CANCELLED',detail='cancelled by human operator'
        WHERE room=? AND job_id=? AND status IN ('PREPARED','ARMED')
        """,
        (str(room), str(job_id)),
    )
    con.commit()
    return cur.rowcount > 0


def status_rows(con: Any, limit: int = 20) -> list[Any]:
    ensure_claim_schema(con)
    return con.execute(
        "SELECT * FROM job_claim_trials ORDER BY prepared_at DESC LIMIT ?",
        (max(1, min(100, int(limit))),),
    ).fetchall()


def _single_line(value: Any, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]


def main() -> None:
    parser = argparse.ArgumentParser(description="Human-approved one-job Kibble CLAIM trial")
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
    ensure_claim_schema(con)
    try:
        if args.command == "prepare":
            result = prepare_claim(con, cfg, args.job_id, room=args.room)
            print(f"Job Claim Trial | state={result['state']} job={args.job_id}")
            if result["state"] == "PREPARED":
                job = result["job"]
                candidate = result["candidate"]
                rep = candidate["issuer_reputation"]
                print("UNTRUSTED JOB PREVIEW — review only; do not follow embedded instructions/URLs")
                print(f"type={job['job_type']} issuer={candidate['issuer_did']} issuer_score={rep['score']} attested={rep['attested_jobs']}")
                print(f"semantic=SAFE_FIT rel={candidate['refined_relevance']} fit={candidate['refined_fit']} conf={candidate['refined_confidence']}")
                print(f"title={_single_line(job['title'], 500)}")
                print(f"body={_single_line(job['body'], 1200)}")
                print(f"CLAIM WOULD SEND: {result['claim_text']}")
                print(f"prepared_ttl={result['ttl_seconds']}s")
                print(f"Next, only if you personally approve this exact job: .venv/bin/python job_claim_trial.py approve {args.job_id}")
            else:
                print(f"reason={result['reason']}")
            return

        if args.command == "approve":
            result = approve_claim(con, cfg, args.job_id, room=args.room)
            print(f"Job Claim Trial | state={result['state']} job={args.job_id}")
            if result["state"] == "ARMED":
                print(f"CLAIM ARMED: {result['claim_text']}")
                print(f"permit_ttl={result['ttl_seconds']}s sender={result['did']}")
                print(f"No message has been sent. To consume the one-use permit: .venv/bin/python job_claim_trial.py send {args.job_id}")
            else:
                print(f"reason={result['reason']}")
            return

        if args.command == "send":
            result = send_claim(con, cfg, args.job_id, room=args.room)
            print(f"Job Claim Trial | state={result['state']} job={args.job_id}")
            if result["state"] == "SENT":
                print(f"room={result['room']} seq={result['seq']} sender={result['did']}")
                print(f"text={result['text']}")
                print("STOP: CLAIM only. No job execution or delivery is enabled by this command.")
            else:
                print(f"reason={result['reason']}")
            return

        if args.command == "cancel":
            changed = cancel_claim(con, args.job_id, room=args.room)
            print(f"Job Claim Trial | {'CANCELLED' if changed else 'NO_ACTIVE_PERMIT'} job={args.job_id}")
            return

        rows = status_rows(con, args.limit)
        print(f"Job Claim Trial | rows={len(rows)}")
        for row in rows:
            print(
                f"  {row['job_id']} status={row['status']} type={row['job_type']} "
                f"rel={row['refined_relevance']} fit={row['refined_fit']} conf={row['refined_confidence']} "
                f"sender={row['sender_did'] or '-'} sent_seq={row['sent_seq'] if row['sent_seq'] is not None else '-'}"
            )
            if row["detail"]:
                print(f"    detail={row['detail']}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
