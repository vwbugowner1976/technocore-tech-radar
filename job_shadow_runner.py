#!/usr/bin/env python3
"""One-shot Job Scout runner for launchd. Read-only against Technocore."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from job_shadow import print_job_shadow_status, sync_job_shadow
from job_shadow_policy import deterministic_shadow_evaluator
from technoscout import load_config, database_path
from technoscout.common import technocore_json
from technoscout.db import connect


def latest_then_since_fetcher(
    cfg: dict[str, Any],
    path: str,
    query: dict[str, Any],
) -> Any:
    """Initial scan reads the latest window; later scans continue from cursor."""
    safe_query = dict(query)
    if int(safe_query.get("since", 0) or 0) <= 0:
        safe_query.pop("since", None)
    return technocore_json(cfg, path, safe_query)


def run_once(config_path: str, verbose: bool = False) -> int:
    cfg = load_config(config_path)
    con = connect(database_path(cfg))
    try:
        stats = sync_job_shadow(
            con,
            cfg,
            llm=None,
            model="",
            fetcher=latest_then_since_fetcher,
            evaluator=deterministic_shadow_evaluator,
            verbose=verbose,
        )
        if stats.get("messages", 0) or stats.get("errors", 0):
            print(
                "[job-shadow-run] "
                f"messages={stats.get('messages',0)} "
                f"jobs={stats.get('jobs',0)} "
                f"evaluated={stats.get('evaluated',0)} "
                f"inserted={stats.get('inserted',0)} "
                f"updated={stats.get('updated',0)} "
                f"lifecycle={stats.get('lifecycle_updates',0)} "
                f"errors={stats.get('errors',0)}",
                flush=True,
            )
        return 0
    except Exception as exc:
        print(
            f"[job-shadow-run] ERROR {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one read-only Job Scout shadow collection pass"
    )
    parser.add_argument("--config", default="technoscout.config.json")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        cfg = load_config(args.config)
        con = connect(database_path(cfg))
        try:
            print_job_shadow_status(con, 25)
        finally:
            con.close()
        return

    raise SystemExit(run_once(args.config, verbose=args.verbose))


if __name__ == "__main__":
    main()
