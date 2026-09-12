#!/usr/bin/env python3
"""Small, fail-soft notification helpers for TechnoScout.

Notifications are a local UX side effect only. They never CLAIM work, write to
Technocore, execute job content, or include raw JOB title/body. The ntfy target is
configured privately (technoscout.config.json) so endpoint details are not stored
in the repository.

CLAIM_READY and DELIVERY_READY notifications contain a copy/paste-safe shell
snippet built only from a validated job id and fixed local commands. The snippet
still requires explicit human confirmation immediately before each signed send.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


_JOB_ID_RE = re.compile(r"^k[0-9a-f]{10}$")
_RESULT_RE = re.compile(r"[^a-z0-9_.+-]+")
_STAGE_RE = re.compile(r"[^a-z0-9_.+-]+")


def _publish_ntfy(
    cfg: dict[str, Any],
    *,
    url: str,
    title: str,
    message: str,
    tag: str,
    opener: Callable[..., Any] | None = None,
) -> dict[str, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {"state": "FAILED", "detail": "notification URL must be an absolute http(s) URL"}
    if parsed.username or parsed.password:
        return {"state": "FAILED", "detail": "credentials in notification URL are not allowed"}

    timeout = float(cfg.get("job_ready_ntfy_timeout_seconds", 5.0))
    timeout = max(1.0, min(15.0, timeout))
    request = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": title[:80],
            "Priority": "high",
            "Tags": tag[:80],
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    open_call = opener or urllib.request.urlopen
    try:
        response = open_call(request, timeout=timeout)
        status = getattr(response, "status", None)
        close = getattr(response, "close", None)
        if callable(close):
            close()
        if status is not None and not (200 <= int(status) < 300):
            return {"state": "FAILED", "detail": f"ntfy HTTP {status}"}
        return {"state": "PUBLISHED_LOCAL", "detail": message[:200]}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        return {"state": "FAILED", "detail": f"{type(exc).__name__}: {exc}"[:300]}
    except Exception as exc:  # notifier must never crash the watcher
        return {"state": "FAILED", "detail": f"{type(exc).__name__}: {exc}"[:300]}


def _human_action_notice(
    cfg: dict[str, Any],
    job_id: str,
    *,
    message: str,
    tag: str,
    opener: Callable[..., Any] | None = None,
) -> dict[str, str]:
    url = str(cfg.get("job_ready_ntfy_url", "") or "").strip()
    if not url:
        return {"state": "DISABLED", "detail": "job_ready_ntfy_url is not configured"}
    if not _JOB_ID_RE.fullmatch(str(job_id or "")):
        return {"state": "SKIPPED", "detail": "invalid job id"}

    title = str(cfg.get("job_ready_ntfy_title", "TechnoScout") or "TechnoScout")
    return _publish_ntfy(
        cfg,
        url=url,
        title=title,
        message=message,
        tag=tag,
        opener=opener,
    )


def _claim_shell_message(job_id: str) -> str:
    command = (
        'cd "$HOME/technocore-tech-radar"; '
        f'.venv/bin/python job_claim_trial.py prepare {job_id}; '
        f"printf 'CLAIMを承認するなら {job_id} を入力: '; "
        'IFS= read -r TS_JOB </dev/tty; '
        f'if [ "$TS_JOB" = "{job_id}" ]; then '
        f'.venv/bin/python job_claim_trial.py approve {job_id}; '
        "printf 'CLAIMを送信するなら SEND と入力: '; "
        'IFS= read -r TS_SEND </dev/tty; '
        'if [ "$TS_SEND" = "SEND" ]; then '
        f'.venv/bin/python job_claim_trial.py send {job_id}; '
        "else echo 'CLAIMは送信しませんでした'; fi; "
        "else echo 'CLAIMは承認しませんでした'; fi; "
        'unset TS_JOB TS_SEND'
    )
    return (
        f"# CLAIM_READY job={job_id}\n"
        "# この通知を丸ごとMac Terminalへ貼り付け\n"
        + command
    )


def _delivery_shell_message(job_id: str) -> str:
    command = (
        'cd "$HOME/technocore-tech-radar"; '
        f'.venv/bin/python job_delivery_trial.py prepare {job_id}; '
        f"printf 'DELIVERを承認するなら {job_id} を入力: '; "
        'IFS= read -r TS_JOB </dev/tty; '
        f'if [ "$TS_JOB" = "{job_id}" ]; then '
        f'.venv/bin/python job_delivery_trial.py approve {job_id}; '
        "printf 'DELIVERを送信するなら SEND と入力: '; "
        'IFS= read -r TS_SEND </dev/tty; '
        'if [ "$TS_SEND" = "SEND" ]; then '
        f'.venv/bin/python job_delivery_trial.py send {job_id}; '
        "else echo 'DELIVERは送信しませんでした'; fi; "
        "else echo 'DELIVERは承認しませんでした'; fi; "
        'unset TS_JOB TS_SEND'
    )
    return (
        f"# DELIVERY_READY job={job_id}\n"
        "# この通知を丸ごとMac Terminalへ貼り付け\n"
        + command
    )


def notify_ready_candidate(
    cfg: dict[str, Any],
    job_id: str,
    *,
    opener: Callable[..., Any] | None = None,
) -> dict[str, str]:
    """Legacy READY notification kept for compatibility."""
    return _human_action_notice(
        cfg,
        job_id,
        message=f"READY candidate={job_id}",
        tag="robot",
        opener=opener,
    )


def notify_claim_ready(
    cfg: dict[str, Any],
    job_id: str,
    *,
    opener: Callable[..., Any] | None = None,
) -> dict[str, str]:
    """Publish a copy/paste-safe interactive CLAIM workflow for Mac Terminal."""
    return _human_action_notice(
        cfg,
        job_id,
        message=_claim_shell_message(job_id),
        tag="hand",
        opener=opener,
    )


def notify_delivery_ready(
    cfg: dict[str, Any],
    job_id: str,
    *,
    opener: Callable[..., Any] | None = None,
) -> dict[str, str]:
    """Publish a copy/paste-safe interactive DELIVER workflow for Mac Terminal."""
    return _human_action_notice(
        cfg,
        job_id,
        message=_delivery_shell_message(job_id),
        tag="outbox_tray",
        opener=opener,
    )


def notify_job_blocked(
    cfg: dict[str, Any],
    job_id: str,
    stage: str,
    *,
    opener: Callable[..., Any] | None = None,
) -> dict[str, str]:
    """Publish a minimal blocked-state notification without raw JOB data."""
    safe_stage = _STAGE_RE.sub("-", str(stage or "unknown").strip().lower()).strip("-")[:32]
    if not safe_stage:
        safe_stage = "unknown"
    return _human_action_notice(
        cfg,
        job_id,
        message=f"BLOCKED job={job_id} stage={safe_stage}",
        tag="warning",
        opener=opener,
    )


def notify_job_attest(
    cfg: dict[str, Any],
    job_id: str,
    result: str,
    *,
    opener: Callable[..., Any] | None = None,
) -> dict[str, str]:
    """Publish one minimal ATTEST notification for our own delivered job.

    Raw ATTEST payload is never sent to ntfy. ``result`` is reduced to a short
    safe label such as ``useful``. By default this reuses the READY topic so the
    Android subscription does not need to change.
    """
    url = str(
        cfg.get("job_attest_ntfy_url")
        or cfg.get("job_ready_ntfy_url")
        or ""
    ).strip()
    if not url:
        return {"state": "DISABLED", "detail": "job attest/ready ntfy URL is not configured"}
    if not _JOB_ID_RE.fullmatch(str(job_id or "")):
        return {"state": "SKIPPED", "detail": "invalid ATTEST job id"}

    safe_result = _RESULT_RE.sub("-", str(result or "").strip().lower()).strip("-")[:32]
    if not safe_result:
        safe_result = "attested"
    title = str(
        cfg.get("job_attest_ntfy_title")
        or cfg.get("job_ready_ntfy_title")
        or "TechnoScout"
    )
    return _publish_ntfy(
        cfg,
        url=url,
        title=title,
        message=f"ATTEST job={job_id} result={safe_result}",
        tag="white_check_mark",
        opener=opener,
    )
