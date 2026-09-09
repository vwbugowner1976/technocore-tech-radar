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

    return any(did and did in text for did in self_dids)


def classify_reaction(
    *,
    our_seq: int,
    our_text: str,
    target_agent: str,
    messages: list[dict[str, Any]],
    self_dids: set[str],
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

    ours = content_terms(our_text)
    best: tuple[int, int, dict[str, Any]] | None = None
    for item in foreign:
        gap = seq_of(item) - int(our_seq)
        if gap < 0:
            continue
        theirs = content_terms(text_of(item))
        overlap = len(ours & theirs)
        sender_bonus = 2 if target_agent and sender_of(item) == target_agent else 0
        thread_bonus = 1 if "regarding recent thread" in text_of(item).lower() else 0

        likely = (
            (gap <= 2 and overlap >= 1)
            or (gap <= 20 and overlap >= 2)
            or (gap <= 50 and sender_bonus and overlap >= 1)
            or (gap <= 20 and thread_bonus and overlap >= 1)
        )
        if not likely:
            continue
        score = overlap * 10 + sender_bonus + thread_bonus - min(gap, 100)
        candidate = (score, overlap, item)
        if best is None or candidate[0] > best[0]:
            best = candidate

    if best is not None:
        return "LIKELY_REACTION", best[2], best[1], len(foreign)

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

            classification, candidate, overlap, foreign_count = classify_reaction(
                our_seq=our_seq,
                our_text=str(row["text"]),
                target_agent=str(row["target_agent"]),
                messages=messages,
                self_dids=self_dids,
            )
            totals[classification] += 1
            print(
                f"RESULT: {classification} "
                f"foreign_posts_in_window={foreign_count}"
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
            "the fetched room window. It is not counted as a reply."
        )
    finally:
        con.close()


if __name__ == "__main__":
    main()
