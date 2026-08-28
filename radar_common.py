#!/usr/bin/env python3
"""Shared, read-only Technocore and local persistence helpers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
ROOM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config(path: str = "config.json") -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    with config_path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    base = str(config.get("base_url", "https://technocore.chat")).rstrip("/")
    if urllib.parse.urlsplit(base).scheme != "https" and not config.get("allow_http", False):
        raise ValueError("base_url must use HTTPS (or explicitly set allow_http for a local server)")
    config["base_url"] = base
    return config


def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def http_json(config: dict[str, Any], path: str, query: dict[str, Any] | None = None) -> Any:
    """GET only. Deliberately accepts a server path, never an untrusted URL."""
    url = config["base_url"] + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers={"User-Agent": "technocore-tech-radar/1"}, method="GET")
    limit = int(config.get("max_response_bytes", 5_000_000))
    with urllib.request.urlopen(request, timeout=float(config.get("http_timeout_seconds", 20))) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError("Technocore response exceeded configured limit")
    return json.loads(body.decode("utf-8"))


def room_messages(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("messages", payload.get("items", payload.get("events", [])))
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def seq_of(message: dict[str, Any]) -> int:
    try:
        return max(0, int(message.get("seq", 0)))
    except (TypeError, ValueError):
        return 0


def safe_room(value: Any) -> str | None:
    room = str(value or "").strip().lower()
    return room if ROOM_RE.fullmatch(room) and not room.startswith("p-") else None


def event_room(message: dict[str, Any]) -> str | None:
    for key in ("room", "name", "created_room"):
        if key in message:
            room = safe_room(message[key])
            if room and room != "events":
                return room
    text = str(message.get("text", message.get("message", "")))
    match = re.search(r"(?:created|new(?: public)? room)\s+([a-z0-9][a-z0-9_-]{0,47})", text, re.I)
    return safe_room(match.group(1)) if match else None


def compact_messages(messages: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    result = []
    for item in messages[-maximum:]:
        result.append({
            "seq": seq_of(item),
            "from": str(item.get("from", item.get("did", "")))[:200],
            "ts": item.get("ts", item.get("timestamp")),
            "text": str(item.get("text", item.get("message", "")))[:4096],
        })
    return result


def untrusted_block(value: Any) -> str:
    return "BEGIN_UNTRUSTED_DATA\n" + json.dumps(value, ensure_ascii=False) + "\nEND_UNTRUSTED_DATA"


def codex_json(config: dict[str, Any], instructions: str, untrusted: Any | None = None) -> dict[str, Any]:
    prompt = instructions.strip()
    if untrusted is not None:
        prompt += "\n\n" + untrusted_block(untrusted)
    command = config.get("codex_command", ["codex", "exec", "--sandbox", "read-only", "--ephemeral", "-"])
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError("codex_command must be a non-empty string array")
    completed = subprocess.run(
        command, input=prompt, text=True, capture_output=True,
        timeout=float(config.get("codex_timeout_seconds", 180)), check=False,
        cwd=ROOT, env={**os.environ, "NO_COLOR": "1"},
    )
    if completed.returncode:
        raise RuntimeError(f"Codex CLI failed with exit code {completed.returncode}")
    text = completed.stdout.strip()
    decoder = json.JSONDecoder()
    candidates = [text]
    candidates.extend(line.strip() for line in reversed(text.splitlines()) if line.strip())
    for candidate in candidates:
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
        try:
            value, _ = decoder.raw_decode(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    raise ValueError("Codex CLI did not return a JSON object")


def topics_by_room(config: dict[str, Any]) -> dict[str, str]:
    payload = http_json(config, "/rooms", {"format": "json", "limit": 512})
    items = payload if isinstance(payload, list) else payload.get("rooms", []) if isinstance(payload, dict) else []
    result: dict[str, str] = {}
    for item in items:
        if isinstance(item, dict):
            room = safe_room(item.get("room", item.get("name")))
            if room:
                result[room] = str(item.get("topic", ""))[:4096]
    return result
