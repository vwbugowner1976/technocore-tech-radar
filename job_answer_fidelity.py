from __future__ import annotations

import re
from typing import Any


def _clean(value: Any, maximum: int = 8000) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _nonfactual(sentence: str) -> bool:
    s = sentence.lower()
    return any(x in s for x in ("for example", "could ", "would ", "should ", "might ", "if ", "alternative", "rejected", "recommend"))


def fidelity_flags(job: dict[str, Any], answer: str) -> list[str]:
    job_text = _clean(f"{job.get('title','')} {job.get('body','')}", 12000).lower()
    ans = _clean(answer)
    low = ans.lower()
    flags: list[str] = []

    labels = {
        "leading indicator:": ("leading indicator", "leading signal", "signal that shows up before"),
        "failure mode:": ("failure mode",),
        "recovery time target:": ("recovery time target", "rto"),
        "data-loss boundary:": ("data-loss boundary", "data loss boundary", "rpo"),
    }
    for label, triggers in labels.items():
        if label in low and not any(t in job_text for t in triggers):
            flags.append(f"unrelated template label: {label.rstrip(':')}")

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", ans) if s.strip()]
    if "gut feeling" in job_text or "gut-feeling" in job_text:
        for sentence in sentences:
            s = sentence.lower()
            historical = any(x in s for x in ("historical load data", "historical data", "previous quarter", "last quarter", "prior quarter"))
            asserts_source = any(x in s for x in ("threshold was set based on", "threshold is based on", "threshold was based on", "decision was based on", "threshold was derived from", "threshold was determined from"))
            if historical and asserts_source and not _nonfactual(sentence):
                flags.append("contradicts gut-feeling premise with a data-derived threshold")
                break

    for phrase in ("previous quarter", "last quarter", "prior quarter"):
        if phrase in low and phrase not in job_text:
            for sentence in sentences:
                if phrase in sentence.lower() and not _nonfactual(sentence):
                    flags.append(f"unsupported case-specific period: {phrase}")
                    break

    return list(dict.fromkeys(flags))
