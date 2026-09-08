#!/usr/bin/env python3
"""LLM backends for TechnoScout v0.6."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


class LLMBackend:
    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        timeout_seconds: float,
    ) -> str:
        raise NotImplementedError

    def models(self) -> list[str]:
        return []

    def describe(self) -> str:
        return self.__class__.__name__

    def close(self) -> None:
        return None


class HttpLLMBackend(LLMBackend):
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        timeout_seconds: float,
    ) -> str:
        payload = {
            "model": model,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "messages": messages,
        }
        request = urllib.request.Request(
            self.cfg["llm_base_url"] + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=float(timeout_seconds)) as response:
            raw = json.loads(
                response.read(int(self.cfg["max_response_bytes"])).decode("utf-8")
            )
        return str(raw["choices"][0]["message"]["content"])

    def models(self) -> list[str]:
        request = urllib.request.Request(
            self.cfg["llm_base_url"] + "/models",
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(
            request, timeout=float(self.cfg["llm_timeout_seconds"])
        ) as response:
            payload = json.loads(
                response.read(int(self.cfg["max_response_bytes"])).decode("utf-8")
            )
        items = payload.get("data", []) if isinstance(payload, dict) else []
        return [
            str(item["id"])
            for item in items
            if isinstance(item, dict) and item.get("id")
        ]

    def describe(self) -> str:
        return f"http:{self.cfg['llm_base_url']}"


class ManagedMLXBackend(LLMBackend):
    """One persistent MLX process.

    If a request exceeds its deadline, the worker process group is terminated
    before TimeoutError is raised. Therefore timed-out generation cannot remain
    queued or continue consuming GPU/CPU behind the next TechnoScout request.
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.proc: subprocess.Popen[str] | None = None
        self.loaded_model = ""
        self.request_id = 0
        self.requests_since_start = 0
        self.restart_count = 0
        self.worker_python = self._resolve_worker_python()
        worker_script = str(cfg.get("mlx_worker_script", "")).strip()
        self.worker_script = (
            Path(worker_script).expanduser()
            if worker_script
            else Path(__file__).with_name("mlx_worker.py")
        )
        self.log_path = self._resolve_path(
            str(cfg.get("mlx_worker_log", "logs/mlx-worker.log"))
        )
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = self.log_path.open("a", encoding="utf-8", buffering=1)

    def _resolve_path(self, value: str) -> Path:
        p = Path(value).expanduser()
        if p.is_absolute():
            return p
        return Path.cwd() / p

    def _resolve_worker_python(self) -> str:
        explicit = str(self.cfg.get("mlx_worker_python", "")).strip()
        if explicit:
            return str(Path(explicit).expanduser())

        uv_tool_python = (
            Path.home()
            / ".local"
            / "share"
            / "uv"
            / "tools"
            / "mlx-lm"
            / "bin"
            / "python"
        )
        if uv_tool_python.exists():
            return str(uv_tool_python)

        return sys.executable

    def _read_line(self, timeout_seconds: float) -> str:
        if self.proc is None or self.proc.stdout is None:
            raise RuntimeError("MLX worker is not running")

        selector = selectors.DefaultSelector()
        try:
            selector.register(self.proc.stdout, selectors.EVENT_READ)
            events = selector.select(max(0.0, float(timeout_seconds)))
            if not events:
                raise TimeoutError(
                    f"managed MLX worker timed out after {float(timeout_seconds):.1f}s"
                )
            line = self.proc.stdout.readline()
        finally:
            selector.close()

        if line == "":
            code = self.proc.poll()
            raise RuntimeError(f"managed MLX worker exited unexpectedly code={code}")
        return line.rstrip("\n")

    @staticmethod
    def _close_process_pipes(proc: subprocess.Popen[str]) -> None:
        for stream in (proc.stdin, proc.stdout):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:
                pass

    def _terminate(self, reason: str) -> None:
        proc = self.proc
        self.proc = None
        self.loaded_model = ""
        self.requests_since_start = 0
        if proc is None:
            return

        pid = proc.pid
        print(
            f"[llm-worker] stop pid={pid} reason={reason}",
            file=sys.stderr,
            flush=True,
        )
        try:
            try:
                pgid = os.getpgid(pid)
            except ProcessLookupError:
                pgid = None

            grace = max(
                0.1,
                float(self.cfg.get("mlx_worker_kill_grace_seconds", 2)),
            )
            try:
                if pgid is not None:
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    proc.terminate()
                proc.wait(timeout=grace)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                try:
                    if pgid is not None:
                        os.killpg(pgid, signal.SIGKILL)
                    else:
                        proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            self._close_process_pipes(proc)

    def _start(self, model: str) -> None:
        if (
            self.proc is not None
            and self.proc.poll() is None
            and self.loaded_model == model
        ):
            return

        if self.proc is not None:
            self._terminate("model-switch-or-restart")

        command = [
            self.worker_python,
            "-u",
            str(self.worker_script),
            "--model",
            model,
        ]
        print(
            f"[llm-worker] start model={model} python={self.worker_python}",
            flush=True,
        )
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log_handle,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self.loaded_model = model
        self.requests_since_start = 0
        self.restart_count += 1

        timeout = float(self.cfg.get("mlx_worker_start_timeout_seconds", 180))
        try:
            response = json.loads(self._read_line(timeout))
        except Exception:
            self._terminate("startup-failure")
            raise

        if response.get("type") == "fatal":
            error = str(response.get("error", "worker startup failed"))
            self._terminate("startup-fatal")
            raise RuntimeError(error)
        if response.get("type") != "ready":
            self._terminate("startup-protocol-error")
            raise RuntimeError(f"unexpected MLX worker startup response: {response}")

        print(
            f"[llm-worker] ready pid={self.proc.pid if self.proc else '?'} "
            f"model={model} log={self.log_path}",
            flush=True,
        )

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        timeout_seconds: float,
    ) -> str:
        self._start(model)
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("MLX worker failed to start")

        self.request_id += 1
        request_id = self.request_id
        request = {
            "op": "chat",
            "id": request_id,
            "messages": messages,
            "max_tokens": int(max_tokens),
            # Direct worker intentionally uses deterministic generation.
            # Keep the field for diagnostics / forward compatibility.
            "temperature": float(temperature),
        }
        self.proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

        deadline = float(timeout_seconds)
        if self.requests_since_start == 0:
            deadline += float(
                self.cfg.get("mlx_worker_first_request_extra_seconds", 60)
            )
        self.requests_since_start += 1

        try:
            response = json.loads(self._read_line(deadline))
        except TimeoutError:
            pid = self.proc.pid if self.proc is not None else "?"
            self._terminate(f"request-timeout id={request_id}")
            raise TimeoutError(
                f"MLX request {request_id} exceeded {deadline:.1f}s; "
                f"worker pid={pid} was killed"
            ) from None
        except Exception:
            self._terminate(f"request-protocol-failure id={request_id}")
            raise

        if response.get("type") != "result" or response.get("id") != request_id:
            self._terminate(f"response-mismatch id={request_id}")
            raise RuntimeError(
                f"MLX worker response mismatch request={request_id}: {response}"
            )
        if response.get("ok") is not True:
            raise RuntimeError(
                f"MLX worker generation failed: {response.get('error', 'unknown error')}"
            )
        return str(response.get("content", ""))

    def models(self) -> list[str]:
        values = [
            str(self.cfg.get("triage_model", "")).strip(),
            str(self.cfg.get("research_model", "")).strip(),
            str(self.cfg.get("mlx_worker_model", "")).strip(),
        ]
        result: list[str] = []
        for value in values:
            if value and value not in result:
                result.append(value)
        return result

    def describe(self) -> str:
        state = "running" if self.proc is not None and self.proc.poll() is None else "stopped"
        return (
            f"managed_mlx state={state} model={self.loaded_model or '-'} "
            f"restarts={self.restart_count}"
        )

    def close(self) -> None:
        self._terminate("shutdown")
        try:
            self.log_handle.close()
        except Exception:
            pass


def create_llm_backend(cfg: dict[str, Any]) -> LLMBackend:
    backend = str(cfg.get("llm_backend", "managed_mlx")).strip().lower()
    if backend in {"managed_mlx", "mlx", "direct_mlx"}:
        return ManagedMLXBackend(cfg)
    if backend in {"http", "openai_http"}:
        return HttpLLMBackend(cfg)
    raise ValueError(f"unsupported llm_backend: {backend}")
