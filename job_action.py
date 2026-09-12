#!/usr/bin/env python3
"""Unified human action flow for one tracked TechnoScout JOB.

This command is the copy/paste entry point used by ntfy. It preserves the two
mandatory human approval boundaries while removing the need to remember separate
prepare/approve/send commands.

Flow:
  CLAIM_READY -> review -> explicit CLAIM approval -> signed CLAIM
              -> local post-claim pipeline (no signed write)
              -> review -> explicit DELIVER approval -> signed DELIVER

Safety properties:
- accepts only validated kXXXXXXXXXX job ids
- only operates on jobs already tracked by job_auto_orchestrator
- raw JOB content is review-only and never executed
- CLAIM and DELIVER each require exact job-id confirmation plus a distinct SEND phrase
- no automatic retry after signed-write uncertainty/terminal states
- a prior local Success-stage grounding-only BLOCK may be retried once on a new
  human invocation; this retry contains no signed write
- existing claim/delivery live revalidation and one-use permits remain authoritative
"""

from __future__ import annotations

import argparse
import re
from typing import Any

from job_auto_orchestrator import ensure_auto_schema, run_once as run_auto_once
from job_candidate_refiner import _runtime_defaults
from job_claim_trial import approve_claim, prepare_claim, send_claim
from job_delivery_trial import approve_delivery, prepare_delivery, send_delivery
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
        # Existing send code persists REFUSED/UNCERTAIN terminal state. Never retry here.
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
    """Re-arm only the local pipeline for a known grounding-only Success block.

    This never changes CLAIM/DELIVER trial status and never performs a signed
    write. The normal post-claim pipeline performs all retained-claim, exact-JOB,
    Success, quality, and delivery-readiness checks again.
    """
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
        # Existing send code persists REFUSED/UNCERTAIN terminal state. Never retry here.
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
        retried = _retry_grounding_only_local_block(con, cfg, job_id, room, tracked)
        if retried == "DELIVERY_READY":
            return _delivery_flow(con, cfg, job_id, room)
        if retried != "BLOCKED":
            return retried
        print(f"STOP: pipeline is BLOCKED: {tracked['detail']}")
        return "BLOCKED"

    if claim_state != "SENT":
        claim_result = _claim_flow(con, cfg, job_id, room)
        if claim_result != "SENT":
            return claim_result

    # A human-confirmed CLAIM is now SENT. The local pipeline contains no signed write.
    tracked = _latest_auto(con, room, job_id)
    pipeline_state = str(tracked["pipeline_state"] or "") if tracked is not None else ""
    if pipeline_state != "DELIVERY_READY":
        pipeline_state = _run_local_pipeline(con, cfg, job_id, room)

    if pipeline_state == "DELIVERY_READY":
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
