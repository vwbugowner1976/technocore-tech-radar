#!/usr/bin/env python3
"""Small, fail-soft notification helpers for TechnoScout.

Notifications are a local UX side effect only. They never CLAIM work, write to
Technocore, execute job content, or include raw JOB title/body. The ntfy target is
configured privately (technoscout.config.json) so endpoint details are not stored
in the repository.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


_JOB_ID_RE = re.compile(r"^k[0-9a-f]{10}$")


def notify_ready_candidate(
    cfg: dict[str, Any],
    job_id: str,
    *,
    opener: Callable[..., Any] | None = None,
) -> dict[str, str]:
    """Publish one minimal READY notification to a configured ntfy topic.

    The endpoint must be an explicit http(s) URL such as
    ``http://127.0.0.1:2586/technoscout-ready``. Only the job ID is sent; raw
    untrusted JOB text is deliberately excluded. Any network failure is returned
    as FAILED and should not make the read-only watcher fail.
    """
    url = str(cfg.get("job_ready_ntfy_url", "") or "").strip()
    if not url:
        return {"state": "DISABLED", "detail": "job_ready_ntfy_url is not configured"}

    if not _JOB_ID_RE.fullmatch(str(job_id or "")):
        return {"state": "SKIPPED", "detail": "invalid READY job id"}

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {"state": "FAILED", "detail": "job_ready_ntfy_url must be an absolute http(s) URL"}
    if parsed.username or parsed.password:
        return {"state": "FAILED", "detail": "credentials in notification URL are not allowed"}

    timeout = float(cfg.get("job_ready_ntfy_timeout_seconds", 5.0))
    timeout = max(1.0, min(15.0, timeout))
    title = str(cfg.get("job_ready_ntfy_title", "TechnoScout") or "TechnoScout")[:80]
    message = f"READY candidate={job_id}"
    request = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": "robot",
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
        return {"state": "PUBLISHED_LOCAL", "detail": job_id}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        return {"state": "FAILED", "detail": f"{type(exc).__name__}: {exc}"[:300]}
    except Exception as exc:  # notifier must never crash the watcher
        return {"state": "FAILED", "detail": f"{type(exc).__name__}: {exc}"[:300]}
