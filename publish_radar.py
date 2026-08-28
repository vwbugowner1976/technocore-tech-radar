#!/usr/bin/env python3
"""Publish only a locally generated Tech Radar summary to the configured hub."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from radar_common import ROOT, load_config


SOURCE = "Source: Raspberry Pi / OpenMediaVault 7 / aarch64"
TEST_MESSAGE = (
    "[TECH-RADAR TEST] Source: Raspberry Pi / OpenMediaVault 7 / aarch64 | "
    "Technocore Tech Radar is now running from an always-on Raspberry Pi. "
    "Scout, Watch, Daily Radar, and hourly publishing are being tested from this node."
)
STATE_PATH = ROOT / "publish-state.json"


def load_env(path: Path) -> dict[str, str]:
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def normalize_message(value: str, limit: int) -> str:
    swept = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co", "Zl", "Zp"} else char
        for char in value
    )
    return " ".join(swept.split())[: min(limit, 4096)].strip()


def radar_message(config: dict, target: str) -> str:
    radar_path = ROOT / config.get("radar_dir", "radar") / f"{target}.json"
    with radar_path.open(encoding="utf-8") as handle:
        radar = json.load(handle)
    # Only daily_radar.py output fields are used. Raw observations/posts are not loaded.
    parts = [f"[TECH-RADAR] {SOURCE}", f"{radar.get('title', '')} — {radar.get('overview', '')}"]
    for item in radar.get("items", []):
        if isinstance(item, dict):
            parts.append(f"{item.get('headline', '')}: {item.get('summary', '')}")
    return normalize_message(" | ".join(parts), int(config.get("publish_max_chars", 4096)))


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def previous_hash() -> str | None:
    try:
        with STATE_PATH.open(encoding="utf-8") as handle:
            state = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    value = state.get("sha256") if isinstance(state, dict) else None
    return value if isinstance(value, str) else None


def save_successful_hash(digest: str) -> None:
    temporary = STATE_PATH.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump({"sha256": digest}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, STATE_PATH)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--date")
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--test-source", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    room = config.get("hub_room")
    if not isinstance(room, str) or not room.strip():
        raise RuntimeError("hub_room is missing from config.json")
    room = room.strip()

    if args.test_source:
        text = normalize_message(TEST_MESSAGE, int(config.get("publish_max_chars", 4096)))
    else:
        target = args.date or datetime.now(
            ZoneInfo(config.get("timezone", "Asia/Tokyo"))
        ).date().isoformat()
        text = radar_message(config, target)
    if not text:
        raise RuntimeError("Radar summary is empty")

    digest = content_hash(text)
    if args.auto and digest == previous_hash():
        print("SKIP: radar content unchanged")
        return

    print(f"DESTINATION:\n{config['base_url']}/r/{room}\n\nMESSAGE:\n{text}\n", flush=True)
    if not args.auto and input("Type PUBLISH to continue: ").strip() != "PUBLISH":
        print("Not published.")
        return

    env_values = load_env(ROOT / config.get("env_file", ".env"))
    seed = env_values.get("SIGN_SEED")
    if not seed:
        raise RuntimeError("SIGN_SEED is missing from .env")

    # These exact values are used both for signing and for the POST payload.
    nonce = str(time.time_ns())
    signer = ROOT / config.get("sign_script", "sign.py")
    sign_env = {**os.environ, "SIGN_SEED": seed}
    signed = subprocess.run(
        [sys.executable, str(signer), "say", room, nonce, text],
        text=True, capture_output=True, timeout=30, check=False, cwd=ROOT, env=sign_env,
    )
    if signed.returncode:
        safe_stderr = signed.stderr.replace(seed, "***REDACTED***")
        raise RuntimeError(
            f"sign.py failed with exit code {signed.returncode}\n"
            f"stderr:\n{safe_stderr}"
        )
    lines = [line.strip() for line in signed.stdout.splitlines() if line.strip()]
    if len(lines) != 2 or not lines[0].startswith("did:key:"):
        raise RuntimeError("sign.py returned an unexpected response")
    did, signature = lines

    body = json.dumps(
        {"did": did, "sig": signature, "nonce": nonce, "text": text}
    ).encode("utf-8")
    url = config["base_url"] + f"/r/{urllib.parse.quote(room, safe='')}?format=json"
    request = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "technocore-tech-radar/1"},
        method="POST",
    )
    with urllib.request.urlopen(
        request, timeout=float(config.get("http_timeout_seconds", 20))
    ) as response:
        response.read(1_000_000)

    save_successful_hash(digest)
    print("Published.")


if __name__ == "__main__":
    main()
