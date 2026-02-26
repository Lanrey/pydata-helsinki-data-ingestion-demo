from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_COPILOT_CHAT_URL = "https://api.githubcopilot.com/chat/completions"
DEFAULT_MODEL = "gpt-5.3-codex"


@dataclass(slots=True)
class CompletionOptions:
    model: str
    temperature: float | None
    max_tokens: int | None
    reasoning_effort: str | None
    thinking_budget: int | None
    timeout_seconds: int
    dry_run: bool
    stream: bool = False


@dataclass(slots=True)
class CompletionResult:
    text: str
    usage: dict[str, Any] | None
    model: str | None
    request_payload: dict[str, Any] | None


def _chat_url() -> str:
    return os.environ.get("AGENT_COPILOT_CHAT_URL", DEFAULT_COPILOT_CHAT_URL)


def _extract_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts: list[str] = []
                    for item in content:
                        if isinstance(item, dict):
                            text = item.get("text")
                            if isinstance(text, str):
                                parts.append(text)
                    if parts:
                        return "\n".join(parts)
    raise ValueError("Completion response did not include text content")


def complete_chat(
    *,
    token: str,
    user_prompt: str,
    system_prompt: str | None,
    options: CompletionOptions,
    stream_handler: Callable[[str], None] | None = None,
) -> CompletionResult:
    model = options.model or DEFAULT_MODEL
    prompt_parts: list[str] = []
    if system_prompt:
        prompt_parts.append(f"System instructions:\n{system_prompt.strip()}")
    prompt_parts.append(user_prompt.strip())
    prompt = "\n\n".join(part for part in prompt_parts if part)

    command = [
        "copilot",
        "-p",
        prompt,
        "--model",
        model,
        "--allow-all-tools",
        "--stream",
        ("on" if options.stream else "off"),
        "--silent",
        "--no-color",
    ]

    if options.dry_run:
        return CompletionResult(
            text="",
            usage=None,
            model=model,
            request_payload={
                "mode": "copilot-cli",
                "command": command,
                "prompt": prompt,
            },
        )

    try:
        if not options.stream:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=options.timeout_seconds,
                env=os.environ.copy(),
            )
            stdout_text = (result.stdout or "").strip()
            stderr_text = (result.stderr or "").strip()
            returncode = int(result.returncode)
        else:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=os.environ.copy(),
            )
            if process.stdout is None:
                process.kill()
                raise RuntimeError("Copilot CLI streaming failed to initialize stdout")

            line_queue: queue.Queue[str | None] = queue.Queue()

            def _reader() -> None:
                assert process.stdout is not None
                for line in process.stdout:
                    line_queue.put(line)
                line_queue.put(None)

            reader_thread = threading.Thread(target=_reader, daemon=True)
            reader_thread.start()

            collected: list[str] = []
            deadline = time.monotonic() + max(1, int(options.timeout_seconds))
            stream_done = False
            while not stream_done:
                now = time.monotonic()
                if now >= deadline:
                    process.kill()
                    raise RuntimeError(f"Copilot CLI timed out after {options.timeout_seconds}s")
                try:
                    item = line_queue.get(timeout=min(0.2, deadline - now))
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue

                if item is None:
                    stream_done = True
                    continue

                collected.append(item)
                if stream_handler:
                    chunk = item.rstrip("\n")
                    if chunk:
                        stream_handler(chunk)

            returncode = int(process.wait(timeout=1))
            stderr_text = ""
            if process.stderr is not None:
                stderr_text = (process.stderr.read() or "").strip()
            stdout_text = "".join(collected).strip()
    except FileNotFoundError as error:
        raise RuntimeError(
            "Copilot CLI not found. Install it and run `copilot login`, then retry."
        ) from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"Copilot CLI timed out after {options.timeout_seconds}s") from error
    except Exception as error:
        raise RuntimeError(f"Copilot CLI execution failed: {error}") from error

    if returncode != 0:
        details = stderr_text or stdout_text or f"exit code {returncode}"
        raise RuntimeError(f"Copilot CLI request failed: {details}")

    return CompletionResult(
        text=stdout_text,
        usage=None,
        model=model,
        request_payload={"mode": "copilot-cli", "command": command},
    )
