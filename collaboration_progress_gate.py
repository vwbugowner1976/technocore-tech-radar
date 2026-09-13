#!/usr/bin/env python3
"""Read-only gate for deciding when collaboration-aware targeting deserves a controlled trial."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any

from technoscout.db import (
    collaboration_shadow_counts,
    collaboration_shadow_evaluation_counts,
    connect,
    reaction_memory_counts,
)
from technoscout_cli import database_path, load_config


@dataclass(frozen=True)
class CollaborationGateResult:
    state: str
    ready_for_controlled_trial: bool
    reason: str
    metrics: dict[str, int]
    thresholds: dict[str, int]


def _count(con: Any, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = con.execute(sql, params).fetchone()
    return int((row["n"] if row else 0) or 0)


def collaboration_gate_metrics(con: Any) -> dict[str, int]:
    reaction = reaction_memory_counts(con)
    shadow = collaboration_shadow_counts(con)
    shadow_eval = collaboration_shadow_evaluation_counts(con)

    verified_sends = _count(
        con,
        "SELECT COUNT(*) AS n FROM send_attempts WHERE status='sent'",
    )
    reaction_total = _count(
        con,
        "SELECT COUNT(*) AS n FROM reaction_memory",
    )
    observed_windows = _count(
        con,
        """
        SELECT COUNT(*) AS n
        FROM reaction_memory
        WHERE coverage='OBSERVED'
          AND classification NOT IN ('READ_ERROR','WINDOW_TRUNCATED')
        """,
    )
    resolved_shadow = (
        int(shadow_eval.get("ACTUAL_REPLIED", 0))
        + int(shadow_eval.get("ACTUAL_NO_REPLY", 0))
    )
    would_prefer_resolved = _count(
        con,
        """
        SELECT COUNT(*) AS n
        FROM collaboration_shadow_evaluations e
        JOIN collaboration_shadow_decisions d ON d.id=e.decision_id
        WHERE d.marker='WOULD_PREFER'
          AND e.state IN ('ACTUAL_REPLIED','ACTUAL_NO_REPLY')
        """,
    )
    would_prefer_actual_no_reply = _count(
        con,
        """
        SELECT COUNT(*) AS n
        FROM collaboration_shadow_evaluations e
        JOIN collaboration_shadow_decisions d ON d.id=e.decision_id
        WHERE d.marker='WOULD_PREFER'
          AND e.state='ACTUAL_NO_REPLY'
        """,
    )
    would_prefer_actual_replied = _count(
        con,
        """
        SELECT COUNT(*) AS n
        FROM collaboration_shadow_evaluations e
        JOIN collaboration_shadow_decisions d ON d.id=e.decision_id
        WHERE d.marker='WOULD_PREFER'
          AND e.state='ACTUAL_REPLIED'
        """,
    )

    return {
        "verified_sends": verified_sends,
        "reaction_total": reaction_total,
        "observed_windows": observed_windows,
        "direct_replies": int(reaction.get("DIRECT_REPLY", 0)),
        "likely_reactions": int(reaction.get("LIKELY_REACTION", 0)),
        "qualifying_reactions": (
            int(reaction.get("DIRECT_REPLY", 0))
            + int(reaction.get("LIKELY_REACTION", 0))
        ),
        "shadow_decisions": int(sum(shadow.values())),
        "shadow_same": int(shadow.get("SAME", 0)),
        "shadow_would_prefer": int(shadow.get("WOULD_PREFER", 0)),
        "shadow_resolved": resolved_shadow,
        "shadow_unresolved": int(shadow_eval.get("UNRESOLVED", 0)),
        "would_prefer_resolved": would_prefer_resolved,
        "would_prefer_actual_no_reply": would_prefer_actual_no_reply,
        "would_prefer_actual_replied": would_prefer_actual_replied,
    }


def collaboration_gate_thresholds(cfg: dict[str, Any]) -> dict[str, int]:
    return {
        "verified_sends": max(
            1, int(cfg.get("collaboration_gate_min_verified_sends", 30))
        ),
        "reaction_total": max(
            1, int(cfg.get("collaboration_gate_min_reaction_memory", 25))
        ),
        "observed_windows": max(
            1, int(cfg.get("collaboration_gate_min_observed_windows", 15))
        ),
        "direct_replies": max(
            0, int(cfg.get("collaboration_gate_min_direct_replies", 2))
        ),
        "qualifying_reactions": max(
            1, int(cfg.get("collaboration_gate_min_qualifying_reactions", 5))
        ),
        "shadow_decisions": max(
            1, int(cfg.get("collaboration_gate_min_shadow_decisions", 10))
        ),
        "shadow_resolved": max(
            1, int(cfg.get("collaboration_gate_min_shadow_resolved", 8))
        ),
        "shadow_would_prefer": max(
            1, int(cfg.get("collaboration_gate_min_would_prefer", 3))
        ),
        "would_prefer_resolved": max(
            1, int(cfg.get("collaboration_gate_min_would_prefer_resolved", 2))
        ),
        "opportunity_rate_percent": max(
            0,
            min(
                100,
                int(
                    cfg.get(
                        "collaboration_gate_min_opportunity_rate_percent",
                        60,
                    )
                ),
            ),
        ),
    }


def evaluate_collaboration_gate(
    metrics: dict[str, int],
    thresholds: dict[str, int],
) -> CollaborationGateResult:
    base_keys = (
        "verified_sends",
        "reaction_total",
        "observed_windows",
        "direct_replies",
        "qualifying_reactions",
        "shadow_decisions",
        "shadow_resolved",
    )
    missing = [
        key
        for key in base_keys
        if int(metrics.get(key, 0)) < int(thresholds[key])
    ]
    if missing:
        reason = "collecting: " + ", ".join(
            f"{key}={metrics.get(key,0)}/{thresholds[key]}"
            for key in missing
        )
        return CollaborationGateResult(
            state="COLLECTING",
            ready_for_controlled_trial=False,
            reason=reason,
            metrics=metrics,
            thresholds=thresholds,
        )

    if int(metrics.get("shadow_would_prefer", 0)) < int(
        thresholds["shadow_would_prefer"]
    ):
        return CollaborationGateResult(
            state="NO_MATERIAL_DIFFERENCE",
            ready_for_controlled_trial=False,
            reason=(
                "enough baseline data, but collaboration-aware selection "
                f"rarely disagrees with relationship-only selection "
                f"({metrics.get('shadow_would_prefer',0)}/"
                f"{thresholds['shadow_would_prefer']})"
            ),
            metrics=metrics,
            thresholds=thresholds,
        )

    if int(metrics.get("would_prefer_resolved", 0)) < int(
        thresholds["would_prefer_resolved"]
    ):
        return CollaborationGateResult(
            state="WAITING_FOR_DISAGREEMENT_OUTCOMES",
            ready_for_controlled_trial=False,
            reason=(
                "shadow disagreements exist, but too few actual-target outcomes "
                f"are resolved ({metrics.get('would_prefer_resolved',0)}/"
                f"{thresholds['would_prefer_resolved']})"
            ),
            metrics=metrics,
            thresholds=thresholds,
        )

    resolved = max(1, int(metrics.get("would_prefer_resolved", 0)))
    opportunity_rate = round(
        100
        * int(metrics.get("would_prefer_actual_no_reply", 0))
        / resolved
    )
    if opportunity_rate < int(thresholds["opportunity_rate_percent"]):
        return CollaborationGateResult(
            state="HOLD",
            ready_for_controlled_trial=False,
            reason=(
                "when shadow disagreed, the existing target still replied too "
                "often to justify a trial; "
                f"opportunity_rate={opportunity_rate}% "
                f"< {thresholds['opportunity_rate_percent']}%"
            ),
            metrics=metrics,
            thresholds=thresholds,
        )

    return CollaborationGateResult(
        state="READY_FOR_CONTROLLED_TRIAL",
        ready_for_controlled_trial=True,
        reason=(
            "minimum evidence reached and shadow disagreements correlate with "
            "actual-target non-response often enough for a small controlled "
            f"trial; opportunity_rate={opportunity_rate}%"
        ),
        metrics=metrics,
        thresholds=thresholds,
    )


def evaluate_from_db(con: Any, cfg: dict[str, Any]) -> CollaborationGateResult:
    return evaluate_collaboration_gate(
        collaboration_gate_metrics(con),
        collaboration_gate_thresholds(cfg),
    )


def print_gate(result: CollaborationGateResult) -> None:
    m = result.metrics
    t = result.thresholds
    resolved = max(1, int(m.get("would_prefer_resolved", 0)))
    opportunity_rate = round(
        100 * int(m.get("would_prefer_actual_no_reply", 0)) / resolved
    ) if int(m.get("would_prefer_resolved", 0)) else 0

    print(
        "Collaboration Progress Gate | "
        f"state={result.state} "
        f"ready={'yes' if result.ready_for_controlled_trial else 'no'}"
    )
    print(f"reason={result.reason}")
    print(
        "  baseline | "
        f"sends={m['verified_sends']}/{t['verified_sends']} "
        f"memory={m['reaction_total']}/{t['reaction_total']} "
        f"observed={m['observed_windows']}/{t['observed_windows']} "
        f"direct={m['direct_replies']}/{t['direct_replies']} "
        f"qualifying={m['qualifying_reactions']}/{t['qualifying_reactions']}"
    )
    print(
        "  shadow | "
        f"decisions={m['shadow_decisions']}/{t['shadow_decisions']} "
        f"resolved={m['shadow_resolved']}/{t['shadow_resolved']} "
        f"same={m['shadow_same']} "
        f"would_prefer={m['shadow_would_prefer']}/{t['shadow_would_prefer']}"
    )
    print(
        "  disagreements | "
        f"resolved={m['would_prefer_resolved']}/{t['would_prefer_resolved']} "
        f"actual_no_reply={m['would_prefer_actual_no_reply']} "
        f"actual_replied={m['would_prefer_actual_replied']} "
        f"opportunity_rate={opportunity_rate}%/"
        f"{t['opportunity_rate_percent']}%"
    )
    print(
        "Gate note: READY_FOR_CONTROLLED_TRIAL is not permission to change "
        "autonomous targeting. It only means there is enough evidence to "
        "design a separate, bounded trial."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate collaboration-targeting progress gate"
    )
    parser.add_argument("--config", default="technoscout.config.json")
    args = parser.parse_args()

    cfg = load_config(args.config)
    con = connect(database_path(cfg))
    try:
        print_gate(evaluate_from_db(con, cfg))
    finally:
        con.close()


if __name__ == "__main__":
    main()
