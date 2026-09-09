#!/usr/bin/env python3
"""Deterministic autonomy policy for TechnoScout v0.8."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

URL_RE = re.compile(r"(?:https?://|www\.)", re.I)
JAPANESE_OR_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


@dataclass(frozen=True)
class AutonomyDecision:
    allowed: bool
    reason: str


def evaluate_autonomy(
    cfg: dict[str, Any],
    *,
    room: str,
    draft_text: str,
    signal_summary: str,
    tags: list[str],
    relevance: int,
    technical: int,
    relationship: int,
    evidence_seqs: list[int],
    recent_hour_sends: int,
    room_cooldown_ok: bool,
    agent_cooldown_ok: bool,
) -> AutonomyDecision:
    mode = str(cfg.get("autonomy_mode", "shadow")).strip().lower()
    if mode not in {"shadow", "limited"}:
        return AutonomyDecision(False, "autonomy mode is off")

    if not evidence_seqs:
        return AutonomyDecision(False, "no message evidence")

    if int(relevance) < int(cfg.get("autonomy_min_relevance", 75)):
        return AutonomyDecision(False, "relevance below threshold")
    if int(technical) < int(cfg.get("autonomy_min_technical", 75)):
        return AutonomyDecision(False, "technical score below threshold")
    if int(relationship) < int(cfg.get("autonomy_min_relationship", 30)):
        return AutonomyDecision(False, "relationship below threshold")

    if recent_hour_sends >= int(cfg.get("autonomy_max_sends_per_hour", 3)):
        return AutonomyDecision(False, "hourly autonomous send cap reached")
    if not room_cooldown_ok:
        return AutonomyDecision(False, "room cooldown active")
    if not agent_cooldown_ok:
        return AutonomyDecision(False, "agent cooldown active")

    text = str(draft_text).strip()
    if not text:
        return AutonomyDecision(False, "empty draft")
    if len(text) > int(cfg.get("autonomy_max_draft_chars", 600)):
        return AutonomyDecision(False, "draft too long")
    if URL_RE.search(text):
        return AutonomyDecision(False, "URLs are not allowed in autonomous posts")
    if JAPANESE_OR_CJK_RE.search(text):
        return AutonomyDecision(False, "autonomous outbound posts must be English")

    room_lower = room.lower()
    for term in cfg.get("autonomy_blocked_room_terms", []):
        term = str(term).strip().lower()
        if term and term in room_lower:
            return AutonomyDecision(False, f"blocked room term: {term}")

    combined = " ".join([
        text,
        str(signal_summary),
        " ".join(str(x) for x in tags),
    ]).lower()
    for term in cfg.get("autonomy_blocked_text_terms", []):
        term = str(term).strip().lower()
        if term and term in combined:
            return AutonomyDecision(False, f"blocked content term: {term}")

    # Autonomous replies are intentionally limited to lightweight technical
    # questions or qualified observations.
    lowered = text.lower()
    question_like = "?" in text
    qualified = any(
        phrase in lowered
        for phrase in (
            "it looks like",
            "it seems",
            "one possibility",
            "have you",
            "could you",
            "would you",
            "did you",
            "how ",
            "what ",
            "which ",
        )
    )
    if not (question_like or qualified):
        return AutonomyDecision(False, "not a lightweight question/qualified observation")

    return AutonomyDecision(True, "passed deterministic limited-autonomy policy")
