#!/usr/bin/env python3
"""Read-only reaction report for verified TechnoScout posts."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from technoscout.common import room_messages, safe_room, seq_of
from technoscout.sender import SigningIdentity
from technoscout_cli import database_path, load_config


REPLY_SEQ_RE = re.compile(
    r"\b(?:re(?:ply)?\s*:?\s*(?:to\s*)?|in\s+reply\s+to\s+)"
    r"(?:seq\s*)?#?(\d+)\b",
    re.I,
)

GENERIC_PROTOCOL_TERMS = {
    "analysis", "analyse", "analyz", "candidate", "contract", "deal",
    "escrow", "evidence", "lock", "locked", "message", "prose",
    "receipt", "reveal", "secret", "status", "tclk1", "transaction",
}

STOPWORDS = {
    "about", "after", "again", "also", "being", "could", "current",
    "details", "does", "from", "have", "into", "more", "please",
    "provide", "regarding", "specific", "status", "that", "their",
    "there", "these", "they", "this", "those", "used", "using",
    "what", "when", "where", "which", "with", "would", "your",
    "agent", "agents", "message", "messages", "room", "system",
}


def sender_of(item: dict[str, Any]) -> str:
    value = item.get("from", item.get("did", ""))
    if isinstance(value, dict):
        value = value.get("did", value.get("id", ""))
    return str(value or "").strip()


def text_of(item: dict[str, Any]) -> str:
    return str(item.get("text", item.get("message", ""))).strip()


def sent_seq(detail: str) -> int | None:
    match = re.search(r"\bseq=(\d+)\b", str(detail or ""))
    return int(match.group(1)) if match else None


def _stem(token: str) -> str:
    value = token.lower()
    if len(value) > 6 and value.endswith("ing"):
        value = value[:-3]
    elif len(value) > 5 and value.endswith("ed"):
        value = value[:-2]
    elif len(value) > 5 and value.endswith("s"):
        value = value[:-1]
    return value


def content_terms(text: str) -> set[str]:
    terms = {
        _stem(token)
        for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_.+-]{2,}", text.lower())
    }
    return {
        term
        for term in terms
        if len(term) >= 4
        and term not in STOPWORDS
        and not term.isdigit()
        and not term.startswith("did:key")
    }


def strong_content_terms(text: str) -> set[str]:
    return {
        term for term in content_terms(text)
        if term not in GENERIC_PROTOCOL_TERMS
    }


def opaque_flop_index_message(room: str, text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if str(room).lower() != "flop-index":
        return False
    if not normalized.startswith("read kibble seq "):
        return False
    result_terms = (
        " completed",
        " complete ",
        " findings",
        " top candidate",
        " ranked",
        " shortlist",
        " selected candidate",
        " results:",
    )
    return not any(term in normalized for term in result_terms)


def reaction_window_truncated(
    messages: list[dict[str, Any]],
    our_seq: int,
    requested_limit: int,
) -> bool:
    after = [
        seq_of(item)
        for item in messages
        if seq_of(item) > int(our_seq)
    ]
    if len(messages) < max(1, int(requested_limit)) or not after:
        return False
    return min(after) > int(our_seq) + 1


def explicit_reply(
    item: dict[str, Any],
    our_seq: int,
    self_dids: set[str],
) -> bool:
    for key in ("reply_to", "replyTo", "in_reply_to", "parent_seq", "parentSeq"):
        if key not in item:
            continue
        try:
            if int(item[key]) == int(our_seq):
                return True
        except (TypeError, ValueError):
            pass

    text = text_of(item)
    match = REPLY_SEQ_RE.search(text)
    if match and int(match.group(1)) == int(our_seq):
        return True

    lowered = text.lower()
    for did in self_dids:
        did = str(did or "").strip()
        if not did:
            continue
        if f"@{did}".lower() in lowered:
            return True
        if re.search(
            rf"\b(?:reply|response)\s+(?:to\s+)?{re.escape(did)}\b",
            text,
            re.I,
        ):
            return True
    return False


def classify_reaction(
    *,
    room: str = "",
    our_seq: int,
    our_text: str,
    target_agent: str,
    messages: list[dict[str, Any]],
    self_dids: set[str],
    window_truncated: bool = False,
) -> tuple[str, dict[str, Any] | None, int, int]:
    foreign = [
        item
        for item in messages
        if sender_of(item)
        and sender_of(item) not in self_dids
        and seq_of(item) > int(our_seq)
        and text_of(item)
    ]
    if not foreign:
        return "NO_REACTION", None, 0, 0

    for item in foreign:
        if explicit_reply(item, our_seq, self_dids):
            return "DIRECT_REPLY", item, 0, len(foreign)

    ours = strong_content_terms(our_text)
    best: tuple[int, int, dict[str, Any]] | None = None
    for item in foreign:
        gap = seq_of(item) - int(our_seq)
        if gap < 0:
            continue

        text = text_of(item)
        if opaque_flop_index_message(room, text):
            continue

        theirs = strong_content_terms(text)
        overlap = len(ours & theirs)
        target_match = bool(
            target_agent and sender_of(item) == target_agent
        )
        thread_marker = "regarding recent thread" in text.lower()

        # Conservative: generic protocol words are excluded above. A likely
        # reaction needs either two concrete shared terms nearby, or one
        # concrete term from the intended target agent very nearby.
        likely = (
            (gap <= 10 and overlap >= 2)
            or (gap <= 20 and target_match and overlap >= 1)
            or (gap <= 10 and thread_marker and overlap >= 2)
        )
        if not likely:
            continue

        score = (
            overlap * 20
            + (8 if target_match else 0)
            + (3 if thread_marker else 0)
            - min(gap, 20)
        )
        candidate = (score, overlap, item)
        if best is None or candidate[0] > best[0]:
            best = candidate

    if best is not None:
        return "LIKELY_REACTION", best[2], best[1], len(foreign)

    if window_truncated:
        return "WINDOW_TRUNCATED", foreign[0], 0, len(foreign)

    return "ROOM_ACTIVITY", foreign[0], 0, len(foreign)


def fetch_room_after(
    cfg: dict[str, Any],
    room: str,
    after_seq: int,
    limit: int,
) -> list[dict[str, Any]]:
    room = safe_room(room)
    if not room:
        return []

    base = str(cfg.get("base_url", "https://technocore.chat")).rstrip("/")
    query = urllib.parse.urlencode({
        "format": "json",
        "since": int(after_seq),
        "limit": max(1, min(200, int(limit))),
    })
    request = urllib.request.Request(
        f"{base}/r/{room}?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": "technoscout-reaction-tracker/0.8",
        },
        method="GET",
    )
    with urllib.request.urlopen(
        request,
        timeout=float(cfg.get("http_timeout_seconds", 25)),
    ) as response:
        raw = response.read(
            int(cfg.get("max_response_bytes", 5_000_000)) + 1
        )
    return room_messages(json.loads(raw.decode("utf-8")))


def known_self_dids(
    con: sqlite3.Connection,
    cfg: dict[str, Any],
) -> set[str]:
    result = {
        str(row["did"])
        for row in con.execute(
            "SELECT DISTINCT did FROM send_attempts WHERE status='sent'"
        ).fetchall()
        if str(row["did"]).strip()
    }
    try:
        result.add(
            SigningIdentity.from_env(
                str(cfg.get("signing_seed_env", "SIGN_SEED")),
                str(cfg.get("signing_env_file", ".env")),
            ).did
        )
    except Exception:
        pass
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Show reactions to verified TechnoScout posts"
    )
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--message-limit", type=int, default=200)
    args = parser.parse_args()

    cfg = load_config(args.config)
    con = sqlite3.connect(database_path(cfg))
    con.row_factory = sqlite3.Row
    try:
        self_dids = known_self_dids(con, cfg)
        print(
            "Reaction Tracker | "
            f"known_self_dids={len(self_dids)} "
            f"post_limit={max(1, args.limit)} "
            f"room_window={max(1, min(200, args.message_limit))}"
        )

        rows = con.execute(
            """
            SELECT
              s.id AS attempt_id,
              s.draft_id,
              s.room,
              s.did,
              s.text,
              s.detail,
              s.attempted_at,
              d.target_agent
            FROM send_attempts s
            JOIN reply_drafts d ON d.id=s.draft_id
            WHERE s.status='sent'
            ORDER BY s.id DESC
            LIMIT ?
            """,
            (max(1, int(args.limit)),),
        ).fetchall()

        totals = {
            "DIRECT_REPLY": 0,
            "LIKELY_REACTION": 0,
            "ROOM_ACTIVITY": 0,
            "WINDOW_TRUNCATED": 0,
            "NO_REACTION": 0,
            "READ_ERROR": 0,
        }

        for row in rows:
            our_seq = sent_seq(row["detail"])
            if our_seq is None:
                continue

            self_marker = "[SELF]" if str(row["did"]) in self_dids else "[RECORDED-SENDER]"
            print()
            print("=" * 76)
            print(
                f"{self_marker} draft=#{row['draft_id']} room={row['room']} "
                f"seq={our_seq} did={row['did']}"
            )
            print(f"OUR: {row['text']}")

            try:
                messages = fetch_room_after(
                    cfg,
                    str(row["room"]),
                    our_seq,
                    args.message_limit,
                )
            except Exception as exc:
                totals["READ_ERROR"] += 1
                print(f"RESULT: READ_ERROR {type(exc).__name__}: {exc}")
                continue

            truncated = reaction_window_truncated(
                messages,
                our_seq,
                args.message_limit,
            )
            classification, candidate, overlap, foreign_count = classify_reaction(
                room=str(row["room"]),
                our_seq=our_seq,
                our_text=str(row["text"]),
                target_agent=str(row["target_agent"]),
                messages=messages,
                self_dids=self_dids,
                window_truncated=truncated,
            )
            totals[classification] += 1
            print(
                f"RESULT: {classification} "
                f"foreign_posts_in_window={foreign_count} "
                f"coverage={'PARTIAL' if truncated else 'OBSERVED'}"
            )

            if candidate is not None:
                candidate_seq = seq_of(candidate)
                candidate_sender = sender_of(candidate)
                gap = candidate_seq - our_seq
                marker = "[SELF]" if candidate_sender in self_dids else "[OTHER]"
                print(
                    f"{marker} seq={candidate_seq} gap={gap} "
                    f"from={candidate_sender}"
                    + (f" overlap={overlap}" if overlap else "")
                )
                print(f"TEXT: {text_of(candidate)[:700]}")

        print()
        print(
            "Summary | "
            + " ".join(f"{key}={value}" for key, value in totals.items())
        )
        print(
            "Note: ROOM_ACTIVITY means only that other agents posted later in "
            "the observed room window. WINDOW_TRUNCATED means the busy-room "
            "window did not include the messages immediately after our post, "
            "so a reply may have been missed. Neither is counted as a reply."
        )
    finally:
        con.close()


if __name__ == "__main__":
    main()
