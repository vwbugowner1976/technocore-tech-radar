#!/usr/bin/env python3
"""Export-aware live OPEN revalidation for Kibble jobs.

Kibble can advance by more than one normal room-read window between observing a
JOB and checking it. A ``?since=`` tail alone can therefore omit the original
JOB even while that record is still retained. This module verifies a retained
room snapshot via ``/export``, then catches up from the snapshot's highest seq
using bounded incremental reads.

If Kibble advances so quickly that the first catch-up page already has a gap,
the checker may retry from a fresh export snapshot. A retry never weakens the
positive condition: OPEN_CONFIRMED still requires an exact retained JOB and a
complete gap-free catch-up from one fresh snapshot. Persistent ambiguity fails
closed.

It is strictly read-only: no claim, send, tool execution, wallet action, or
FLOP/token spend occurs here.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

from job_shadow import content_hash, parse_kibble_message, sender_of
from technoscout.common import RateLimited, room_messages, safe_room, seq_of, technocore_json


LIVE_LIFECYCLE = {
    "CLAIM": "CLAIMED",
    "RESULT": "DELIVERED",
    "DELIVER": "DELIVERED",
    "ATTEST": "ATTESTED",
    "WITNESS": "ATTESTED",
}
LIVE_RANK = {
    "OPEN": 0,
    "CLAIMED": 1,
    "DELIVERED": 2,
    "ATTESTED": 3,
}


RETRYABLE_SNAPSHOT_STATES = {
    "INCONCLUSIVE_GAP",
    "INCONCLUSIVE_TRUNCATED",
    "INCONCLUSIVE",
    "INCONCLUSIVE_EMPTY_EXPORT",
}


def retained_export_messages(cfg: dict[str, Any], room: str) -> list[dict[str, Any]]:
    """Read the currently retained room ring as raw JSONL, in memory only."""
    safe = safe_room(room)
    if not safe or safe != room:
        raise ValueError("invalid room")
    base_url = str(cfg.get("base_url", "https://technocore.chat")).rstrip("/")
    url = f"{base_url}/r/{safe}/export"
    max_bytes = max(
        1_048_576,
        min(
            12 * 1024 * 1024,
            int(cfg.get("job_live_export_max_bytes", 11 * 1024 * 1024)),
        ),
    )
    timeout = float(cfg.get("http_timeout_seconds", 25))
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "technoscout/0.8",
            "Accept": "application/x-ndjson, application/json, text/plain",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read(16384).decode("utf-8", "replace")
        if exc.code == 429:
            raise RateLimited(30.0) from exc
        raise RuntimeError(f"Technocore export HTTP {exc.code}: {detail[:300]}") from exc

    if len(body) > max_bytes:
        raise ValueError("Technocore room export exceeded configured live-check limit")

    messages: list[dict[str, Any]] = []
    for raw_line in body.splitlines():
        if not raw_line.strip():
            continue
        try:
            item = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(item, dict):
            messages.append(item)
    return messages


def _scan_job(
    candidate: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    require_exact_job: bool,
) -> dict[str, Any]:
    job_id = str(candidate["job_id"])
    job_seq = int(candidate["job_seq"])
    issuer_did = str(candidate["issuer_did"])
    expected_hash = str(candidate["content_hash"])

    saw_exact = False
    lifecycle = "OPEN"
    highest_seq = 0
    for message in sorted(messages, key=seq_of):
        message_seq = seq_of(message)
        highest_seq = max(highest_seq, message_seq)
        parsed = parse_kibble_message(message.get("text", message.get("message", "")))
        if not parsed or parsed.get("job_id") != job_id:
            continue

        if parsed["verb"] == "JOB":
            if message_seq != job_seq:
                return {
                    "state": "JOB_MISMATCH",
                    "lifecycle": lifecycle,
                    "highest_seq": highest_seq,
                }
            if sender_of(message) != issuer_did or content_hash(parsed) != expected_hash:
                return {
                    "state": "JOB_MISMATCH",
                    "lifecycle": lifecycle,
                    "highest_seq": highest_seq,
                }
            saw_exact = True
            continue

        if message_seq <= job_seq:
            continue
        state = LIVE_LIFECYCLE.get(parsed["verb"])
        if state and LIVE_RANK[state] > LIVE_RANK[lifecycle]:
            lifecycle = state

    if require_exact_job and not saw_exact:
        return {
            "state": "JOB_NOT_RETAINED",
            "lifecycle": lifecycle,
            "highest_seq": highest_seq,
        }
    if lifecycle != "OPEN":
        return {
            "state": "NOT_OPEN",
            "lifecycle": lifecycle,
            "highest_seq": highest_seq,
        }
    return {"state": "OPEN", "lifecycle": "OPEN", "highest_seq": highest_seq}


def _check_one_snapshot(
    cfg: dict[str, Any],
    candidate: dict[str, Any],
    *,
    read: Callable[[dict[str, Any], str, dict[str, Any]], Any],
    export_read: Callable[[dict[str, Any], str], list[dict[str, Any]]],
    page_limit: int,
    max_pages: int,
    snapshot_attempt: int,
) -> dict[str, Any]:
    """Check one export snapshot plus its catch-up window."""
    room = str(candidate["room"])
    snapshot = export_read(cfg, room)
    snapshot_check = _scan_job(candidate, snapshot, require_exact_job=True)
    messages_seen = len(snapshot)
    if snapshot_check["state"] != "OPEN":
        return {
            "state": snapshot_check["state"],
            "lifecycle": snapshot_check["lifecycle"],
            "pages": 0,
            "messages": messages_seen,
            "source": "export",
            "snapshot_attempts": snapshot_attempt,
        }

    cursor = int(snapshot_check["highest_seq"])
    if cursor <= 0:
        return {
            "state": "INCONCLUSIVE_EMPTY_EXPORT",
            "lifecycle": "OPEN",
            "pages": 0,
            "messages": messages_seen,
            "source": "export",
            "snapshot_attempts": snapshot_attempt,
        }

    pages = 0
    while pages < max_pages:
        payload = read(
            cfg,
            f"/r/{room}",
            {"format": "json", "since": cursor, "limit": page_limit},
        )
        messages = sorted(room_messages(payload), key=seq_of)
        pages += 1
        messages_seen += len(messages)

        if not messages:
            return {
                "state": "OPEN_CONFIRMED",
                "lifecycle": "OPEN",
                "pages": pages,
                "messages": messages_seen,
                "source": "export+catchup",
                "snapshot_attempts": snapshot_attempt,
            }

        first_seq = seq_of(messages[0])
        if first_seq > cursor + 1:
            return {
                "state": "INCONCLUSIVE_GAP",
                "lifecycle": "OPEN",
                "pages": pages,
                "messages": messages_seen,
                "source": "export+catchup",
                "snapshot_attempts": snapshot_attempt,
                "gap_from": cursor + 1,
                "gap_to": first_seq - 1,
            }

        catchup_check = _scan_job(candidate, messages, require_exact_job=False)
        if catchup_check["state"] in {"JOB_MISMATCH", "NOT_OPEN"}:
            return {
                "state": catchup_check["state"],
                "lifecycle": catchup_check["lifecycle"],
                "pages": pages,
                "messages": messages_seen,
                "source": "export+catchup",
                "snapshot_attempts": snapshot_attempt,
            }

        new_cursor = max(cursor, int(catchup_check["highest_seq"]))
        if new_cursor <= cursor:
            return {
                "state": "INCONCLUSIVE",
                "lifecycle": "OPEN",
                "pages": pages,
                "messages": messages_seen,
                "source": "export+catchup",
                "snapshot_attempts": snapshot_attempt,
            }
        cursor = new_cursor

        if len(messages) < page_limit:
            return {
                "state": "OPEN_CONFIRMED",
                "lifecycle": "OPEN",
                "pages": pages,
                "messages": messages_seen,
                "source": "export+catchup",
                "snapshot_attempts": snapshot_attempt,
            }

    return {
        "state": "INCONCLUSIVE_TRUNCATED",
        "lifecycle": "OPEN",
        "pages": pages,
        "messages": messages_seen,
        "source": "export+catchup",
        "snapshot_attempts": snapshot_attempt,
    }


def live_revalidate_job_export_aware(
    cfg: dict[str, Any],
    candidate: dict[str, Any],
    *,
    fetcher: Callable[[dict[str, Any], str, dict[str, Any]], Any] | None = None,
    export_fetcher: Callable[[dict[str, Any], str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Verify exact retained JOB, then catch up to the live room head.

    A positive result requires one fresh snapshot to contain the exact persisted
    JOB and a complete gap-free catch-up with no later lifecycle event. When a
    busy-room race causes an incomplete catch-up, retry from a new snapshot a
    small bounded number of times. Any persistent ambiguity still fails closed.
    """
    page_limit = max(20, min(200, int(cfg.get("job_gate_live_page_limit", 200))))
    max_pages = max(1, min(20, int(cfg.get("job_gate_live_max_pages", 6))))
    snapshot_retries = max(
        0,
        min(4, int(cfg.get("job_gate_live_snapshot_retries", 2))),
    )
    read = fetcher or technocore_json
    export_read = export_fetcher or retained_export_messages

    last: dict[str, Any] | None = None
    for attempt in range(1, snapshot_retries + 2):
        result = _check_one_snapshot(
            cfg,
            candidate,
            read=read,
            export_read=export_read,
            page_limit=page_limit,
            max_pages=max_pages,
            snapshot_attempt=attempt,
        )
        if result["state"] not in RETRYABLE_SNAPSHOT_STATES:
            return result
        last = result

    assert last is not None
    return last
