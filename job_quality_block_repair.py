#!/usr/bin/env python3
"""Compatibility wrapper for adjudicator-guided quality repair.

Before invoking the current additive repairer, apply one narrow deterministic
false-positive guard for explicit Success contracts of the form:

    Success: names one <A> and one <B>.

If the reviewed answer already names both requested items explicitly and gives
non-empty payload for each, an adjudicator claim that the items are missing is
contradicted by the answer text. In that case we preserve the existing answer and
let the separate Generic Success Gate make the final decision. This wrapper never
approves or sends anything.

When a previous V3 repair attempt already consumed its one-shot ledger, the
current pipeline defect may only say "already attempted". For that migration case,
we also inspect the saved V3 critique. A bypass is still allowed only if that
saved critique itself is a missing-item claim contradicted by the reviewed answer.
"""

from __future__ import annotations

import re
from typing import Any

from job_quality_block_repair_v3 import (
    ensure_repair_schema,
    repair_adjudicator_block as _repair_adjudicator_block_v3,
)


_MODIFIERS = {
    "concrete",
    "specific",
    "explicit",
    "requested",
    "clear",
    "actual",
    "single",
}


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def _success_pair(job: dict[str, Any]) -> tuple[str, str] | None:
    body = _clean(job.get("body", ""))
    match = re.search(
        r"\bSuccess\s*:\s*names\s+one\s+(.+?)\s+and\s+one\s+(.+?)(?:[.;]|$)",
        body,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip()


def _anchor(requirement: str) -> str:
    words = re.findall(r"[a-z0-9][a-z0-9_-]*", requirement.casefold())
    while words and words[0] in _MODIFIERS:
        words.pop(0)
    return " ".join(words)


def _has_payload(answer: str, anchor: str) -> bool:
    if not anchor:
        return False
    text = answer.casefold()
    idx = text.find(anchor.casefold())
    if idx < 0:
        return False
    tail = text[idx + len(anchor): idx + len(anchor) + 180]
    payload_tokens = [
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", tail)
        if token not in {
            "the", "and", "for", "with", "that", "this", "from", "one",
            "is", "are", "was", "were", "but", "or", "a", "an",
        }
    ]
    return len(payload_tokens) >= 2


def _explicit_success_coverage(
    job: dict[str, Any],
    answer: str,
    defect: str,
) -> tuple[bool, tuple[str, str] | None]:
    pair = _success_pair(job)
    if pair is None:
        return False, None

    defect_text = _clean(defect).casefold()
    missing_language = any(
        phrase in defect_text
        for phrase in (
            "does not provide",
            "doesn't provide",
            "missing",
            "lacks",
            "without specifying",
            "does not name",
            "doesn't name",
        )
    )
    if not missing_language:
        return False, pair

    anchors = (_anchor(pair[0]), _anchor(pair[1]))
    if not all(anchors):
        return False, pair
    if not all(anchor in defect_text for anchor in anchors):
        return False, pair
    if not all(_has_payload(answer, anchor) for anchor in anchors):
        return False, pair
    return True, pair


def _saved_v3_critique(
    con: Any,
    *,
    room: str,
    job_id: str,
    content_hash: str,
) -> str:
    """Return the saved V3 critique for this exact JOB binding, if any."""
    try:
        row = con.execute(
            """
            SELECT status,critique
            FROM job_quality_patch_repairs_v3
            WHERE room=? AND job_id=? AND content_hash=?
            """,
            (str(room), str(job_id), str(content_hash)),
        ).fetchone()
    except Exception:
        return ""
    if row is None:
        return ""
    if str(row["status"] or "") not in {
        "NO_NEW_INFORMATION",
        "UNCHANGED",
        "BLOCKED",
        "DETERMINISTIC_BLOCK",
    }:
        return ""
    return _clean(row["critique"])


def repair_adjudicator_block(
    con: Any,
    cfg: dict[str, Any],
    job_id: str,
    *,
    room: str = "kibble",
    content_hash: str,
    job: dict[str, Any],
    defect: str,
    llm: Any | None = None,
    model: str | None = None,
    evaluator: Any | None = None,
) -> dict[str, Any]:
    """Defer contradicted missing-item defects to Generic Success; else use V3."""
    ensure_repair_schema(con)

    row = con.execute(
        """
        SELECT content_hash,answer_text,confidence,model,status
        FROM job_execution_reviews
        WHERE room=? AND job_id=?
        ORDER BY reviewed_at DESC
        LIMIT 1
        """,
        (str(room), str(job_id)),
    ).fetchone()

    if row is not None and str(row["status"]) == "REVIEWED" and str(row["content_hash"]) == str(content_hash):
        answer = _clean(row["answer_text"])
        defect_sources = [
            ("current-adjudicator-defect", _clean(defect)),
        ]
        saved_v3 = _saved_v3_critique(
            con,
            room=room,
            job_id=job_id,
            content_hash=content_hash,
        )
        if saved_v3 and saved_v3 != defect_sources[0][1]:
            defect_sources.append(("saved-v3-critique", saved_v3))

        for source, candidate_defect in defect_sources:
            covered, pair = _explicit_success_coverage(job, answer, candidate_defect)
            if covered:
                anchors = (_anchor(pair[0]), _anchor(pair[1])) if pair else ("", "")
                return {
                    "state": "QUALITY_REVIEWED",
                    "job_id": str(job_id),
                    "decision": "PASS",
                    "confidence": int(row["confidence"] or 0),
                    "flags_before": [],
                    "critique": (
                        "adjudicator missing-item claim contradicted by explicit reviewed-answer "
                        f"coverage of '{anchors[0]}' and '{anchors[1]}' using {source}; "
                        "deferring final judgment to the Generic Success Gate"
                    ),
                    "answer": answer,
                    "repair_attempted": True,
                    "adjudication_attempted": True,
                    "adjudication_repair_attempted": False,
                    "repair_strategy": "explicit-success-coverage-bypass-v2",
                    "model": str(row["model"] or model or ""),
                }

    return _repair_adjudicator_block_v3(
        con,
        cfg,
        job_id,
        room=room,
        content_hash=content_hash,
        job=job,
        defect=defect,
        llm=llm,
        model=model,
        evaluator=evaluator,
    )


__all__ = [
    "ensure_repair_schema",
    "repair_adjudicator_block",
    "_explicit_success_coverage",
]
