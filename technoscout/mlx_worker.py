#!/usr/bin/env python3
"""Persistent direct-MLX inference worker for TechnoScout v0.4.

Protocol: one JSON object per stdin line, one JSON object per stdout line.
stdout is reserved for the protocol. Library/model output is redirected to stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
import traceback
from typing import Any


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    args = parser.parse_args()

    try:
        with contextlib.redirect_stdout(sys.stderr):
            from mlx_lm import generate, load
            model, tokenizer = load(args.model)
        emit({"type": "ready", "model": args.model})
    except Exception as exc:
        emit({
            "type": "fatal",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=8),
        })
        return

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            request = json.loads(raw_line)
            if request.get("op") == "shutdown":
                emit({"type": "bye"})
                return
            if request.get("op") != "chat":
                raise ValueError("unsupported worker operation")

            request_id = int(request["id"])
            messages = request.get("messages", [])
            max_tokens = max(1, int(request.get("max_tokens", 256)))
            started = time.monotonic()

            with contextlib.redirect_stdout(sys.stderr):
                prompt = tokenizer.apply_chat_template(
                    messages,
                    tokenize=False,
                    add_generation_prompt=True,
                )
                content = generate(
                    model,
                    tokenizer,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    verbose=False,
                )

            emit({
                "type": "result",
                "id": request_id,
                "ok": True,
                "content": str(content),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            })
        except Exception as exc:
            emit({
                "type": "result",
                "id": request.get("id") if isinstance(request, dict) else None,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=6),
            })


if __name__ == "__main__":
    main()
