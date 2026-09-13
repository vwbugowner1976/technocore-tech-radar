#!/usr/bin/env python3
"""Shadow-only collaboration preference scoring from persistent reaction memory."""

from __future__ import annotations

from typing import Any, Iterable


def collaboration_profile(con: Any, agent_id: str) -> dict[str, int]:
    agent = str(agent_id or "")[:240]
    if not agent:
        return {
            "score": 0,
            "responder_direct": 0,
            "responder_likely": 0,
            "target_observed": 0,
            "target_direct": 0,
            "target_likely": 0,
            "target_partial": 0,
            "rooms": 0,
        }

    responder = con.execute(
        """
        SELECT
          SUM(CASE WHEN classification='DIRECT_REPLY' THEN 1 ELSE 0 END) AS direct,
          SUM(CASE WHEN classification='LIKELY_REACTION' THEN 1 ELSE 0 END) AS likely,
          COUNT(DISTINCT room) AS rooms
        FROM reaction_memory
        WHERE responder_did=?
          AND classification IN ('DIRECT_REPLY','LIKELY_REACTION')
        """,
        (agent,),
    ).fetchone()

    target = con.execute(
        """
        SELECT
          SUM(CASE
                WHEN coverage='OBSERVED'
                 AND classification NOT IN ('READ_ERROR','WINDOW_TRUNCATED')
                THEN 1 ELSE 0 END) AS observed,
          SUM(CASE
                WHEN coverage='OBSERVED'
                 AND classification='DIRECT_REPLY'
                 AND responder_did=target_agent
                THEN 1 ELSE 0 END) AS direct,
          SUM(CASE
                WHEN coverage='OBSERVED'
                 AND classification='LIKELY_REACTION'
                 AND responder_did=target_agent
                THEN 1 ELSE 0 END) AS likely,
          SUM(CASE
                WHEN coverage='PARTIAL'
                  OR classification='WINDOW_TRUNCATED'
                THEN 1 ELSE 0 END) AS partial,
          COUNT(DISTINCT CASE
                WHEN coverage='OBSERVED' THEN room END) AS rooms
        FROM reaction_memory
        WHERE target_agent=?
        """,
        (agent,),
    ).fetchone()

    responder_direct = int((responder["direct"] if responder else 0) or 0)
    responder_likely = int((responder["likely"] if responder else 0) or 0)
    target_observed = int((target["observed"] if target else 0) or 0)
    target_direct = int((target["direct"] if target else 0) or 0)
    target_likely = int((target["likely"] if target else 0) or 0)
    target_partial = int((target["partial"] if target else 0) or 0)
    rooms = max(
        int((responder["rooms"] if responder else 0) or 0),
        int((target["rooms"] if target else 0) or 0),
    )

    # Conservative and intentionally slow-growing. One merely-likely reaction
    # is evidence, but is not enough to dominate established relationship memory.
    responder_points = min(
        55,
        responder_direct * 30 + responder_likely * 15,
    )
    if target_observed > 0:
        target_rate_points = round(
            30 * (
                target_direct * 1.0 + target_likely * 0.6
            ) / target_observed
        )
    else:
        target_rate_points = 0
    breadth_points = min(15, rooms * 5)

    score = max(
        0,
        min(100, responder_points + target_rate_points + breadth_points),
    )
    return {
        "score": int(score),
        "responder_direct": responder_direct,
        "responder_likely": responder_likely,
        "target_observed": target_observed,
        "target_direct": target_direct,
        "target_likely": target_likely,
        "target_partial": target_partial,
        "rooms": rooms,
    }


def shadow_candidate(
    con: Any,
    candidates: Iterable[str],
    *,
    relationship_lookup: Any,
    collaboration_weight_percent: int = 35,
) -> dict[str, Any] | None:
    unique = sorted({str(value) for value in candidates if str(value).strip()})
    if not unique:
        return None

    weight = max(0, min(100, int(collaboration_weight_percent)))
    rows: list[dict[str, Any]] = []
    for agent_id in unique:
        relationship = relationship_lookup(agent_id)
        relationship_score = int(relationship.get("score", 0))
        collaboration = collaboration_profile(con, agent_id)
        collaboration_score = int(collaboration["score"])
        combined = round(
            relationship_score * (100 - weight) / 100
            + collaboration_score * weight / 100
        )
        rows.append(
            {
                "agent_id": agent_id,
                "relationship": relationship_score,
                "collaboration": collaboration_score,
                "combined": int(combined),
                "evidence": collaboration,
            }
        )

    best = max(
        rows,
        key=lambda item: (
            item["combined"],
            item["collaboration"],
            item["relationship"],
            item["agent_id"],
        ),
    )
    best["candidate_count"] = len(rows)
    best["has_reaction_evidence"] = any(
        int(row["collaboration"]) > 0 for row in rows
    )
    return best
