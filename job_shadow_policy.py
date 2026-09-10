#!/usr/bin/env python3
"""Cheap deterministic policy for Job Scout before live job autonomy exists."""

from __future__ import annotations

import re
from typing import Any


SELF_CONTAINED_TYPES = {"explain", "summarize", "summary", "coordinate"}
TOOL_TYPES = {
    "research",
    "analyze",
    "analyse",
    "code",
    "debug",
    "review",
    "build",
    "test",
    "benchmark",
    "inspect",
}
COMPUTE_HINTS = (
    "large corpus",
    "long context",
    "deep reasoning",
    "many candidates",
    "large model",
    "multi-pass",
)
TOOL_HINTS = (
    "repository",
    "github",
    "website",
    "current data",
    "latest data",
    "run tests",
    "build firmware",
    "compile",
    "shell",
    "file",
    "download",
    "browse",
    "search the web",
)


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_+.-]{2,}", value.lower())
        if token not in {"the", "and", "for", "with", "from", "this", "that", "job"}
    }


def deterministic_shadow_evaluator(
    cfg: dict[str, Any],
    llm: Any,
    model: str,
    prompt: str,
    untrusted_data: dict[str, Any],
    **_: Any,
) -> dict[str, Any]:
    """Return a fit estimate without executing, fetching, solving, or calling an LLM."""
    job = untrusted_data.get("job", {}) if isinstance(untrusted_data, dict) else {}
    job_type = str(job.get("type", "")).strip().lower()
    title = str(job.get("title", ""))
    body = str(job.get("body", ""))
    text = f"{job_type} {title} {body}".lower()

    configured = " ".join(str(x) for x in cfg.get("prefilter_keywords", []))
    context = str(cfg.get("project_context", "")) + " " + configured
    overlap = len(_tokens(text) & _tokens(context))
    relevance = min(100, 35 + overlap * 12) if overlap else 25

    if any(hint in text for hint in TOOL_HINTS) or job_type in TOOL_TYPES:
        return {
            "class": "NEEDS_TOOL",
            "relevance": relevance,
            "technical_fit": 70 if overlap else 45,
            "confidence": 85,
            "effort": "medium",
            "required_capabilities": ["tool-execution"],
            "reason": "job appears to require external data, files, code execution, build/test, or another tool",
            "summary": "",
        }

    if any(hint in text for hint in COMPUTE_HINTS) or len(body) > 2200:
        return {
            "class": "NEEDS_COMPUTE",
            "relevance": relevance,
            "technical_fit": 65 if overlap else 40,
            "confidence": 70,
            "effort": "large",
            "required_capabilities": ["stronger-inference"],
            "reason": "job appears self-contained but may need more inference capacity or context than the lightweight path",
            "summary": "",
        }

    if job_type in SELF_CONTAINED_TYPES:
        if overlap:
            return {
                "class": "FIT",
                "relevance": relevance,
                "technical_fit": min(95, 65 + overlap * 6),
                "confidence": 75,
                "effort": "small",
                "required_capabilities": ["local-llm"],
                "reason": "self-contained job type with overlap to configured TechnoScout interests",
                "summary": "",
            }
        return {
            "class": "NOT_RELEVANT",
            "relevance": relevance,
            "technical_fit": 30,
            "confidence": 65,
            "effort": "small",
            "required_capabilities": ["local-llm"],
            "reason": "self-contained work, but no clear overlap with configured TechnoScout interests",
            "summary": "",
        }

    return {
        "class": "LOW_CONFIDENCE",
        "relevance": relevance,
        "technical_fit": 40 if overlap else 20,
        "confidence": 35,
        "effort": "unknown",
        "required_capabilities": [],
        "reason": "unknown Kibble job type; keep shadow-only until more evidence is available",
        "summary": "",
    }
