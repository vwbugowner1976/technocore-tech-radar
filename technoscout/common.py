#!/usr/bin/env python3
"""Shared helpers for TechnoScout v0.1."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

ROOM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_room(value: Any) -> str | None:
    room = str(value or "").strip().lower()
    return room if ROOM_RE.fullmatch(room) else None


def seq_of(item: dict[str, Any]) -> int:
    try:
        return max(0, int(item.get("seq", 0)))
    except (TypeError, ValueError):
        return 0


def room_messages(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("messages", payload.get("items", payload.get("events", [])))
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def compact_messages(messages: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    result = []
    for item in messages[-maximum:]:
        result.append({
            "seq": seq_of(item),
            "from": str(item.get("from", item.get("did", "")))[:200],
            "ts": item.get("ts", item.get("timestamp")),
            "text": str(item.get("text", item.get("message", "")))[:1200],
        })
    return result


def event_room(item: dict[str, Any]) -> str | None:
    for key in ("room", "name", "created_room"):
        if key in item:
            room = safe_room(item[key])
            if room and room != "events":
                return room
    text = str(item.get("text", item.get("message", "")))
    match = re.search(
        r"(?:created|new(?: public)? room)\s+([a-z0-9][a-z0-9_-]{0,47})",
        text,
        re.I,
    )
    return safe_room(match.group(1)) if match else None


class RateLimited(RuntimeError):
    def __init__(self, wait_seconds: float) -> None:
        super().__init__(f"rate limited; wait {wait_seconds:.1f}s")
        self.wait_seconds = wait_seconds


def _rate_wait(body: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\b", body, re.I)
    if match:
        return max(1.0, min(300.0, float(match.group(1))))
    match = re.search(r"\b(?:retry|wait|back\s*off)\D{0,20}(\d+(?:\.\d+)?)\b", body, re.I)
    if match:
        return max(1.0, min(300.0, float(match.group(1))))
    return 30.0


def technocore_json(cfg: dict[str, Any], path: str, query: dict[str, Any] | None = None) -> Any:
    """GET only. The caller supplies a validated server path, never an external URL."""
    if not path.startswith("/") or "://" in path:
        raise ValueError("invalid Technocore path")
    url = cfg["base_url"] + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "technoscout/0.1", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(cfg["http_timeout_seconds"])) as response:
            body = response.read(int(cfg["max_response_bytes"]) + 1)
    except urllib.error.HTTPError as exc:
        body = exc.read(16384).decode("utf-8", "replace")
        if exc.code == 429:
            raise RateLimited(_rate_wait(body)) from exc
        raise RuntimeError(f"Technocore HTTP {exc.code}: {body[:300]}") from exc
    if len(body) > int(cfg["max_response_bytes"]):
        raise ValueError("Technocore response exceeded configured limit")
    return json.loads(body.decode("utf-8"))


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    decoder = json.JSONDecoder()
    for candidate in [cleaned] + [x.strip() for x in reversed(cleaned.splitlines()) if x.strip()]:
        try:
            value, _ = decoder.raw_decode(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        value = json.loads(cleaned[start:end + 1])
        if isinstance(value, dict):
            return value
    raise ValueError("LLM did not return a JSON object")


def local_llm_json(
    cfg: dict[str, Any],
    model: str,
    system_prompt: str,
    untrusted_data: Any,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": int(cfg.get("llm_max_tokens", 256)),
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": "BEGIN_UNTRUSTED_TECHNOCORE_DATA\n"
                + json.dumps(untrusted_data, ensure_ascii=False)
                + "\nEND_UNTRUSTED_TECHNOCORE_DATA",
            },
        ],
    }
    request = urllib.request.Request(
        cfg["llm_base_url"] + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=float(cfg["llm_timeout_seconds"])) as response:
        raw = json.loads(response.read(int(cfg["max_response_bytes"])).decode("utf-8"))
    return parse_json_object(raw["choices"][0]["message"]["content"])


def available_models(cfg: dict[str, Any]) -> list[str]:
    request = urllib.request.Request(
        cfg["llm_base_url"] + "/models",
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=float(cfg["llm_timeout_seconds"])) as response:
        payload = json.loads(response.read(int(cfg["max_response_bytes"])).decode("utf-8"))
    items = payload.get("data", []) if isinstance(payload, dict) else []
    return [str(item["id"]) for item in items if isinstance(item, dict) and item.get("id")]


def _model_size(name: str) -> float:
    values = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*[bB]\b", name)]
    return max(values) if values else 0.0


def resolve_models(cfg: dict[str, Any]) -> tuple[str, str]:
    models = available_models(cfg)
    triage = str(cfg.get("triage_model") or "")
    research = str(cfg.get("research_model") or "")
    if not models and not triage:
        raise RuntimeError("no model found at local /v1/models")
    if not triage:
        triage = min(models, key=lambda x: (_model_size(x) or 10000, x))
    if not research:
        research = max(models, key=lambda x: (_model_size(x), x))
    return triage, research


def clamp_score(value: Any) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0
