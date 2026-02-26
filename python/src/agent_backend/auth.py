from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


AUTH_ROOT = Path.home() / ".agent-runtime" / "auth"
COPILOT_AUTH_FILE = AUTH_ROOT / "github-copilot.json"


@dataclass(slots=True)
class CopilotAuthRecord:
    provider: str
    token: str
    source: str
    login: str | None
    validatedAt: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "token": self.token,
            "source": self.source,
            "login": self.login,
            "validatedAt": self.validatedAt,
        }


def load_copilot_auth() -> CopilotAuthRecord | None:
    if not COPILOT_AUTH_FILE.exists():
        return None
    try:
        payload = json.loads(COPILOT_AUTH_FILE.read_text(encoding="utf-8"))
        return CopilotAuthRecord(
            provider=str(payload.get("provider") or "github-copilot"),
            token=str(payload["token"]),
            source=str(payload.get("source") or "unknown"),
            login=(str(payload["login"]) if payload.get("login") is not None else None),
            validatedAt=int(payload.get("validatedAt") or 0),
        )
    except Exception:
        return None


def save_copilot_auth(record: CopilotAuthRecord) -> None:
    AUTH_ROOT.mkdir(parents=True, exist_ok=True)
    COPILOT_AUTH_FILE.write_text(json.dumps(record.to_dict(), indent=2) + "\n", encoding="utf-8")


def clear_copilot_auth() -> bool:
    if not COPILOT_AUTH_FILE.exists():
        return False
    COPILOT_AUTH_FILE.unlink()
    return True


def token_from_gh_cli() -> str | None:
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        token = result.stdout.strip()
        return token or None
    except Exception:
        return None


def resolve_login_token(cli_token: str | None, from_gh: bool) -> tuple[str | None, str | None]:
    if cli_token:
        return (cli_token.strip(), "--token")

    env_token = os.environ.get("COPILOT_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if env_token:
        return (env_token.strip(), "environment")

    if from_gh:
        gh_token = token_from_gh_cli()
        if gh_token:
            return (gh_token, "gh auth token")

    return (None, None)


def validate_github_token(token: str) -> tuple[bool, str | None, str | None]:
    request = Request(
        url="https://api.github.com/user",
        method="GET",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-backend-cli",
        },
    )

    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        login = payload.get("login") if isinstance(payload, dict) else None
        return (True, str(login) if login else None, None)
    except HTTPError as error:
        if error.code in {401, 403}:
            return (False, None, f"GitHub token rejected (HTTP {error.code}).")
        return (False, None, f"GitHub API error (HTTP {error.code}).")
    except URLError as error:
        reason = getattr(error, "reason", error)
        return (False, None, f"GitHub API unreachable: {reason}")
    except Exception as error:
        return (False, None, f"GitHub token validation failed: {error}")


def build_auth_record(token: str, source: str, login: str | None) -> CopilotAuthRecord:
    return CopilotAuthRecord(
        provider="github-copilot",
        token=token,
        source=source,
        login=login,
        validatedAt=int(time.time() * 1000),
    )
