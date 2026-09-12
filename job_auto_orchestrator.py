#!/usr/bin/env python3
"""Safe local orchestration around human-approved CLAIM and DELIVER boundaries.

Automation stops at exactly two human-action boundaries:

    SAFE_FIT READY -> local CLAIM PREPARED -> CLAIM_READY notification
    CLAIM SENT      -> local post-claim pipeline -> DELIVER PREPARED -> DELIVERY_READY

This module deliberately imports no CLAIM approve/send or DELIVER approve/send
functions. It never signs or posts a Technocore write. JOB text remains untrusted
and is only processed by the existing hardened local pipeline.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable

from job_candidate_refiner import _runtime_defaults
from job_claim_trial import ensure_claim_schema, prepare_claim
from job_delivery_trial import ensure_delivery_schema
from job_postclaim_pipeline import run_postclaim_pipeline
from technoscout.common import utc_now
from technoscout.db import connect
from technoscout.llm_backend import create_llm_backend
from technoscout_cli import database_path, load_config
from technoscout_notify import (
    notify_claim_ready,
    notify_delivery_ready,
    notify_job_blocked,
)


AUTO_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_auto_orchestrator (
    room TEXT NOT NULL,
    job_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    claim_state TEXT NOT NULL DEFAULT '',
    claim_notified INTEGER NOT NULL DEFAULT 0,
    pipeline_state TEXT NOT NULL DEFAULT '',
    delivery_notified INTEGER NOT NULL DEFAULT 0,
    blocked_notified INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(room, job_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_job_auto_orchestrator_state
    ON job_auto_orchestrator(pipeline_state, updated_at);
"""


CLAIM_TERMINAL = {"SENT", "UNCERTAIN", "RESERVED"}
DELIVERY_TERMINAL = {"SENT", "UNCERTAIN", "RESERVED"}


def ensure_auto_schema(con: Any) -> None:
    con.executescript(AUTO_SCHEMA)
    ensure_claim_schema(con)
    ensure_delivery_schema(con)


def _upsert(
    con: Any,
    *,
    room: str,
    job_id: str,
    content_hash: str,
    claim_state: str | None = None,
    pipeline_state: str | None = None,
    detail: str | None = None,
) -> None:
    ensure_auto_schema(con)
    existing = con.execute(
        "SELECT * FROM job_auto_orchestrator WHERE room=? AND job_id=? AND content_hash=?",
        (room, job_id, content_hash),
    ).fetchone()
    values = {
        "claim_state": str(existing["claim_state"]) if existing is not None else "",
        "pipeline_state": str(existing["pipeline_state"]) if existing is not None else "",
        "detail": str(existing["detail"]) if existing is not None else "",
    }
    if claim_state is not None:
        values["claim_state"] = str(claim_state)
    if pipeline_state is not None:
        values["pipeline_state"] = str(pipeline_state)
    if detail is not None:
        values["detail"] = str(detail)[:500]

    con.execute(
        """
        INSERT INTO job_auto_orchestrator(
          room,job_id,content_hash,claim_state,pipeline_state,detail,updated_at
        ) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(room,job_id,content_hash) DO UPDATE SET
          claim_state=excluded.claim_state,
          pipeline_state=excluded.pipeline_state,
          detail=excluded.detail,
          updated_at=excluded.updated_at
        """,
        (
            room,
            job_id,
            content_hash,
            values["claim_state"],
            values["pipeline_state"],
            values["detail"],
            utc_now(),
        ),
    )
    con.commit()


def _latest_claim(con: Any, room: str, job_id: str) -> Any | None:
    ensure_claim_schema(con)
    return con.execute(
        """
        SELECT room,job_id,content_hash,status,sent_seq,prepared_at
        FROM job_claim_trials
        WHERE room=? AND job_id=?
        ORDER BY prepared_at DESC
        LIMIT 1
        """,
        (room, job_id),
    ).fetchone()


def prepare_claim_ready(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    *,
    room: str = "kibble",
    prepare_runner: Callable[..., dict[str, Any]] = prepare_claim,
) -> dict[str, Any]:
    """Prepare, but never approve/send, one READY candidate."""
    ensure_auto_schema(con)
    existing = _latest_claim(con, room, job_id)
    if existing is not None:
        status = str(existing["status"])
        content_hash = str(existing["content_hash"])
        if status in {"PREPARED", "APPROVED"}:
            _upsert(
                con,
                room=room,
                job_id=job_id,
                content_hash=content_hash,
                claim_state="CLAIM_READY",
                pipeline_state="WAITING_FOR_HUMAN_CLAIM",
            )
            return {"state": "CLAIM_READY", "job_id": job_id, "existing": True}
        if status == "SENT":
            _upsert(
                con,
                room=room,
                job_id=job_id,
                content_hash=content_hash,
                claim_state="CLAIM_SENT",
                pipeline_state="WAITING_POSTCLAIM",
            )
            return {"state": "CLAIM_SENT", "job_id": job_id, "existing": True}
        if status in {"UNCERTAIN", "RESERVED"}:
            _upsert(
                con,
                room=room,
                job_id=job_id,
                content_hash=content_hash,
                claim_state=status,
                pipeline_state="BLOCKED",
                detail=f"claim trial is terminal: {status}",
            )
            return {"state": "BLOCKED", "job_id": job_id, "reason": f"claim trial is terminal: {status}"}

    prepared = prepare_runner(con, cfg, job_id, room=room)
    if prepared.get("state") != "PREPARED":
        return {
            "state": "BLOCKED",
            "job_id": job_id,
            "reason": str(prepared.get("reason", "claim prepare failed")),
        }

    candidate = prepared["candidate"]
    _upsert(
        con,
        room=str(candidate["room"]),
        job_id=str(candidate["job_id"]),
        content_hash=str(candidate["content_hash"]),
        claim_state="CLAIM_READY",
        pipeline_state="WAITING_FOR_HUMAN_CLAIM",
    )
    return {"state": "CLAIM_READY", "job_id": job_id, "existing": False}


def _sent_claim_rows(con: Any, room: str, limit: int) -> list[Any]:
    """Return only SENT claims that this orchestrator already tracked pre-send."""
    ensure_auto_schema(con)
    return con.execute(
        """
        SELECT c.room,c.job_id,c.content_hash,c.status,c.sent_seq,
               d.status AS delivery_status,
               a.pipeline_state AS pipeline_state
        FROM job_claim_trials AS c
        LEFT JOIN job_delivery_trials AS d
          ON d.room=c.room AND d.job_id=c.job_id AND d.content_hash=c.content_hash
        JOIN job_auto_orchestrator AS a
          ON a.room=c.room AND a.job_id=c.job_id AND a.content_hash=c.content_hash
        WHERE c.room=? AND c.status='SENT'
          AND COALESCE(d.status,'') != 'SENT'
          AND a.claim_state IN ('CLAIM_READY','CLAIM_SENT')
          AND a.pipeline_state IN ('WAITING_FOR_HUMAN_CLAIM','WAITING_POSTCLAIM')
        ORDER BY c.prepared_at ASC
        LIMIT ?
        """,
        (room, max(1, min(10, int(limit)))),
    ).fetchall()


def process_sent_claims(
    con: Any,
    cfg: dict[str, Any],
    *,
    room: str = "kibble",
    limit: int = 2,
    pipeline_runner: Callable[..., dict[str, Any]] = run_postclaim_pipeline,
    llm_factory: Callable[[dict[str, Any]], Any] = create_llm_backend,
) -> list[dict[str, Any]]:
    """Run local post-claim work once for newly SENT tracked claims; never DELIVER."""
    rows = _sent_claim_rows(con, room, limit)
    if not rows:
        return []

    model = str(cfg.get("research_model") or cfg.get("triage_model") or "").strip()
    results: list[dict[str, Any]] = []
    llm = None
    try:
        for row in rows:
            job_id = str(row["job_id"])
            content_hash = str(row["content_hash"])
            delivery_status = str(row["delivery_status"] or "")

            if delivery_status in {"PREPARED", "APPROVED"}:
                _upsert(
                    con,
                    room=room,
                    job_id=job_id,
                    content_hash=content_hash,
                    claim_state="CLAIM_SENT",
                    pipeline_state="DELIVERY_READY",
                )
                results.append({"job_id": job_id, "state": "DELIVERY_READY", "existing": True})
                continue

            if delivery_status in DELIVERY_TERMINAL:
                terminal_state = "DELIVERED" if delivery_status == "SENT" else "DELIVERY_TERMINAL"
                _upsert(
                    con,
                    room=room,
                    job_id=job_id,
                    content_hash=content_hash,
                    claim_state="CLAIM_SENT",
                    pipeline_state=terminal_state,
                    detail=f"delivery trial is terminal: {delivery_status}",
                )
                results.append({"job_id": job_id, "state": terminal_state})
                continue

            if not model:
                _upsert(
                    con,
                    room=room,
                    job_id=job_id,
                    content_hash=content_hash,
                    claim_state="CLAIM_SENT",
                    pipeline_state="BLOCKED",
                    detail="research_model or triage_model is not configured",
                )
                results.append({"job_id": job_id, "state": "BLOCKED", "stage": "model-config"})
                continue

            if llm is None:
                llm = llm_factory(cfg)

            result = pipeline_runner(
                con,
                cfg,
                job_id,
                room=room,
                llm=llm,
                model=model,
            )
            if result.get("state") == "READY_FOR_HUMAN_DELIVERY":
                _upsert(
                    con,
                    room=room,
                    job_id=job_id,
                    content_hash=content_hash,
                    claim_state="CLAIM_SENT",
                    pipeline_state="DELIVERY_READY",
                )
                results.append({"job_id": job_id, "state": "DELIVERY_READY", "result": result})
            else:
                stage = str(result.get("stage", "postclaim"))
                reason = str(result.get("reason", "postclaim pipeline blocked"))
                _upsert(
                    con,
                    room=room,
                    job_id=job_id,
                    content_hash=content_hash,
                    claim_state="CLAIM_SENT",
                    pipeline_state="BLOCKED",
                    detail=f"{stage}: {reason}",
                )
                results.append({"job_id": job_id, "state": "BLOCKED", "stage": stage, "reason": reason})
    finally:
        if llm is not None:
            close = getattr(llm, "close", None)
            if callable(close):
                close()
    return results


def publish_pending_notifications(
    con: Any,
    cfg: dict[str, Any],
    *,
    room: str = "kibble",
    claim_notifier: Callable[[dict[str, Any], str], dict[str, str]] = notify_claim_ready,
    delivery_notifier: Callable[[dict[str, Any], str], dict[str, str]] = notify_delivery_ready,
    blocked_notifier: Callable[[dict[str, Any], str, str], dict[str, str]] = notify_job_blocked,
) -> dict[str, int]:
    """Retry ntfy delivery only; never retry JOB actions."""
    ensure_auto_schema(con)
    stats = {"claim_ready": 0, "delivery_ready": 0, "blocked": 0, "failed": 0}
    rows = con.execute(
        """
        SELECT room,job_id,content_hash,claim_state,claim_notified,
               pipeline_state,delivery_notified,blocked_notified,detail
        FROM job_auto_orchestrator
        WHERE room=?
        ORDER BY updated_at ASC
        """,
        (room,),
    ).fetchall()

    for row in rows:
        job_id = str(row["job_id"])
        content_hash = str(row["content_hash"])
        pipeline_state = str(row["pipeline_state"])

        if (
            str(row["claim_state"]) == "CLAIM_READY"
            and pipeline_state == "WAITING_FOR_HUMAN_CLAIM"
            and not int(row["claim_notified"])
        ):
            notice = claim_notifier(cfg, job_id)
            if notice.get("state") == "PUBLISHED_LOCAL":
                con.execute(
                    "UPDATE job_auto_orchestrator SET claim_notified=1,updated_at=? WHERE room=? AND job_id=? AND content_hash=?",
                    (utc_now(), room, job_id, content_hash),
                )
                stats["claim_ready"] += 1
            elif notice.get("state") != "DISABLED":
                stats["failed"] += 1

        if pipeline_state == "DELIVERY_READY" and not int(row["delivery_notified"]):
            notice = delivery_notifier(cfg, job_id)
            if notice.get("state") == "PUBLISHED_LOCAL":
                con.execute(
                    "UPDATE job_auto_orchestrator SET delivery_notified=1,updated_at=? WHERE room=? AND job_id=? AND content_hash=?",
                    (utc_now(), room, job_id, content_hash),
                )
                stats["delivery_ready"] += 1
            elif notice.get("state") != "DISABLED":
                stats["failed"] += 1

        if pipeline_state == "BLOCKED" and not int(row["blocked_notified"]):
            stage = str(row["detail"] or "blocked").split(":", 1)[0]
            notice = blocked_notifier(cfg, job_id, stage)
            if notice.get("state") == "PUBLISHED_LOCAL":
                con.execute(
                    "UPDATE job_auto_orchestrator SET blocked_notified=1,updated_at=? WHERE room=? AND job_id=? AND content_hash=?",
                    (utc_now(), room, job_id, content_hash),
                )
                stats["blocked"] += 1
            elif notice.get("state") != "DISABLED":
                stats["failed"] += 1

    con.commit()
    return stats


def run_once(
    con: Any,
    cfg: dict[str, Any],
    *,
    ready_job_id: str = "",
    room: str = "kibble",
    limit: int = 2,
    prepare_runner: Callable[..., dict[str, Any]] = prepare_claim,
    pipeline_runner: Callable[..., dict[str, Any]] = run_postclaim_pipeline,
    llm_factory: Callable[[dict[str, Any]], Any] = create_llm_backend,
    claim_notifier: Callable[[dict[str, Any], str], dict[str, str]] = notify_claim_ready,
    delivery_notifier: Callable[[dict[str, Any], str], dict[str, str]] = notify_delivery_ready,
    blocked_notifier: Callable[[dict[str, Any], str, str], dict[str, str]] = notify_job_blocked,
) -> dict[str, Any]:
    """One safe orchestration pass. No signed write exists in this call graph."""
    ensure_auto_schema(con)
    prepared = None
    if ready_job_id:
        prepared = prepare_claim_ready(
            con,
            cfg,
            ready_job_id,
            room=room,
            prepare_runner=prepare_runner,
        )

    processed = process_sent_claims(
        con,
        cfg,
        room=room,
        limit=limit,
        pipeline_runner=pipeline_runner,
        llm_factory=llm_factory,
    )
    notices = publish_pending_notifications(
        con,
        cfg,
        room=room,
        claim_notifier=claim_notifier,
        delivery_notifier=delivery_notifier,
        blocked_notifier=blocked_notifier,
    )
    return {"prepared": prepared, "processed": processed, "notifications": notices}


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe JOB orchestration; never CLAIMs or DELIVERs")
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--room", default="kibble")
    parser.add_argument("--ready-job-id", default="")
    parser.add_argument("--limit", type=int, default=2)
    args = parser.parse_args()

    cfg = _runtime_defaults(load_config(args.config))
    con = connect(database_path(cfg))
    try:
        result = run_once(
            con,
            cfg,
            ready_job_id=str(args.ready_job_id or ""),
            room=str(args.room),
            limit=max(1, min(10, int(args.limit))),
        )
    finally:
        con.close()

    prepared = result.get("prepared")
    if prepared:
        print(f"Auto Orchestrator | prepare={prepared.get('state')} job={prepared.get('job_id','')}")
    for item in result["processed"]:
        print(f"Auto Orchestrator | job={item['job_id']} state={item['state']}")
    n = result["notifications"]
    print(
        "Auto Orchestrator | notifications "
        f"claim_ready={n['claim_ready']} delivery_ready={n['delivery_ready']} "
        f"blocked={n['blocked']} failed={n['failed']}"
    )
    print("STOP: no CLAIM or DELIVER was approved or sent.")


if __name__ == "__main__":
    main()
