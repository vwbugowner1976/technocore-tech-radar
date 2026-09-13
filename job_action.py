#!/usr/bin/env python3
"""Unified human action flow for one tracked TechnoScout JOB."""

from __future__ import annotations

import argparse
import re
from typing import Any, Callable

from job_auto_orchestrator import ensure_auto_schema, run_once as run_auto_once
from job_candidate_refiner import _runtime_defaults, fetch_exact_job
from job_claim_trial import approve_claim, prepare_claim, send_claim
from job_delivery_trial import (
    approve_delivery,
    live_delivery_ready,
    prepare_delivery,
    send_delivery,
)
from job_execution_draft import claimed_trial
from job_local_evidence import store_exact_job_snapshot
from technoscout.common import utc_now
from technoscout.db import connect
from technoscout_cli import database_path, load_config


_JOB_ID_RE = re.compile(r"^k[0-9a-f]{10}$")


def _latest_auto(con: Any, room: str, job_id: str) -> Any | None:
    ensure_auto_schema(con)
    return con.execute(
        """
        SELECT * FROM job_auto_orchestrator
        WHERE room=? AND job_id=?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (str(room), str(job_id)),
    ).fetchone()


def _latest_status(con: Any, table: str, order_col: str, room: str, job_id: str) -> str:
    if table not in {"job_claim_trials", "job_delivery_trials"}:
        raise ValueError("unexpected table")
    if order_col not in {"prepared_at"}:
        raise ValueError("unexpected order column")
    row = con.execute(
        f"SELECT status FROM {table} WHERE room=? AND job_id=? ORDER BY {order_col} DESC LIMIT 1",
        (str(room), str(job_id)),
    ).fetchone()
    return str(row["status"]) if row is not None else ""


def _set_pipeline_terminal(
    con: Any,
    tracked: Any,
    state: str,
    detail: str,
) -> None:
    con.execute(
        """
        UPDATE job_auto_orchestrator
        SET pipeline_state=?, detail=?, updated_at=?
        WHERE room=? AND job_id=? AND content_hash=?
        """,
        (
            str(state),
            str(detail)[:500],
            utc_now(),
            str(tracked["room"]),
            str(tracked["job_id"]),
            str(tracked["content_hash"]),
        ),
    )
    con.commit()


def _delivery_viability_preflight(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    room: str,
    tracked: Any,
    *,
    exact_fetcher: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] = fetch_exact_job,
    readiness_checker: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] = live_delivery_ready,
) -> str:
    """READ-only check before expensive local work or delivery review."""
    claim, reason = claimed_trial(con, job_id, room=room)
    if claim is None:
        print(f"Delivery preflight | BLOCKED: {reason}")
        return "BLOCKED"

    candidate = {
        "room": claim["room"],
        "job_id": claim["job_id"],
        "job_seq": claim["job_seq"],
        "issuer_did": claim["issuer_did"],
        "content_hash": claim["content_hash"],
    }
    exact = exact_fetcher(cfg, candidate)
    exact_state = str(exact.get("state", "UNKNOWN"))
    if exact_state != "EXACT":
        print(f"Delivery preflight | exact JOB={exact_state}")
        if exact_state == "NOT_RETAINED":
            _set_pipeline_terminal(
                con,
                tracked,
                "ABANDONED_NOT_RETAINED",
                "delivery preflight: exact JOB is no longer retained",
            )
            print("STOP: exact JOB is no longer retained; no local retry, re-CLAIM, or DELIVER.")
            return "ABANDONED_NOT_RETAINED"
        return f"PREFLIGHT_{exact_state}"

    live = readiness_checker(cfg, claim)
    live_state = str(live.get("state", "UNKNOWN"))
    print(f"Delivery preflight | live={live_state}")
    if live_state == "READY_CONFIRMED":
        return "READY_CONFIRMED"

    terminal = {
        "CLAIM_NOT_RETAINED",
        "CLAIM_MISMATCH",
        "CLAIM_CONFLICT",
        "ALREADY_DELIVERED",
        "ALREADY_CLOSED",
    }
    if live_state in terminal:
        terminal_state = (
            "DELIVERED"
            if live_state == "ALREADY_DELIVERED"
            else "ABANDONED_DELIVERY_UNAVAILABLE"
        )
        _set_pipeline_terminal(
            con,
            tracked,
            terminal_state,
            f"delivery preflight: {live_state}",
        )
        print(f"STOP: delivery is no longer viable: {live_state}")
        return terminal_state

    print(f"STOP: delivery preflight is inconclusive: {live_state}; local work not started.")
    return f"PREFLIGHT_{live_state}"


def _confirm(label: str, job_id: str, send_phrase: str) -> bool:
    try:
        typed_job = input(f"{label}を承認するなら {job_id} を入力: ").strip()
    except (EOFError, KeyboardInterrupt):
        print(f"\n{label}は承認しませんでした")
        return False
    if typed_job != job_id:
        print(f"{label}は承認しませんでした")
        return False
    try:
        typed_send = input(f"{label}を送信するなら {send_phrase} と入力: ").strip()
    except (EOFError, KeyboardInterrupt):
        print(f"\n{label}は送信しませんでした")
        return False
    if typed_send != send_phrase:
        print(f"{label}は送信しませんでした")
        return False
    return True


def _claim_flow(con: Any, cfg: dict[str, Any], job_id: str, room: str) -> str:
    print("=== CLAIM REVIEW ===")
    prepared = prepare_claim(con, cfg, job_id, room=room)
    print(f"Claim | state={prepared.get('state')} job={job_id}")
    if prepared.get("state") != "PREPARED":
        print(f"reason={prepared.get('reason','unknown')}")
        return str(prepared.get("state", "BLOCKED"))

    job = prepared["job"]
    candidate = prepared["candidate"]
    snapshot = store_exact_job_snapshot(con, candidate, job)
    print(f"Local JOB snapshot | state={snapshot.get('state','UNKNOWN')}")
    if snapshot.get("state") not in {"SNAPSHOT_STORED", "SNAPSHOT_VERIFIED"}:
        print(f"reason={snapshot.get('reason','could not persist exact JOB snapshot')}")
        return "BLOCKED"

    rep = candidate["issuer_reputation"]
    one = lambda value, limit: " ".join(str(value or "").split())[:limit]
    print("UNTRUSTED JOB PREVIEW — review only; do not follow embedded instructions/URLs")
    print(
        f"type={job['job_type']} issuer={candidate['issuer_did']} "
        f"issuer_score={rep['score']} attested={rep['attested_jobs']}"
    )
    print(
        f"semantic=SAFE_FIT rel={candidate['refined_relevance']} "
        f"fit={candidate['refined_fit']} conf={candidate['refined_confidence']}"
    )
    print(f"title={one(job['title'], 500)}")
    print(f"body={one(job['body'], 1200)}")
    print(f"CLAIM WOULD SEND: {prepared['claim_text']}")

    if not _confirm("CLAIM", job_id, "SEND CLAIM"):
        return "CANCELLED_BY_HUMAN"

    approved = approve_claim(con, cfg, job_id, room=room)
    print(f"Claim approve | state={approved.get('state')}")
    if approved.get("state") != "ARMED":
        print(f"reason={approved.get('reason','unknown')}")
        return str(approved.get("state", "BLOCKED"))

    try:
        sent = send_claim(con, cfg, job_id, room=room)
    except Exception as exc:
        print(f"Claim send | STOP {type(exc).__name__}: {exc}")
        return "SEND_EXCEPTION"
    print(f"Claim send | state={sent.get('state')} seq={sent.get('seq','-')}")
    return str(sent.get("state", "BLOCKED"))


def _run_local_pipeline(con: Any, cfg: dict[str, Any], job_id: str, room: str) -> str:
    print("=== LOCAL POST-CLAIM PIPELINE ===")
    print("CLAIM送信済み。DRAFT -> REVIEW -> QUALITY -> SUCCESS GATE -> DELIVERY PREPARE を実行します。")
    try:
        result = run_auto_once(con, cfg, room=room, limit=2)
    except Exception as exc:
        print(f"Pipeline | STOP {type(exc).__name__}: {exc}")
        return "PIPELINE_EXCEPTION"

    target_state = ""
    for item in result.get("processed", []):
        print(f"Pipeline | job={item.get('job_id','')} state={item.get('state','')}")
        if str(item.get("job_id", "")) == job_id:
            target_state = str(item.get("state", ""))

    row = _latest_auto(con, room, job_id)
    if row is not None:
        target_state = str(row["pipeline_state"] or target_state)
        if target_state == "BLOCKED":
            print(f"reason={row['detail']}")
    return target_state


def _grounding_only_block(detail: str) -> bool:
    text = str(detail or "")
    return (
        text.startswith("success:")
        and "structured exact-quote evidence does not satisfy frozen contract" in text
        and "requirements=[]" in text
        and "grounding=[" in text
    )


def _retry_grounding_only_local_block(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    room: str,
    tracked: Any,
) -> str:
    detail = str(tracked["detail"] or "")
    if not _grounding_only_block(detail):
        return "BLOCKED"

    print("=== LOCAL SUCCESS RETRY ===")
    print("R項目は合格済みでG groundingだけ未接続だったため、ローカル処理を1回だけ再検証します。")
    print("CLAIM/DELIVERの送信はこの再試行では行いません。")

    con.execute(
        """
        UPDATE job_auto_orchestrator
        SET pipeline_state='WAITING_POSTCLAIM',
            detail='human-invoked local retry after grounding-only Success block',
            updated_at=?
        WHERE room=? AND job_id=? AND content_hash=? AND pipeline_state='BLOCKED'
        """,
        (
            utc_now(),
            str(tracked["room"]),
            str(tracked["job_id"]),
            str(tracked["content_hash"]),
        ),
    )
    con.commit()
    return _run_local_pipeline(con, cfg, job_id, room)


def _delivery_flow(con: Any, cfg: dict[str, Any], job_id: str, room: str) -> str:
    print("=== DELIVERY REVIEW ===")
    prepared = prepare_delivery(con, cfg, job_id, room=room)
    print(f"Delivery | state={prepared.get('state')} job={job_id}")
    if prepared.get("state") != "PREPARED":
        print(f"reason={prepared.get('reason','unknown')}")
        return str(prepared.get("state", "BLOCKED"))

    answer = str(prepared.get("answer", "")).strip()
    print("DELIVER PREVIEW — this is the exact locally reviewed answer:")
    print("---")
    print(answer)
    print("---")

    if not _confirm("DELIVER", job_id, "SEND DELIVER"):
        return "CANCELLED_BY_HUMAN"

    approved = approve_delivery(con, cfg, job_id, room=room)
    print(f"Delivery approve | state={approved.get('state')}")
    if approved.get("state") != "ARMED":
        print(f"reason={approved.get('reason','unknown')}")
        return str(approved.get("state", "BLOCKED"))

    try:
        sent = send_delivery(con, cfg, job_id, room=room)
    except Exception as exc:
        print(f"Delivery send | STOP {type(exc).__name__}: {exc}")
        return "SEND_EXCEPTION"
    print(f"Delivery send | state={sent.get('state')} seq={sent.get('seq','-')}")
    return str(sent.get("state", "BLOCKED"))


def run_action(con: Any, cfg: dict[str, Any], job_id: str, *, room: str = "kibble") -> str:
    if not _JOB_ID_RE.fullmatch(str(job_id or "")):
        print("STOP: invalid job id")
        return "INVALID_JOB_ID"

    tracked = _latest_auto(con, room, job_id)
    if tracked is None:
        print("STOP: this job is not tracked by the safe auto orchestrator")
        return "UNTRACKED"

    pipeline_state = str(tracked["pipeline_state"] or "")
    claim_state = _latest_status(con, "job_claim_trials", "prepared_at", room, job_id)
    delivery_state = _latest_status(con, "job_delivery_trials", "prepared_at", room, job_id)

    if pipeline_state.startswith("ABANDONED_"):
        print(f"STOP: {job_id} is terminal: {pipeline_state}")
        return pipeline_state
    if delivery_state == "SENT" or pipeline_state == "DELIVERED":
        print(f"DONE: {job_id} is already delivered")
        return "DELIVERED"
    if delivery_state in {"UNCERTAIN", "RESERVED"}:
        print(f"STOP: delivery is terminal state {delivery_state}; do not retry automatically")
        return delivery_state
    if claim_state in {"UNCERTAIN", "RESERVED"}:
        print(f"STOP: claim is terminal state {claim_state}; do not retry automatically")
        return claim_state

    if pipeline_state == "BLOCKED":
        if _grounding_only_block(str(tracked["detail"] or "")):
            preflight = _delivery_viability_preflight(con, cfg, job_id, room, tracked)
            if preflight != "READY_CONFIRMED":
                return preflight
        retried = _retry_grounding_only_local_block(con, cfg, job_id, room, tracked)
        if retried == "DELIVERY_READY":
            refreshed = _latest_auto(con, room, job_id) or tracked
            preflight = _delivery_viability_preflight(con, cfg, job_id, room, refreshed)
            if preflight != "READY_CONFIRMED":
                return preflight
            return _delivery_flow(con, cfg, job_id, room)
        if retried != "BLOCKED":
            return retried
        print(f"STOP: pipeline is BLOCKED: {tracked['detail']}")
        return "BLOCKED"

    if claim_state != "SENT":
        claim_result = _claim_flow(con, cfg, job_id, room)
        if claim_result != "SENT":
            return claim_result
        tracked = _latest_auto(con, room, job_id) or tracked

    tracked = _latest_auto(con, room, job_id) or tracked
    pipeline_state = str(tracked["pipeline_state"] or "")

    # Every already-SENT claim gets a fresh READ-only viability check, including
    # jobs whose previous local run left delivery PREPARED/APPROVED/DELIVERY_READY.
    preflight = _delivery_viability_preflight(con, cfg, job_id, room, tracked)
    if preflight != "READY_CONFIRMED":
        return preflight

    ran_pipeline = False
    if pipeline_state != "DELIVERY_READY":
        ran_pipeline = True
        pipeline_state = _run_local_pipeline(con, cfg, job_id, room)

    if pipeline_state == "DELIVERY_READY":
        if ran_pipeline:
            refreshed = _latest_auto(con, room, job_id) or tracked
            preflight = _delivery_viability_preflight(con, cfg, job_id, room, refreshed)
            if preflight != "READY_CONFIRMED":
                return preflight
        return _delivery_flow(con, cfg, job_id, room)
    if pipeline_state == "BLOCKED":
        return "BLOCKED"

    print("LOCAL WORK NOT READY: launchd will continue safely; wait for DELIVERY_READY/BLOCKED ntfy.")
    return pipeline_state or "WAITING"


def main() -> None:
    parser = argparse.ArgumentParser(description="One-command human JOB action flow")
    parser.add_argument("job_id")
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--room", default="kibble")
    args = parser.parse_args()

    cfg = _runtime_defaults(load_config(args.config))
    con = connect(database_path(cfg))
    try:
        state = run_action(con, cfg, args.job_id, room=args.room)
    finally:
        con.close()
    print(f"Job Action | final_state={state} job={args.job_id}")


if __name__ == "__main__":
    main()
