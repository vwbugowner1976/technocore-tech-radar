#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from typing import Any

from job_auto_orchestrator import ensure_auto_schema
from job_candidate_refiner import _runtime_defaults
from job_shadow_policy import SELF_CONTAINED_TYPES, TOOL_HINTS
from technoscout.common import utc_now
from technoscout.db import connect
from technoscout_cli import database_path, load_config

CANARY_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_canary_auto_control (
    id INTEGER PRIMARY KEY CHECK(id=1),
    mode TEXT NOT NULL DEFAULT 'OFF',
    remaining_successes INTEGER NOT NULL DEFAULT 0,
    total_successes INTEGER NOT NULL DEFAULT 0,
    last_job_id TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_canary_auto_runs (
    room TEXT NOT NULL,
    job_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    claim_seq INTEGER,
    delivery_seq INTEGER,
    detail TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(room,job_id,content_hash)
);
"""

_URL_RE = re.compile(r"https?://|www\.", re.I)


def ensure_canary_schema(con: Any) -> None:
    con.executescript(CANARY_SCHEMA)
    ensure_auto_schema(con)
    con.execute(
        "INSERT OR IGNORE INTO job_canary_auto_control(id,mode,remaining_successes,total_successes,last_job_id,detail,updated_at) VALUES(1,'OFF',0,0,'','',?)",
        (utc_now(),),
    )
    con.commit()


def canary_status(con: Any) -> dict[str, Any]:
    ensure_canary_schema(con)
    row = con.execute("SELECT * FROM job_canary_auto_control WHERE id=1").fetchone()
    return {key: row[key] for key in row.keys()}


def enable_canary(con: Any, budget: int = 1) -> dict[str, Any]:
    ensure_canary_schema(con)
    amount = max(1, min(10, int(budget)))
    con.execute(
        "UPDATE job_canary_auto_control SET mode='CANARY',remaining_successes=?,detail='operator armed canary',updated_at=? WHERE id=1",
        (amount, utc_now()),
    )
    con.commit()
    return canary_status(con)


def pause_canary(con: Any, reason: str, job_id: str = '') -> dict[str, Any]:
    ensure_canary_schema(con)
    con.execute(
        "UPDATE job_canary_auto_control SET mode='PAUSED',last_job_id=?,detail=?,updated_at=? WHERE id=1",
        (str(job_id), str(reason)[:500], utc_now()),
    )
    con.commit()
    return canary_status(con)


def disable_canary(con: Any) -> dict[str, Any]:
    ensure_canary_schema(con)
    con.execute(
        "UPDATE job_canary_auto_control SET mode='OFF',remaining_successes=0,detail='operator disabled canary',updated_at=? WHERE id=1",
        (utc_now(),),
    )
    con.commit()
    return canary_status(con)


def record_run(con: Any, room: str, job_id: str, content_hash: str, state: str, detail: str = '', claim_seq: int | None = None, delivery_seq: int | None = None) -> None:
    ensure_canary_schema(con)
    now = utc_now()
    con.execute(
        """
        INSERT INTO job_canary_auto_runs(room,job_id,content_hash,state,claim_seq,delivery_seq,detail,started_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(room,job_id,content_hash) DO UPDATE SET
          state=excluded.state,
          claim_seq=COALESCE(excluded.claim_seq,job_canary_auto_runs.claim_seq),
          delivery_seq=COALESCE(excluded.delivery_seq,job_canary_auto_runs.delivery_seq),
          detail=excluded.detail,
          updated_at=excluded.updated_at
        """,
        (room,job_id,content_hash,state,claim_seq,delivery_seq,str(detail)[:500],now,now),
    )
    con.commit()


def active_run(con: Any, room: str) -> Any | None:
    ensure_canary_schema(con)
    return con.execute(
        """
        SELECT * FROM job_canary_auto_runs
        WHERE room=? AND state IN ('PREPARED','CLAIM_SENT','DELIVERY_READY')
        ORDER BY started_at ASC LIMIT 1
        """,
        (str(room),),
    ).fetchone()


def candidate_policy(cfg: dict[str, Any], prepared: dict[str, Any]) -> dict[str, str]:
    candidate = prepared.get('candidate') or {}
    job = prepared.get('job') or {}
    rep = candidate.get('issuer_reputation') or {}
    allowed = cfg.get('job_canary_allowed_types')
    allowed_types = set(SELF_CONTAINED_TYPES) if not isinstance(allowed, list) or not allowed else {str(x).strip().lower() for x in allowed if str(x).strip()}
    checks = (
        (str(candidate.get('job_type','')) in allowed_types, 'job type is not canary self-contained'),
        (str(candidate.get('deterministic_class','')) == 'FIT', 'deterministic class is not FIT'),
        (str(candidate.get('refined_effort','')) in {'tiny','small'}, 'refined effort is not tiny/small'),
        (int(candidate.get('refined_relevance',0)) >= int(cfg.get('job_canary_min_relevance',80)), 'relevance below canary threshold'),
        (int(candidate.get('refined_fit',0)) >= int(cfg.get('job_canary_min_fit',90)), 'technical fit below canary threshold'),
        (int(candidate.get('refined_confidence',0)) >= int(cfg.get('job_canary_min_confidence',95)), 'confidence below canary threshold'),
        (int(rep.get('score',0)) >= int(cfg.get('job_canary_min_issuer_score',90)), 'issuer score below canary threshold'),
        (int(rep.get('attested_jobs',0)) >= int(cfg.get('job_canary_min_attested_jobs',3)), 'issuer attestation history below canary threshold'),
        (int(rep.get('completion_rate_percent',0)) >= int(cfg.get('job_canary_min_completion_percent',70)), 'issuer completion rate below canary threshold'),
    )
    for ok, reason in checks:
        if not ok:
            return {'state':'SKIP','reason':reason}
    text = f"{job.get('title','')} {job.get('body','')}".lower()
    if 'success:' not in text:
        return {'state':'SKIP','reason':'explicit Success clause is required'}
    if _URL_RE.search(text):
        return {'state':'SKIP','reason':'URL-like content is excluded'}
    tool_hit = next((term for term in TOOL_HINTS if term in text), '')
    if tool_hit:
        return {'state':'SKIP','reason':f'external-tool hint excluded: {tool_hit}'}
    if len(str(job.get('body',''))) > int(cfg.get('job_canary_max_body_chars',2200)):
        return {'state':'SKIP','reason':'JOB body exceeds canary size limit'}
    return {'state':'ELIGIBLE','reason':''}


def delivery_policy(cfg: dict[str, Any], result: dict[str, Any]) -> dict[str, str]:
    if result.get('state') != 'READY_FOR_HUMAN_DELIVERY':
        return {'state':'BLOCK','reason':'post-claim pipeline did not reach delivery-ready'}
    quality = result.get('quality') or {}
    success = result.get('success') or {}
    if int(quality.get('confidence',0)) < int(cfg.get('job_canary_min_quality_confidence',90)):
        return {'state':'BLOCK','reason':'quality confidence below canary threshold'}
    if success.get('state') == 'SUCCESS_REVIEWED' and int(success.get('confidence',0)) < int(cfg.get('job_canary_min_success_confidence',90)):
        return {'state':'BLOCK','reason':'Success confidence below canary threshold'}
    if success.get('state') not in {'SUCCESS_REVIEWED','NOT_APPLICABLE'}:
        return {'state':'BLOCK','reason':'Success gate not reviewed'}
    if result.get('semantic_fallback') is not None:
        return {'state':'BLOCK','reason':'semantic fallback required; human review required'}
    if result.get('quality_block_repair') is not None:
        return {'state':'BLOCK','reason':'quality repair required; human review required'}
    return {'state':'ELIGIBLE','reason':''}


def complete_success(con: Any, job_id: str) -> dict[str, Any]:
    control = canary_status(con)
    remaining = max(0, int(control['remaining_successes']) - 1)
    mode = 'CANARY' if remaining > 0 else 'PAUSED'
    detail = 'canary continues within operator budget' if remaining else 'canary success budget exhausted; review before re-arming'
    con.execute(
        "UPDATE job_canary_auto_control SET mode=?,remaining_successes=?,total_successes=total_successes+1,last_job_id=?,detail=?,updated_at=? WHERE id=1",
        (mode,remaining,str(job_id),detail,utc_now()),
    )
    con.commit()
    return canary_status(con)


def main() -> None:
    parser = argparse.ArgumentParser(description='TechnoScout CANARY auto control')
    parser.add_argument('--config', default='technoscout.config.json')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('enable'); p.add_argument('--budget', type=int, default=1)
    sub.add_parser('disable'); sub.add_parser('status')
    args = parser.parse_args()
    cfg = _runtime_defaults(load_config(args.config))
    con = connect(database_path(cfg))
    try:
        if args.command == 'enable': status = enable_canary(con,args.budget)
        elif args.command == 'disable': status = disable_canary(con)
        else: status = canary_status(con)
        print(f"Canary Auto | mode={status['mode']} remaining={status['remaining_successes']} total_successes={status['total_successes']} last_job={status['last_job_id'] or '-'}")
        if status['detail']: print(f"detail={status['detail']}")
    finally:
        con.close()


if __name__ == '__main__':
    main()
