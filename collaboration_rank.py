#!/usr/bin/env python3
"""Read-only collaboration ranking from verified TechnoScout posts and reactions."""

from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from reaction_tracker import (
    classify_reaction,
    fetch_room_after,
    known_self_dids,
    reaction_window_truncated,
    sender_of,
    sent_seq,
)
from technoscout_cli import database_path, load_config


@dataclass
class ResponderStats:
    agent_id: str
    direct: int = 0
    likely: int = 0
    target_matches: int = 0
    overlap_total: int = 0
    rooms: set[str] = field(default_factory=set)

    @property
    def score(self) -> int:
        value = (
            self.direct * 45
            + self.likely * 25
            + self.target_matches * 15
            + min(15, len(self.rooms) * 5)
            + min(10, self.overlap_total * 2)
        )
        return max(0, min(100, int(value)))


@dataclass
class TargetStats:
    agent_id: str
    sends: int = 0
    observed: int = 0
    partial: int = 0
    direct: int = 0
    likely: int = 0
    room_activity: int = 0
    no_reaction: int = 0
    read_error: int = 0
    rooms: set[str] = field(default_factory=set)

    @property
    def score(self) -> int:
        if self.observed <= 0:
            return 0
        weighted = self.direct * 100 + self.likely * 60
        return max(0, min(100, round(weighted / self.observed)))


def sent_rows(con: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return con.execute(
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
        (max(1, int(limit)),),
    ).fetchall()


def rank_collaboration(
    rows: list[Any],
    *,
    self_dids: set[str],
    message_limit: int,
    fetcher: Callable[[str, int, int], list[dict[str, Any]]],
) -> tuple[
    dict[str, ResponderStats],
    dict[str, TargetStats],
    dict[str, int],
]:
    responders: dict[str, ResponderStats] = {}
    targets: dict[str, TargetStats] = {}
    totals: dict[str, int] = defaultdict(int)

    for row in rows:
        our_seq = sent_seq(str(row["detail"]))
        if our_seq is None:
            totals["NO_SEQ"] += 1
            continue

        room = str(row["room"])
        target_agent = str(row["target_agent"] or "")
        if target_agent:
            target = targets.setdefault(
                target_agent,
                TargetStats(agent_id=target_agent),
            )
            target.sends += 1
            target.rooms.add(room)
        else:
            target = None

        try:
            messages = fetcher(room, our_seq, message_limit)
        except Exception:
            totals["READ_ERROR"] += 1
            if target is not None:
                target.read_error += 1
            continue

        truncated = reaction_window_truncated(
            messages,
            our_seq,
            message_limit,
        )
        classification, candidate, overlap, _ = classify_reaction(
            room=room,
            our_seq=our_seq,
            our_text=str(row["text"]),
            target_agent=target_agent,
            messages=messages,
            self_dids=self_dids,
            window_truncated=truncated,
        )
        totals[classification] += 1

        responder_id = sender_of(candidate) if candidate is not None else ""

        if target is not None:
            if classification == "WINDOW_TRUNCATED":
                target.partial += 1
            else:
                target.observed += 1
                if classification == "DIRECT_REPLY" and responder_id == target_agent:
                    target.direct += 1
                elif classification == "LIKELY_REACTION" and responder_id == target_agent:
                    target.likely += 1
                elif classification == "ROOM_ACTIVITY":
                    target.room_activity += 1
                elif classification == "NO_REACTION":
                    target.no_reaction += 1

        if (
            classification in {"DIRECT_REPLY", "LIKELY_REACTION"}
            and responder_id
            and responder_id not in self_dids
        ):
            responder = responders.setdefault(
                responder_id,
                ResponderStats(agent_id=responder_id),
            )
            responder.rooms.add(room)
            responder.overlap_total += int(overlap)
            if classification == "DIRECT_REPLY":
                responder.direct += 1
            else:
                responder.likely += 1
            if target_agent and responder_id == target_agent:
                responder.target_matches += 1

    return responders, targets, dict(totals)


def short_did(value: str) -> str:
    text = str(value or "")
    if len(text) <= 28:
        return text
    return text[:18] + "…" + text[-8:]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rank Technocore agents by observed conversational reactions"
    )
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--message-limit", type=int, default=200)
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()

    cfg = load_config(args.config)
    con = sqlite3.connect(database_path(cfg))
    con.row_factory = sqlite3.Row
    try:
        rows = sent_rows(con, args.limit)
        self_dids = known_self_dids(con, cfg)

        def fetcher(room: str, seq: int, limit: int) -> list[dict[str, Any]]:
            return fetch_room_after(cfg, room, seq, limit)

        responders, targets, totals = rank_collaboration(
            rows,
            self_dids=self_dids,
            message_limit=max(1, min(200, args.message_limit)),
            fetcher=fetcher,
        )

        print(
            "Collaboration Ranking | "
            f"verified_posts={len(rows)} self_dids={len(self_dids)} "
            + " ".join(
                f"{key}={totals.get(key, 0)}"
                for key in (
                    "DIRECT_REPLY",
                    "LIKELY_REACTION",
                    "ROOM_ACTIVITY",
                    "WINDOW_TRUNCATED",
                    "NO_REACTION",
                    "READ_ERROR",
                )
            )
        )

        ranked_responders = sorted(
            responders.values(),
            key=lambda item: (
                item.score,
                item.direct,
                item.likely,
                item.target_matches,
                len(item.rooms),
            ),
            reverse=True,
        )[: max(1, args.top)]

        print()
        print(f"Responder Agents | {len(ranked_responders)} shown")
        if not ranked_responders:
            print("  none yet")
        for index, item in enumerate(ranked_responders, start=1):
            print(
                f"  #{index} score={item.score:3d} "
                f"direct={item.direct} likely={item.likely} "
                f"target_match={item.target_matches} "
                f"rooms={len(item.rooms)} overlap={item.overlap_total} "
                f"did={short_did(item.agent_id)}"
            )

        ranked_targets = sorted(
            targets.values(),
            key=lambda item: (
                item.score,
                item.observed,
                item.direct,
                item.likely,
                -item.partial,
            ),
            reverse=True,
        )[: max(1, args.top)]

        print()
        print(f"Target Agents | {len(ranked_targets)} shown")
        for index, item in enumerate(ranked_targets, start=1):
            print(
                f"  #{index} score={item.score:3d} sends={item.sends} "
                f"observed={item.observed} partial={item.partial} "
                f"direct={item.direct} likely={item.likely} "
                f"room_activity={item.room_activity} "
                f"no_reaction={item.no_reaction} "
                f"did={short_did(item.agent_id)}"
            )

        print()
        print(
            "Scoring note: responder score rewards explicit/likely technical "
            "reactions and repeated room evidence. Target score uses only "
            "fully observed windows; partial busy-room windows are excluded "
            "rather than counted as failures."
        )
    finally:
        con.close()


if __name__ == "__main__":
    main()
