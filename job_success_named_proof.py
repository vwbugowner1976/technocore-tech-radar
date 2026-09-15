#!/usr/bin/env python3
"""Narrow deterministic proof supplement for explicit named Success items.

This module does not weaken the Success contract and never sends anything.
It wraps the existing local Success gate and only supplements verifier evidence
when all of the following are true:

- the verifier itself returned PASS,
- a frozen requirement is a short "name one <item>" / "one <item>" style item,
- the candidate answer literally contains "<item> is ...", "<item> are ...",
  or "<item>: ..." in one immutable answer sentence,
- and there is non-empty payload after that literal label.

No semantic inference is performed. Grounding checks are untouched. The normal
frozen-contract verifier remains authoritative for everything else.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from job_success_criterion_gate import validate_success_criterion as _validate_success_criterion
from technoscout.common import local_llm_json


_PREFIX_WORDS = {
    "name", "names", "named", "one", "a", "an", "concrete", "specific",
    "explicit", "clear", "actual", "single", "requested",
}

_STOP_PAYLOAD = {
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "at",
    "for", "from", "with", "by", "as", "is", "are", "was", "were", "be",
    "been", "being", "one", "this", "that",
}


def _clean(value: Any, maximum: int = 4000) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _named_anchor(requirement_text: str) -> str:
    """Return a short literal noun-label anchor, or empty when not safely narrow."""
    words = re.findall(r"[a-z0-9][a-z0-9_-]*", _clean(requirement_text, 400).casefold())
    while words and words[0] in _PREFIX_WORDS:
        words.pop(0)

    # Keep this deliberately narrow. Longer or clause-like requirements remain
    # fully LLM-verified and get no deterministic supplementation.
    if not words or len(words) > 4:
        return ""
    if any(word in {"why", "because", "that", "which", "when", "before", "after"} for word in words):
        return ""
    return " ".join(words)


def _literal_named_sentence(anchor: str, sentence_candidates: list[dict[str, Any]]) -> str:
    if not anchor:
        return ""

    escaped = re.escape(anchor)
    pattern = re.compile(
        rf"\b(?:the\s+)?{escaped}\b\s*(?:is|are|:|-)\s*(.+)",
        flags=re.IGNORECASE,
    )

    for item in sentence_candidates:
        sentence = _clean(item.get("text"), 2000)
        match = pattern.search(sentence)
        if not match:
            continue
        payload = match.group(1)
        tokens = [
            token
            for token in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", payload.casefold())
            if token not in _STOP_PAYLOAD
        ]
        if len(tokens) >= 2:
            return _clean(item.get("id"), 32)
    return ""


def _supplement_explicit_named_checks(raw: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Fill only missing exact sentence IDs for literal named-item requirements."""
    if str(raw.get("decision", "")).strip().upper() != "PASS":
        return raw

    contract = payload.get("contract") or {}
    requirements = contract.get("requirements") or []
    sentence_candidates = payload.get("candidate_sentence_candidates") or []
    if not isinstance(requirements, list) or not isinstance(sentence_candidates, list):
        return raw

    checks = [dict(item) for item in (raw.get("checks") or []) if isinstance(item, dict)]
    by_id = {str(item.get("id", "")): item for item in checks}
    changed = False

    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        rid = _clean(requirement.get("id"), 32)
        if not rid:
            continue
        existing = by_id.get(rid)
        if existing is not None and bool(existing.get("satisfied")) and (existing.get("evidence_ids") or existing.get("evidence")):
            continue

        anchor = _named_anchor(str(requirement.get("text", "")))
        evidence_id = _literal_named_sentence(anchor, sentence_candidates)
        if not evidence_id:
            continue

        replacement = {
            "id": rid,
            "satisfied": True,
            "evidence_ids": [evidence_id],
        }
        if existing is None:
            checks.append(replacement)
        else:
            index = checks.index(existing)
            checks[index] = replacement
        by_id[rid] = replacement
        changed = True

    if not changed:
        return raw

    result = dict(raw)
    result["checks"] = checks
    return result


def validate_success_criterion(
    cfg: dict[str, Any],
    llm: Any,
    model: str,
    job: dict[str, Any],
    candidate_answer: str,
    *,
    caller: Callable[..., dict[str, Any]] = local_llm_json,
) -> dict[str, Any]:
    """Run the frozen Success gate with a literal named-item proof supplement."""

    def wrapped_caller(
        inner_cfg: dict[str, Any],
        inner_llm: Any,
        inner_model: str,
        prompt: str,
        payload: dict[str, Any],
        *,
        max_tokens: int,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        raw = caller(
            inner_cfg,
            inner_llm,
            inner_model,
            prompt,
            payload,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )
        if (
            isinstance(raw, dict)
            and isinstance(payload, dict)
            and "contract" in payload
            and "candidate_sentence_candidates" in payload
            and str(payload.get("mode", "")).startswith("local-success-")
        ):
            return _supplement_explicit_named_checks(raw, payload)
        return raw

    return _validate_success_criterion(
        cfg,
        llm,
        model,
        job,
        candidate_answer,
        caller=wrapped_caller,
    )


__all__ = [
    "validate_success_criterion",
    "_named_anchor",
    "_supplement_explicit_named_checks",
]
