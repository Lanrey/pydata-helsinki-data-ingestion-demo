from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .auth import (
    build_auth_record,
    clear_copilot_auth,
    load_copilot_auth,
    resolve_login_token,
    save_copilot_auth,
    validate_github_token,
)
from .llm import CompletionOptions, complete_chat
from .models import SourceConfig
from .storage import (
    create_source,
    delete_source,
    get_source_path,
    load_source_config,
    load_workspace_sources,
    mark_source_authenticated,
    save_source_config,
)


API_AUTH_TYPES: tuple[str, ...] = ("bearer", "basic", "header", "query", "none")


def _parse_api_auth_types_arg(raw_value: str | None) -> list[str] | None:
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if not text:
        return None
    tokens = [item.strip().lower() for item in text.split(",") if item.strip()]
    if not tokens:
        return None
    allowed = set(API_AUTH_TYPES)
    invalid = [item for item in tokens if item not in allowed]
    if invalid:
        raise ValueError(
            "Invalid value for --auth-types-try. "
            f"Allowed values: {', '.join(API_AUTH_TYPES)}"
        )
    deduped: list[str] = []
    seen: set[str] = set()
    for item in tokens:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _looks_like_auth_error(error_text: str) -> bool:
    lowered = str(error_text or "").lower()
    return any(token in lowered for token in ["401", "403", "unauthorized", "forbidden", "authentication", "authorization"])


def _http_error_is_auth_failure(code: int, body_text: str) -> bool:
    if code in {401, 403}:
        return True
    lowered = str(body_text or "").lower()
    if code == 400 and any(
        token in lowered
        for token in [
            "remove the bearer prefix",
            "authorization",
            "unauthorized",
            "forbidden",
            "authentication",
            "api key as a bearer",
        ]
    ):
        return True
    return False


def _workspace_path(raw_path: str) -> Path:
    return Path(raw_path).expanduser().resolve()


def _print_json(payload: dict[str, Any], *, stream=None) -> None:
    print(json.dumps(payload, indent=2), file=stream or sys.stdout)


def _emit_runtime_event(*, enabled: bool, event: str, message: str, data: dict[str, Any] | None = None) -> None:
    if not enabled:
        return
    payload: dict[str, Any] = {"event": event, "message": message}
    if isinstance(data, dict) and data:
        payload["data"] = data
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


def _append_reasoning(trace: list[str], message: str, *, stream: bool) -> None:
    trace.append(message)
    _emit_runtime_event(enabled=stream, event="reasoning", message=message)


def _append_step(steps: list[str], message: str, *, stream: bool) -> None:
    steps.append(message)
    _emit_runtime_event(enabled=stream, event="step", message=message)


def _error_fix_suggestions(*, error_text: str, source_type: str, provider: str, intent: str) -> list[str]:
    lowered = error_text.lower()
    suggestions: list[str] = []
    if provider == "linear" and "remove the bearer prefix" in lowered:
        suggestions.append("Linear API keys should be sent as the Authorization header value without Bearer prefix. Retry after updating credential/header format.")
    if any(token in lowered for token in ["401", "403", "unauthorized", "forbidden", "authentication"]):
        supported = ", ".join(API_AUTH_TYPES)
        suggestions.append(
            f"Supported API auth types: {supported}. 'none' is valid when the endpoint does not require authentication."
        )
        suggestions.append(
            "Set or refresh credentials, then retry. The API runner now tries auth types automatically (bearer/basic/header/query/none) after auth failures. Example: agentctl credential set --workspace ~/.agent-runtime/workspaces/default --source <slug> --mark-authenticated"
        )
    if any(token in lowered for token in ["timed out", "timeout", "tempor", "connection reset", "unreachable", "429", "502", "503", "504"]):
        suggestions.append("Increase timeout or retry with more heal attempts. Example: --timeout 120 --heal-attempts 4")
    if "toolname" in lowered or "mcp tool" in lowered or "tools/call" in lowered:
        suggestions.append("Specify the MCP tool explicitly in your request or pass a source with known tool names.")
    if "could not determine api path" in lowered or "missing" in lowered:
        suggestions.append("Include an explicit endpoint path in your request, for example: list tickets from /api/v2/tickets")
    if not suggestions and source_type == "api" and provider == "linear" and intent == "list_issues":
        suggestions.append("Ensure Linear source is connected and authenticated, then rerun: agentctl chat --cli \"list all issues in linear\"")
    if not suggestions:
        suggestions.append("Review the error details in agentReasoning and rerun with --heal-attempts increased.")
    return suggestions


def _interactive_fix_enabled(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "interactive_fix", True)) and sys.stdin.isatty()


def _effective_fix_mode(args: argparse.Namespace) -> str:
    mode = str(getattr(args, "fix_mode", "") or "").strip().lower()
    if mode in {"suggest", "guarded", "auto"}:
        return mode
    if bool(getattr(args, "guarded_auto_apply", False)):
        return "guarded"
    return "suggest"


def _fix_scope_allows(args: argparse.Namespace, target_scope: str) -> bool:
    scope = str(getattr(args, "fix_scope", "runtime") or "runtime").strip().lower()
    if scope == "all":
        return True
    return scope == target_scope


def _select_interactive_option(title: str, options: list[str]) -> int | None:
    if not options:
        return None
    print("", file=sys.stderr)
    print(title, file=sys.stderr)
    for idx, option in enumerate(options, start=1):
        print(f"  {idx}. {option}", file=sys.stderr)
    raw = input("Choose an option (or press Enter to skip): ").strip()
    if not raw:
        return None
    if not raw.isdigit():
        return None
    selected = int(raw)
    if selected < 1 or selected > len(options):
        return None
    return selected - 1


def _retry_cmd_act_with_overrides(args: argparse.Namespace, **overrides: Any) -> int:
    merged = vars(args).copy()
    merged.update(overrides)
    merged["interactive_fix"] = False
    retry_args = argparse.Namespace(**merged)
    return cmd_act(retry_args)


def _mask_secret_for_diff(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-4:]}"


def _linear_bearer_fix_preview(workspace_root: Path, source_slug: str) -> tuple[bool, str | None, list[str], str]:
    path = _credential_file(workspace_root, source_slug)
    if not path.exists():
        return False, None, [], "Credential file not found for this source."
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False, None, [], "Credential file is invalid JSON."
    current = payload.get("value")
    if not isinstance(current, str) or not current.strip():
        return False, None, [], "Credential value is missing or empty."
    stripped = re.sub(r"^bearer\s+", "", current.strip(), flags=re.IGNORECASE)
    if stripped == current.strip():
        return False, None, [], "Credential already has no Bearer prefix."
    before = _mask_secret_for_diff(current)
    after = _mask_secret_for_diff(stripped)
    diff_lines = [
        f"--- {path}",
        f"+++ {path}",
        f'-  "value": "{before}"',
        f'+  "value": "{after}"',
    ]
    return True, stripped, diff_lines, ""


def _apply_linear_bearer_fix(workspace_root: Path, source_slug: str, new_value: str) -> None:
    path = _credential_file(workspace_root, source_slug)
    payload: dict[str, Any]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    now = int(time.time() * 1000)
    expires_at = payload.get("expiresAt") if isinstance(payload.get("expiresAt"), int) else now + (24 * 3600 * 1000)
    created_at = payload.get("createdAt") if isinstance(payload.get("createdAt"), int) else now
    payload["value"] = new_value
    payload["createdAt"] = created_at
    payload["expiresAt"] = expires_at
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _timeout_code_patch_preview() -> tuple[bool, str | None, list[str], str]:
    target = Path(__file__).resolve()
    try:
        original = target.read_text(encoding="utf-8")
    except Exception as error:
        return False, None, [], f"Unable to read CLI source file: {error}"

    patterns = [
        (r'(act_parser\.add_argument\("--timeout",\s*type=int,\s*default=)60(,\s*help="Planner timeout in seconds"\))', r"\g<1>90\g<2>"),
        (r'(chat_parser\.add_argument\("--timeout",\s*type=int,\s*default=)60(,\s*help="Planner/discovery timeout in seconds"\))', r"\g<1>90\g<2>"),
    ]

    updated = original
    changed = False
    for pattern, repl in patterns:
        next_text, count = re.subn(pattern, repl, updated)
        if count > 0:
            changed = True
            updated = next_text

    if not changed:
        return False, None, [], "Timeout defaults are already patched or expected patterns were not found."

    diff_lines = [
        f"--- {target}",
        f"+++ {target}",
        '- act_parser.add_argument("--timeout", type=int, default=60, help="Planner timeout in seconds")',
        '+ act_parser.add_argument("--timeout", type=int, default=90, help="Planner timeout in seconds")',
        '- chat_parser.add_argument("--timeout", type=int, default=60, help="Planner/discovery timeout in seconds")',
        '+ chat_parser.add_argument("--timeout", type=int, default=90, help="Planner/discovery timeout in seconds")',
    ]
    return True, updated, diff_lines, ""


def _apply_timeout_code_patch(updated_text: str) -> None:
    target = Path(__file__).resolve()
    target.write_text(updated_text, encoding="utf-8")


def _maybe_guarded_auto_apply(
    args: argparse.Namespace,
    *,
    workspace_root: Path,
    source_slug: str,
    provider: str,
    error_text: str,
    suggestions: list[str],
    stream_enabled: bool,
) -> bool:
    mode = _effective_fix_mode(args)
    if mode == "suggest":
        return False

    if mode == "guarded" and not sys.stdin.isatty():
        _emit_runtime_event(
            enabled=stream_enabled,
            event="fix",
            message="Guarded auto-apply requested, but confirmation requires TTY; skipping.",
        )
        return False

    suggestion_text = "\n".join(suggestions)
    lowered_error = error_text.lower()
    trigger_linear_bearer = _fix_scope_allows(args, "config") and (
        provider.lower() == "linear"
        and (
            "remove the bearer prefix" in lowered_error
            or "without bearer prefix" in suggestion_text.lower()
        )
    )
    trigger_timeout_code_patch = (
        _fix_scope_allows(args, "code")
        and bool(getattr(args, "allow_code_patch", False))
        and any(token in lowered_error for token in ["timed out", "timeout", "mcp stdio timeout/error"])
    )

    if not trigger_linear_bearer and not trigger_timeout_code_patch:
        if _fix_scope_allows(args, "code") and bool(getattr(args, "allow_code_patch", False)):
            _emit_runtime_event(
                enabled=stream_enabled,
                event="fix",
                message="Code-patch auto-fix is enabled, but no supported code patch handler matched this failure.",
            )
        _emit_runtime_event(
            enabled=stream_enabled,
            event="fix",
            message="Auto-fix found no supported safe patch for this failure.",
        )
        return False

    applicable = False
    diff_lines: list[str] = []
    reason = ""
    apply_kind = ""
    stripped: str | None = None
    updated_text: str | None = None

    if trigger_linear_bearer:
        applicable, stripped, diff_lines, reason = _linear_bearer_fix_preview(workspace_root, source_slug)
        apply_kind = "linear_credential"
    elif trigger_timeout_code_patch:
        applicable, updated_text, diff_lines, reason = _timeout_code_patch_preview()
        apply_kind = "timeout_code_patch"

    if not applicable:
        _emit_runtime_event(
            enabled=stream_enabled,
            event="fix",
            message=f"Auto-fix skipped: {reason or 'No applicable patch.'}",
        )
        return False

    print("", file=sys.stderr)
    print("Proposed auto-fix patch:", file=sys.stderr)
    for line in diff_lines:
        print(line, file=sys.stderr)

    if bool(getattr(args, "fix_dry_run", False)):
        _emit_runtime_event(
            enabled=stream_enabled,
            event="step",
            message="Fix dry-run enabled; patch preview only (no changes applied).",
        )
        return False

    if mode == "guarded" and not _prompt_yes_no("Apply this patch and retry now?", default_yes=True):
        _emit_runtime_event(enabled=stream_enabled, event="step", message="Guarded auto-apply declined by user.")
        return False

    try:
        if apply_kind == "linear_credential" and stripped is not None:
            _apply_linear_bearer_fix(workspace_root, source_slug, stripped)
        elif apply_kind == "timeout_code_patch" and updated_text is not None:
            _apply_timeout_code_patch(updated_text)
        else:
            _emit_runtime_event(enabled=stream_enabled, event="error", message="Auto-fix apply failed: invalid patch payload.")
            return False
        _emit_runtime_event(
            enabled=stream_enabled,
            event="self_heal",
            message=(
                "Applied auto-fix: updated CLI timeout defaults in code."
                if apply_kind == "timeout_code_patch" and mode == "auto"
                else "Applied guarded auto-fix: updated CLI timeout defaults in code."
                if apply_kind == "timeout_code_patch"
                else "Applied auto-fix: stripped Bearer prefix from Linear credential."
                if mode == "auto"
                else "Applied guarded auto-fix: stripped Bearer prefix from Linear credential."
            ),
        )
        return True
    except Exception as error:
        _emit_runtime_event(
            enabled=stream_enabled,
            event="error",
            message=f"Guarded auto-apply failed: {error}",
        )
        return False


def _parse_json_object(raw_value: str | None, *, field_name: str) -> dict[str, Any] | None:
    if raw_value is None:
        return None
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{field_name} must be valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return parsed


def _copilot_token() -> str | None:
    auth = load_copilot_auth()
    if auth and auth.token:
        return auth.token
    env_token = os.environ.get("COPILOT_TOKEN") or os.environ.get("GITHUB_TOKEN")
    return env_token.strip() if env_token else None


def _source_requires_auth(source: SourceConfig) -> bool:
    if source.type == "api":
        auth_type = ""
        if isinstance(source.api, dict):
            auth_type = str(source.api.get("authType") or "none").lower()
        return auth_type not in {"", "none"}
    if source.type == "mcp":
        auth_type = ""
        if isinstance(source.mcp, dict):
            auth_type = str(source.mcp.get("authType") or "none").lower()
        return auth_type not in {"", "none"}
    return False


def _provider_auth_docs(provider: str) -> list[dict[str, str]]:
    normalized = provider.strip().lower()
    docs: dict[str, list[dict[str, str]]] = {
        "linear": [
            {"title": "Linear API keys", "url": "https://linear.app/docs/graphql/working-with-the-graphql-api#personal-api-keys"},
        ],
        "github": [
            {"title": "GitHub personal access tokens", "url": "https://docs.github.com/authentication/keeping-your-account-and-data-secure/creating-a-personal-access-token"},
        ],
        "zendesk": [
            {"title": "Zendesk API authentication", "url": "https://developer.zendesk.com/documentation/api-basics/authentication/"},
        ],
        "notion": [
            {"title": "Notion auth and integrations", "url": "https://developers.notion.com/docs/authorization"},
        ],
        "slack": [
            {"title": "Slack OAuth", "url": "https://api.slack.com/authentication/oauth-v2"},
        ],
        "microsoft": [
            {"title": "Microsoft identity platform OAuth", "url": "https://learn.microsoft.com/entra/identity-platform/v2-oauth2-auth-code-flow"},
        ],
        "google": [
            {"title": "Google OAuth 2.0", "url": "https://developers.google.com/identity/protocols/oauth2"},
        ],
    }
    return docs.get(normalized, [])


def _normalize_doc_links(value: Any) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    if not isinstance(value, list):
        return links
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        links.append({"title": title or url, "url": url})
    return links


def _print_documentation_links(docs: list[dict[str, str]], *, heading: str = "Documentation") -> None:
    if not docs:
        return
    print(f"- {heading}:", file=sys.stderr)
    for item in docs:
        if isinstance(item, dict):
            print(f"  - {item.get('title')}: {item.get('url')}", file=sys.stderr)


def _probe_auth_documentation(
    *,
    provider: str,
    source_type: str,
    auth_type: str,
    model: str | None,
    timeout: int,
) -> tuple[list[dict[str, str]], str | None]:
    token = _copilot_token() or ""
    prompt = (
        "Return strict JSON only with shape: "
        '{"reasoning": string, "docs": [{"title": string, "url": string}]}.\n'
        f"Provider: {provider}\n"
        f"SourceType: {source_type}\n"
        f"AuthType: {auth_type}\n"
        "Find likely official authentication docs links for this provider and auth type. "
        "Prefer official provider documentation pages."
    )
    try:
        result = complete_chat(
            token=token,
            user_prompt=prompt,
            system_prompt="You return concise JSON containing best authentication documentation links.",
            options=CompletionOptions(
                model=(model or os.environ.get("AGENT_COPILOT_MODEL") or "gpt-5.3-codex"),
                temperature=None,
                max_tokens=None,
                reasoning_effort=None,
                thinking_budget=None,
                timeout_seconds=max(10, int(timeout)),
                dry_run=False,
            ),
        )
        parsed = _extract_json_object(result.text)
        if isinstance(parsed, dict):
            docs = _normalize_doc_links(parsed.get("docs"))
            reasoning = str(parsed.get("reasoning") or "").strip() or None
            if docs:
                return docs, reasoning
    except Exception:
        pass
    fallback = [
        {
            "title": f"{provider} authentication docs",
            "url": f"https://www.google.com/search?q={provider}+{auth_type}+authentication+official+docs",
        }
    ]
    return fallback, "I could not validate provider-specific docs automatically, so I generated a targeted discovery link."


def _source_auth_guide(source: SourceConfig) -> dict[str, Any]:
    provider = str(source.provider or "custom")
    source_type = str(source.type)
    auth_type = "none"
    if source_type == "api" and isinstance(source.api, dict):
        auth_type = str(source.api.get("authType") or "none").lower()
    if source_type == "mcp" and isinstance(source.mcp, dict):
        auth_type = str(source.mcp.get("authType") or "none").lower()

    steps: list[str] = []
    if auth_type in {"none", ""}:
        steps = [
            "This source does not require credentials.",
            "Run an action to verify connectivity.",
        ]
    elif auth_type in {"bearer", "oauth"}:
        steps = [
            "Open provider settings and create/find an API token.",
            "Run credential set and paste token when prompted.",
            "Confirm source as authenticated.",
        ]
    elif auth_type == "basic":
        steps = [
            "Get a username and password (or API user/password pair) from provider settings.",
            "Run credential set and enter username/password when prompted.",
            "Confirm source as authenticated.",
        ]
    elif auth_type in {"header", "query"}:
        steps = [
            "Get the required API key from provider settings.",
            "Run credential set and paste the key when prompted.",
            "Confirm source as authenticated.",
        ]
    else:
        steps = [
            "Review provider docs for authentication method.",
            "Run credential set and provide credential when prompted.",
            "Confirm source as authenticated.",
        ]

    return {
        "source": source.slug,
        "provider": provider,
        "sourceType": source_type,
        "authType": auth_type,
        "steps": steps,
        "docs": _provider_auth_docs(provider),
    }


def _print_auth_guide_for_user(guide: dict[str, Any]) -> None:
    print("Authentication guide", file=sys.stderr)
    print(f"- Source: {guide.get('source')}", file=sys.stderr)
    print(f"- Provider: {guide.get('provider')}", file=sys.stderr)
    print(f"- Auth type: {guide.get('authType')}", file=sys.stderr)
    for idx, step in enumerate(guide.get("steps") or [], start=1):
        print(f"  {idx}. {step}", file=sys.stderr)
    docs = _normalize_doc_links(guide.get("docs"))
    _print_documentation_links(docs)


def _prompt_yes_no(message: str, *, default_yes: bool = True) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    raw = input(f"{message} {suffix} ").strip().lower()
    if not raw:
        return default_yes
    return raw in {"y", "yes"}


def _prompt_credential_for_source(source: SourceConfig) -> str | None:
    auth_type = "none"
    if source.type == "api" and isinstance(source.api, dict):
        auth_type = str(source.api.get("authType") or "none").lower()
    if source.type == "mcp" and isinstance(source.mcp, dict):
        auth_type = str(source.mcp.get("authType") or "none").lower()

    if auth_type in {"none", ""}:
        return ""
    if auth_type == "basic":
        username = input("Enter username: ").strip()
        password = getpass.getpass("Enter password: ").strip()
        if not username or not password:
            return None
        return f"{username}:{password}"
    prompt = "Enter API token/key (input hidden): "
    token = getpass.getpass(prompt).strip()
    return token or None


def cmd_list(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    sources = load_workspace_sources(workspace)
    _print_json({"workspace": str(workspace), "count": len(sources), "sources": [item.to_dict() for item in sources]})
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    source = load_source_config(workspace, args.slug)
    if source is None:
        _print_json({"error": f"Source '{args.slug}' not found."}, stream=sys.stderr)
        return 1
    _print_json(source.to_dict())
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    try:
        mcp = _parse_json_object(args.mcp, field_name="--mcp")
        api = _parse_json_object(args.api, field_name="--api")
        local = _parse_json_object(args.local, field_name="--local")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    source_type = str(args.type)
    if source_type == "mcp" and not mcp:
        print("MCP source requires --mcp JSON", file=sys.stderr)
        return 2
    if source_type == "api" and not api:
        print("API source requires --api JSON", file=sys.stderr)
        return 2
    if source_type == "local" and not local:
        print("Local source requires --local JSON", file=sys.stderr)
        return 2

    config = create_source(
        workspace_root=workspace,
        name=args.name,
        source_type=source_type,
        provider=args.provider,
        enabled=not bool(args.disabled),
        mcp=mcp,
        api=api,
        local=local,
        icon=args.icon,
    )
    _print_json({"created": True, "source": config.to_dict()})
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    deleted = delete_source(workspace, args.slug)
    if not deleted:
        _print_json({"error": f"Source '{args.slug}' not found."}, stream=sys.stderr)
        return 1
    _print_json({"deleted": True, "slug": args.slug})
    return 0


def cmd_mark_authenticated(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    steps = [
        f"Resolved workspace: {workspace}",
        f"Looking up source: {args.slug}",
    ]
    ok = mark_source_authenticated(workspace, args.slug)
    if not ok:
        _print_json({"error": f"Source '{args.slug}' not found."}, stream=sys.stderr)
        return 1
    steps.extend(
        [
            "Marked source as authenticated.",
            "Set connectionStatus to connected.",
        ]
    )
    _print_json({"updated": True, "slug": args.slug, "isAuthenticated": True, "connectionStatus": "connected", "steps": steps})
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    sources = load_workspace_sources(workspace)
    auth = load_copilot_auth()
    _print_json(
        {
            "workspace": str(workspace),
            "workspaceExists": workspace.exists(),
            "sourcesDir": str(workspace / "sources"),
            "sourceCount": len(sources),
            "copilotAuth": {"present": bool(auth), "provider": (auth.provider if auth else None), "login": (auth.login if auth else None)},
            "pythonExecutable": sys.executable,
        }
    )
    return 0


def cmd_auth_login(args: argparse.Namespace) -> int:
    steps: list[str] = ["Started provider login flow for github-copilot."]
    token, source = resolve_login_token(args.token, bool(args.from_gh))
    if not token:
        print("No token found. Use --token, set COPILOT_TOKEN/GITHUB_TOKEN, or pass --from-gh.", file=sys.stderr)
        return 2
    steps.append(f"Resolved token source: {source or 'unknown'}")

    login: str | None = None
    if not args.no_validate:
        steps.append("Validating token with GitHub API.")
        ok, login, error = validate_github_token(token)
        if not ok:
            print(error or "Token validation failed.", file=sys.stderr)
            return 2
        steps.append("Token validation succeeded.")
    else:
        steps.append("Skipped token validation (--no-validate).")

    record = build_auth_record(token=token, source=(source or "unknown"), login=login)
    save_copilot_auth(record)
    steps.append("Saved local auth session metadata.")
    _print_json({"authenticated": True, "provider": "github-copilot", "source": source, "login": login, "steps": steps})
    return 0


def cmd_auth_status(args: argparse.Namespace) -> int:
    record = load_copilot_auth()
    if not record:
        _print_json({"authenticated": False, "provider": "github-copilot"})
        return 1
    _print_json(
        {
            "authenticated": True,
            "provider": record.provider,
            "source": record.source,
            "login": record.login,
            "validatedAt": record.validatedAt,
        }
    )
    return 0


def cmd_auth_guide(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    source = load_source_config(workspace, args.source)
    if source is None:
        _print_json({"error": f"Source '{args.source}' not found."}, stream=sys.stderr)
        return 1
    guide = _source_auth_guide(source)
    if bool(args.pretty):
        _print_auth_guide_for_user(guide)
    _print_json({"guide": guide})
    return 0


def cmd_auth_logout(args: argparse.Namespace) -> int:
    removed = clear_copilot_auth()
    _print_json({"loggedOut": True, "removedSession": bool(removed)})
    return 0


def _credential_file(workspace_root: Path, source_slug: str) -> Path:
    return get_source_path(workspace_root, source_slug) / "credential.json"


def _write_cached_credential(workspace_root: Path, source_slug: str, value: str, ttl_hours: int) -> None:
    path = _credential_file(workspace_root, source_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time() * 1000)
    ttl = max(1, int(ttl_hours)) * 3600 * 1000
    payload = {
        "value": value,
        "createdAt": now,
        "expiresAt": now + ttl,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _read_cached_credential(workspace_root: Path, source_slug: str) -> str | None:
    path = _credential_file(workspace_root, source_slug)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    expires_at = payload.get("expiresAt")
    if isinstance(expires_at, int) and expires_at < int(time.time() * 1000):
        return None
    value = payload.get("value")
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _credential_status(workspace_root: Path, source_slug: str) -> dict[str, Any]:
    path = _credential_file(workspace_root, source_slug)
    if not path.exists():
        return {"exists": False, "valid": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"exists": True, "valid": False, "error": "Invalid credential file"}

    expires_at = payload.get("expiresAt") if isinstance(payload.get("expiresAt"), int) else None
    now = int(time.time() * 1000)
    valid = bool(payload.get("value")) and (expires_at is None or expires_at > now)
    return {
        "exists": True,
        "valid": valid,
        "createdAt": payload.get("createdAt"),
        "expiresAt": expires_at,
        "remainingMs": (expires_at - now) if isinstance(expires_at, int) else None,
    }


def cmd_credential_set(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    steps = [
        f"Resolved workspace: {workspace}",
        f"Looking up source: {args.source}",
    ]
    source = load_source_config(workspace, args.source)
    if source is None:
        _print_json({"error": f"Source '{args.source}' not found."}, stream=sys.stderr)
        return 1
    steps.append("Source found.")

    guide = _source_auth_guide(source)
    if bool(args.show_guide):
        _print_auth_guide_for_user(guide)
    steps.append("Prepared authentication guide.")

    credential_value = str(args.value).strip() if args.value is not None else ""
    if not credential_value:
        if not sys.stdin.isatty():
            _print_json(
                {
                    "error": "Missing credential value in non-interactive mode. Pass --value explicitly.",
                    "source": args.source,
                    "hint": "Use --value \"TOKEN\" or run in interactive terminal without --value.",
                },
                stream=sys.stderr,
            )
            return 2
        prompted_value = _prompt_credential_for_source(source)
        if prompted_value is None:
            _print_json({"error": "Credential input cancelled or empty."}, stream=sys.stderr)
            return 2
        credential_value = prompted_value
        steps.append("Collected credential via interactive prompt.")
    else:
        steps.append("Using credential provided via --value.")

    _write_cached_credential(workspace, args.source, credential_value, args.ttl_hours)
    steps.append(f"Stored credential cache with TTL={int(args.ttl_hours)}h.")

    marked_authenticated = False
    mark_choice = args.mark_authenticated
    if mark_choice is None:
        if sys.stdin.isatty() and _source_requires_auth(source):
            mark_choice = _prompt_yes_no("Mark source as authenticated now?", default_yes=True)
        else:
            mark_choice = False

    if mark_choice:
        marked_authenticated = bool(mark_source_authenticated(workspace, args.source))
        if marked_authenticated:
            steps.append("Marked source as authenticated.")

    _print_json(
        {
            "stored": True,
            "source": args.source,
            "ttlHours": int(args.ttl_hours),
            "markedAuthenticated": marked_authenticated,
            "steps": steps,
            "authGuide": guide,
        }
    )
    return 0


def cmd_credential_status(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    source = load_source_config(workspace, args.source)
    if source is None:
        _print_json({"error": f"Source '{args.source}' not found."}, stream=sys.stderr)
        return 1
    status = _credential_status(workspace, args.source)
    _print_json({"source": args.source, **status})
    return 0


def _completion_options_from_args(args: argparse.Namespace) -> CompletionOptions:
    model = args.model or os.environ.get("AGENT_COPILOT_MODEL") or "gpt-5.3-codex"
    return CompletionOptions(
        model=model,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        reasoning_effort=args.reasoning_effort,
        thinking_budget=args.thinking_budget,
        timeout_seconds=args.timeout,
        dry_run=bool(args.dry_run),
        stream=bool(getattr(args, "stream", False)),
    )


def _run_completion(args: argparse.Namespace, user_prompt: str, default_system_prompt: str) -> tuple[dict[str, Any], int]:
    token = _copilot_token() or ""
    options = _completion_options_from_args(args)
    system_prompt = args.system or default_system_prompt
    _emit_runtime_event(enabled=options.stream, event="step", message="Starting Copilot completion request")

    def _on_stream_chunk(chunk: str) -> None:
        _emit_runtime_event(enabled=options.stream, event="stream", message=chunk)

    try:
        result = complete_chat(
            token=token,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            options=options,
            stream_handler=_on_stream_chunk if options.stream else None,
        )
        if options.dry_run:
            _emit_runtime_event(enabled=options.stream, event="step", message="Dry-run completed")
            return ({"mode": args.copilot_command, "dryRun": True, "request": result.request_payload}, 0)
        _emit_runtime_event(enabled=options.stream, event="step", message="Copilot completion request finished")
        return ({"mode": args.copilot_command, "model": result.model or options.model, "usage": result.usage, "output": result.text}, 0)
    except Exception as error:
        _emit_runtime_event(enabled=options.stream, event="error", message=str(error))
        return ({"error": str(error)}, 2)


def _normalize_suggest_output(output: str) -> str:
    cleaned = output.replace("\r\n", "\n").strip()
    if not cleaned:
        return cleaned

    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?p>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?(pre|code|kbd)>", "", cleaned, flags=re.IGNORECASE)

    lines: list[str] = []
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```"):
            continue
        line = re.sub(r"^[-*•●▪◦]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        if line.lower().startswith("command:"):
            line = line.split(":", 1)[1].strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip() if lines else cleaned


def cmd_copilot_suggest(args: argparse.Namespace) -> int:
    user = (load_copilot_auth().login if load_copilot_auth() else None) or "copilot-cli"
    shell = args.shell
    prompt = args.prompt.strip()
    user_prompt = (
        f"Generate the best {shell} command for this request. Return command(s) only, no markdown fences.\n"
        f"Request: {prompt}"
    )
    default_system = (
        "You are a shell command assistant. Prefer safe, reversible commands. "
        "Return only command text, no extra commentary unless strictly needed."
    )
    payload, code = _run_completion(args, user_prompt=user_prompt, default_system_prompt=default_system)
    if code == 0 and not payload.get("dryRun") and isinstance(payload.get("output"), str):
        payload["output"] = _normalize_suggest_output(payload["output"])
    payload["shell"] = shell
    payload["user"] = user
    _print_json(payload)
    return code


def cmd_copilot_explain(args: argparse.Namespace) -> int:
    user = (load_copilot_auth().login if load_copilot_auth() else None) or "copilot-cli"
    command_text = str(args.command or "").strip()
    if not command_text:
        print("Command to explain is required.", file=sys.stderr)
        return 2
    tokens = shlex.split(command_text)
    user_prompt = (
        "Explain this shell command succinctly, including what it does, key flags, and risks.\n"
        f"Command: {command_text}\n"
        f"Tokenized argument count: {len(tokens)}"
    )
    default_system = "You explain shell commands clearly and concisely for developers."
    payload, code = _run_completion(args, user_prompt=user_prompt, default_system_prompt=default_system)
    payload["command"] = command_text
    payload["user"] = user
    _print_json(payload)
    return code


def cmd_copilot_chat(args: argparse.Namespace) -> int:
    user = (load_copilot_auth().login if load_copilot_auth() else None) or "copilot-cli"
    prompt = args.prompt.strip()
    payload, code = _run_completion(
        args,
        user_prompt=prompt,
        default_system_prompt="You are GitHub Copilot-style assistant for engineering tasks.",
    )
    payload["prompt"] = prompt
    payload["user"] = user
    _print_json(payload)
    return code


CONNECT_PRESETS: dict[str, dict[str, Any]] = {
    "linear": {
        "type": "api",
        "provider": "linear",
        "api": {"baseUrl": "https://api.linear.app", "authType": "bearer"},
        "reason": "Linear provides a stable HTTP API.",
    },
    "notion": {
        "type": "api",
        "provider": "notion",
        "api": {"baseUrl": "https://api.notion.com/v1", "authType": "bearer"},
        "reason": "Notion integrations generally use bearer token API access.",
    },
    "zendesk": {
        "type": "api",
        "provider": "zendesk",
        "api": {"baseUrl": "https://your-subdomain.zendesk.com/api/v2", "authType": "bearer"},
        "reason": "Zendesk commonly uses REST API endpoints.",
    },
    "github": {
        "type": "mcp",
        "provider": "github",
        "mcp": {"transport": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "authType": "bearer"},
        "reason": "GitHub often has MCP-compatible tool servers.",
    },
}


API_FALLBACK_PRESETS: dict[str, dict[str, str]] = {
    "github": {"baseUrl": "https://api.github.com", "authType": "bearer"},
    "linear": {"baseUrl": "https://api.linear.app", "authType": "bearer"},
    "notion": {"baseUrl": "https://api.notion.com/v1", "authType": "bearer"},
    "zendesk": {"baseUrl": "https://your-subdomain.zendesk.com/api/v2", "authType": "bearer"},
}


def _find_api_source_for_provider(workspace_root: Path, provider: str, *, exclude_slug: str | None = None) -> tuple[SourceConfig | None, str | None]:
    for item in load_workspace_sources(workspace_root):
        if item.type != "api":
            continue
        if str(item.provider or "").lower() != provider.lower():
            continue
        if exclude_slug and item.slug == exclude_slug:
            continue
        return item, item.slug
    return None, None


def _provider_api_fallback_config(provider: str) -> dict[str, Any] | None:
    preset = API_FALLBACK_PRESETS.get(provider.lower().strip())
    if not preset:
        return None
    return {"baseUrl": str(preset.get("baseUrl") or "").strip(), "authType": str(preset.get("authType") or "bearer").strip()}


def _provider_from_connect_request(request: str) -> str:
    match = re.search(r"connect\s+to\s+([a-zA-Z0-9._-]+)", request, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip().lower()
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", request.lower()).strip("-")
    return cleaned or "custom"


def _discover_connection_config(
    request: str,
    *,
    provider_type: str,
    base_url: str | None,
    auth_type: str | None,
) -> tuple[dict[str, Any], list[str]]:
    provider = _provider_from_connect_request(request)
    trace = [f"I parsed provider '{provider}' from your request."]

    preset = CONNECT_PRESETS.get(provider)
    if preset:
        trace.append(f"I matched a provider preset. {preset.get('reason')}")
        plan: dict[str, Any] = {
            "name": f"connect to {provider}",
            "provider": provider,
            "type": str(preset["type"]),
            "api": (dict(preset["api"]) if isinstance(preset.get("api"), dict) else None),
            "mcp": (dict(preset["mcp"]) if isinstance(preset.get("mcp"), dict) else None),
        }
    else:
        inferred_type = "mcp" if provider_type == "mcp" else "api"
        trace.append(f"No preset found, so I used a {inferred_type.upper()} default.")
        plan = {
            "name": f"connect to {provider}",
            "provider": provider,
            "type": inferred_type,
            "api": {"baseUrl": f"https://api.{provider}.com", "authType": "bearer"} if inferred_type == "api" else None,
            "mcp": {"transport": "http", "url": f"https://mcp.{provider}.com", "authType": "bearer"} if inferred_type == "mcp" else None,
        }

    if provider_type in {"api", "mcp"} and plan.get("type") != provider_type:
        trace.append(f"I respected your provider type hint and switched type to '{provider_type}'.")
        plan["type"] = provider_type
        if provider_type == "api" and not isinstance(plan.get("api"), dict):
            plan["api"] = {"baseUrl": f"https://api.{provider}.com", "authType": "bearer"}
            plan["mcp"] = None
        if provider_type == "mcp" and not isinstance(plan.get("mcp"), dict):
            plan["mcp"] = {"transport": "http", "url": f"https://mcp.{provider}.com", "authType": "bearer"}
            plan["api"] = None

    if base_url:
        if plan["type"] == "api":
            api = plan.get("api") if isinstance(plan.get("api"), dict) else {}
            api["baseUrl"] = base_url
            plan["api"] = api
        else:
            mcp = plan.get("mcp") if isinstance(plan.get("mcp"), dict) else {"transport": "http"}
            mcp["url"] = base_url
            plan["mcp"] = mcp
        trace.append("I applied your explicit base URL override.")

    if auth_type:
        if plan["type"] == "api":
            api = plan.get("api") if isinstance(plan.get("api"), dict) else {}
            api["authType"] = auth_type
            plan["api"] = api
        else:
            mcp = plan.get("mcp") if isinstance(plan.get("mcp"), dict) else {}
            mcp["authType"] = auth_type
            plan["mcp"] = mcp
        trace.append("I applied your explicit auth type override.")

    return plan, trace


def cmd_connect(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    stream_enabled = bool(getattr(args, "stream", False))
    request = str(args.request or "").strip()
    if not request:
        print("Request is required", file=sys.stderr)
        return 2

    _emit_runtime_event(enabled=stream_enabled, event="step", message=f"Processing connect request: {request}")

    plan, trace = _discover_connection_config(
        request,
        provider_type=str(args.provider_type or "auto"),
        base_url=(str(args.base_url).strip() if args.base_url else None),
        auth_type=(str(args.auth_type).strip() if args.auth_type else None),
    )
    for item in trace:
        _emit_runtime_event(enabled=stream_enabled, event="reasoning", message=item)
    source_type = str(plan.get("type") or "api")

    connect_capabilities: dict[str, Any] = {"sourceType": source_type}
    if source_type == "api":
        api_cfg = plan.get("api") if isinstance(plan.get("api"), dict) else {}
        connect_capabilities["baseUrl"] = str(api_cfg.get("baseUrl") or "")
        connect_capabilities["authType"] = str(api_cfg.get("authType") or "none")
        connect_capabilities["supportedIntents"] = _contextual_supported_intents(
            request,
            source_type="api",
            capabilities={"supportedIntents": ["api_call", "list_issues", "create_issue"]},
        )
    elif source_type == "mcp":
        mcp_cfg = plan.get("mcp") if isinstance(plan.get("mcp"), dict) else {}
        connect_capabilities["transport"] = str(mcp_cfg.get("transport") or "http")
        connect_capabilities["supportedIntents"] = _contextual_supported_intents(
            request,
            source_type="mcp",
            capabilities={"supportedIntents": ["mcp_call"]},
        )
    else:
        connect_capabilities["supportedIntents"] = []
    trace.append(f"I inferred prompt-context supported intents: {json.dumps(connect_capabilities.get('supportedIntents') or [])}.")

    if args.dry_run:
        _emit_runtime_event(enabled=stream_enabled, event="step", message="Connect dry-run completed")
        payload = {
            "mode": "connect",
            "dryRun": True,
            "plan": plan,
            "capabilities": connect_capabilities,
            "agentReasoning": trace,
        }
        _print_json(payload)
        return 0

    created = create_source(
        workspace_root=workspace,
        name=str(plan["name"]),
        source_type=source_type,
        provider=str(plan.get("provider") or "custom"),
        enabled=True,
        api=(plan.get("api") if isinstance(plan.get("api"), dict) else None),
        mcp=(plan.get("mcp") if isinstance(plan.get("mcp"), dict) else None),
    )

    auth_required = _source_requires_auth(created)
    created.isAuthenticated = False if auth_required else None
    created.connectionStatus = "needs_auth" if auth_required else "untested"
    created.connectionError = None
    save_source_config(workspace, created)
    _emit_runtime_event(enabled=stream_enabled, event="step", message=f"Created source '{created.slug}' ({source_type})")

    onboarding_steps: list[str] = []
    onboarding_error: str | None = None
    docs_confirmed: bool | None = None
    docs_probe: dict[str, Any] | None = None

    auto_auth = getattr(args, "auto_auth", None)
    if auth_required:
        if auto_auth is None and sys.stdin.isatty():
            auto_auth = _prompt_yes_no("Authentication is required. Start guided authentication now?", default_yes=True)

        if auto_auth:
            guide = _source_auth_guide(created)
            _print_auth_guide_for_user(guide)
            _append_step(onboarding_steps, "Displayed authentication guide.", stream=stream_enabled)

            docs = _normalize_doc_links(guide.get("docs"))
            if docs:
                if sys.stdin.isatty():
                    docs_confirmed = _prompt_yes_no("Are these documentation links correct for your setup?", default_yes=True)
                    if docs_confirmed:
                        _append_step(onboarding_steps, "User confirmed authentication docs are correct.", stream=stream_enabled)
                    else:
                        _append_step(onboarding_steps, "User reported suggested authentication docs are not correct.", stream=stream_enabled)
                        _append_step(onboarding_steps, "Starting agentic documentation probe for better auth links.", stream=stream_enabled)
                        auth_type = "none"
                        if created.type == "api" and isinstance(created.api, dict):
                            auth_type = str(created.api.get("authType") or "none").lower()
                        elif created.type == "mcp" and isinstance(created.mcp, dict):
                            auth_type = str(created.mcp.get("authType") or "none").lower()

                        refined_docs, probe_reasoning = _probe_auth_documentation(
                            provider=str(created.provider or "custom"),
                            source_type=str(created.type),
                            auth_type=auth_type,
                            model=getattr(args, "model", None),
                            timeout=int(getattr(args, "timeout", 60)),
                        )
                        docs_probe = {
                            "attempted": True,
                            "reasoning": probe_reasoning,
                            "docs": refined_docs,
                        }
                        if probe_reasoning:
                            _emit_runtime_event(enabled=stream_enabled, event="reasoning", message=probe_reasoning)
                        if refined_docs:
                            _append_step(onboarding_steps, "Found refined authentication docs via agentic probe.", stream=stream_enabled)
                            _print_documentation_links(refined_docs, heading="Refined documentation")
                            if sys.stdin.isatty():
                                docs_confirmed = _prompt_yes_no(
                                    "Use these refined documentation links and continue onboarding?",
                                    default_yes=True,
                                )
                                if docs_confirmed:
                                    _append_step(onboarding_steps, "User accepted refined authentication docs.", stream=stream_enabled)
                                else:
                                    _append_step(onboarding_steps, "User declined refined authentication docs; continuing onboarding anyway.", stream=stream_enabled)
                        else:
                            _append_step(onboarding_steps, "Could not find better docs; continuing onboarding with current context.", stream=stream_enabled)
                else:
                    _append_step(onboarding_steps, "Could not confirm documentation links in non-interactive mode.", stream=stream_enabled)

            credential_value = ""
            if not onboarding_error:
                credential_value = str(getattr(args, "auth_value", "") or "").strip()
                if credential_value:
                    _append_step(onboarding_steps, "Using credential provided via --auth-value.", stream=stream_enabled)
                elif sys.stdin.isatty():
                    prompted = _prompt_credential_for_source(created)
                    if prompted is None:
                        onboarding_error = "Credential input cancelled or empty."
                    else:
                        credential_value = prompted
                        _append_step(onboarding_steps, "Collected credential via interactive prompt.", stream=stream_enabled)
                else:
                    onboarding_error = "Interactive auth requires a TTY. Pass --auth-value or skip --auto-auth."

            if credential_value and not onboarding_error:
                _write_cached_credential(workspace, created.slug, credential_value, int(getattr(args, "ttl_hours", 24)))
                _append_step(onboarding_steps, f"Stored credential cache with TTL={int(getattr(args, 'ttl_hours', 24))}h.", stream=stream_enabled)

                mark_choice = getattr(args, "mark_authenticated", None)
                if mark_choice is None and sys.stdin.isatty():
                    mark_choice = _prompt_yes_no("Mark source as authenticated now?", default_yes=True)
                elif mark_choice is None:
                    mark_choice = False

                if mark_choice:
                    if mark_source_authenticated(workspace, created.slug):
                        _append_step(onboarding_steps, "Marked source as authenticated.", stream=stream_enabled)
                        refreshed = load_source_config(workspace, created.slug)
                        if refreshed is not None:
                            created = refreshed
                    else:
                        onboarding_error = "Failed to mark source authenticated."

    if onboarding_error:
        _emit_runtime_event(enabled=stream_enabled, event="error", message=onboarding_error)

    next_steps = (
        [
            f"Set credentials: agentctl credential set --workspace {workspace} --source {created.slug} --value \"YOUR_TOKEN\"",
            f"Mark authenticated: agentctl mark-authenticated --workspace {workspace} {created.slug}",
        ]
        if auth_required
        else [f"Run a test action: agentctl chat --cli \"list data from {created.provider or created.slug}\" --source {created.slug} --workspace {workspace}"]
    )

    result: dict[str, Any] = {
        "mode": "connect",
        "created": True,
        "source": created.to_dict(),
        "capabilities": connect_capabilities,
        "authentication": {
            "required": auth_required,
            "status": (
                "connected"
                if auth_required and bool(created.isAuthenticated)
                else ("pending" if auth_required else "not_required")
            ),
            "nextSteps": next_steps,
            "onboarding": {
                "enabled": bool(auto_auth) if auth_required else False,
                "docsConfirmed": docs_confirmed,
                "docsProbe": docs_probe,
                "steps": onboarding_steps,
                "error": onboarding_error,
            },
        },
    }
    if bool(args.show_reasoning):
        result["agentReasoning"] = trace + [
            f"I created source '{created.slug}' in workspace '{workspace}'.",
            (
                "I did not mark the source as connected yet because authentication is still required."
                if auth_required
                else "I marked the source as untested (no authentication required at connect-time)."
            ),
        ]
    _emit_runtime_event(enabled=stream_enabled, event="step", message="Connect request completed")
    _print_json(result)
    return 0


def _resolve_source_from_request(
    workspace_root: Path,
    *,
    request: str,
    explicit_source: str | None,
) -> tuple[SourceConfig | None, str | None]:
    if explicit_source:
        source = load_source_config(workspace_root, explicit_source)
        return source, explicit_source if source else None

    all_sources = load_workspace_sources(workspace_root)
    if not all_sources:
        return None, None

    lowered = request.lower()
    for source in all_sources:
        if source.slug.lower() in lowered:
            return source, source.slug
        if source.name.lower() in lowered:
            return source, source.slug
        provider = (source.provider or "").lower()
        if provider and re.search(rf"\b{re.escape(provider)}\b", lowered):
            return source, source.slug

    if len(all_sources) == 1:
        only = all_sources[0]
        return only, only.slug
    return None, None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    snippet = text[start : end + 1]
    try:
        parsed = json.loads(snippet)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _plan_tool_action(
    request: str,
    *,
    provider: str,
    source_type: str,
    capabilities: dict[str, Any],
    model: str | None,
    timeout: int,
) -> tuple[dict[str, Any], str]:
    fallback = _fallback_action_plan(request, provider=provider, source_type=source_type, capabilities=capabilities)
    token = _copilot_token() or ""
    supported = capabilities.get("supportedIntents") if isinstance(capabilities.get("supportedIntents"), list) else []
    intent_options = [str(item).strip() for item in supported if str(item).strip()]
    if not intent_options:
        intent_options = ["list_issues", "create_issue", "api_call", "mcp_call"]
    prompt = (
        "Return strict JSON only with shape: "
        "{\"intent\": string, \"reasoning\": string, \"params\": object}.\n"
        f"Provider: {provider}\n"
        f"SourceType: {source_type}\n"
        f"Capabilities: {json.dumps(capabilities)}\n"
        f"UserRequest: {request}\n"
        f"Intent options: {', '.join(intent_options)}."
    )
    try:
        result = complete_chat(
            token=token,
            user_prompt=prompt,
            system_prompt="You are a planning assistant for API and MCP tool actions.",
            options=CompletionOptions(
                model=(model or os.environ.get("AGENT_COPILOT_MODEL") or "gpt-5.3-codex"),
                temperature=None,
                max_tokens=None,
                reasoning_effort=None,
                thinking_budget=None,
                timeout_seconds=max(10, int(timeout)),
                dry_run=False,
            ),
        )
        raw = result.text
        parsed = _extract_json_object(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("intent"), str):
            params = parsed.get("params") if isinstance(parsed.get("params"), dict) else {}
            return (
                {
                    "intent": str(parsed.get("intent") or fallback["intent"]),
                    "reasoning": str(parsed.get("reasoning") or fallback.get("reasoning") or ""),
                    "params": params,
                },
                raw,
            )
    except Exception:
        pass

    return fallback, json.dumps(fallback)


def _fallback_action_plan(
    request: str,
    *,
    provider: str,
    source_type: str,
    capabilities: dict[str, Any],
) -> dict[str, Any]:
    lowered = request.lower()

    if source_type == "mcp":
        tools = capabilities.get("tools") if isinstance(capabilities.get("tools"), list) else []
        tool_name = ""
        for tool in tools:
            tool_str = str(tool)
            if tool_str.lower() in lowered:
                tool_name = tool_str
                break
        if not tool_name and tools:
            tool_name = str(tools[0])
        return {
            "intent": "mcp_call",
            "reasoning": "I mapped the request to an MCP tool invocation.",
            "params": {"toolName": tool_name, "arguments": {}},
        }

    issue_like = any(token in lowered for token in ["issue", "issues", "ticket", "tickets", "bug", "bugs"])

    if "list" in lowered and issue_like:
        return {
            "intent": "list_issues",
            "reasoning": "I recognized a request to list issues.",
            "params": {},
        }
    if any(token in lowered for token in ["create", "open", "file", "add"]) and issue_like:
        title_match = re.search(r"titled\s+(.+?)(?:\s+description\s+|$)", request, flags=re.IGNORECASE)
        if not title_match:
            title_match = re.search(r"(?:create|open|file|add)\s+(?:an?\s+)?(?:issue|ticket|bug)\s+(?:in\s+[^\s]+\s+)?(?:named|called|title[d]?\s*)?(.+)$", request, flags=re.IGNORECASE)
        description_match = re.search(r"description\s+(.+)$", request, flags=re.IGNORECASE)
        team_match = re.search(r"team\s+([A-Za-z0-9_-]+)", request, flags=re.IGNORECASE)
        return {
            "intent": "create_issue",
            "reasoning": "I recognized a request to create an issue.",
            "params": {
                "title": (title_match.group(1).strip() if title_match else ""),
                "description": (description_match.group(1).strip() if description_match else ""),
                "teamKey": (team_match.group(1).strip() if team_match else ""),
            },
        }

    method = "GET"
    if any(word in lowered for word in ["create", "post", "add"]):
        method = "POST"
    elif any(word in lowered for word in ["update", "patch"]):
        method = "PATCH"
    elif any(word in lowered for word in ["delete", "remove"]):
        method = "DELETE"

    path_match = re.search(r"(/[-a-zA-Z0-9_./]+)", request)
    path = path_match.group(1) if path_match else "/"
    return {
        "intent": "api_call",
        "reasoning": f"I mapped this to a generic API call for provider '{provider}'.",
        "params": {"method": method, "path": path, "query": {}, "body": {}, "headers": {}},
    }


def _contextual_supported_intents(
    request: str,
    *,
    source_type: str,
    capabilities: dict[str, Any],
) -> list[str]:
    if source_type == "mcp":
        return ["mcp_call"]

    available = capabilities.get("supportedIntents") if isinstance(capabilities.get("supportedIntents"), list) else []
    allowed = {str(item).strip() for item in available if str(item).strip()}
    if not allowed:
        allowed = {"api_call", "list_issues", "create_issue"}

    lowered = request.lower()
    wants_list = bool(re.search(r"\b(list\w*|show\w*|get\w*|fetch\w*|find\w*)\b", lowered))
    wants_create = bool(re.search(r"\b(creat\w*|open\w*|file\w*|add\w*|submit\w*)\b", lowered))
    mentions_issue = bool(re.search(r"\b(issue|issues|ticket|tickets|bug|bugs)\b", lowered))

    intents: list[str] = []
    if wants_create and mentions_issue and "create_issue" in allowed:
        intents.append("create_issue")
    if wants_list and mentions_issue and "list_issues" in allowed:
        intents.append("list_issues")
    if "api_call" in allowed:
        intents.append("api_call")

    if not intents:
        if "api_call" in allowed:
            intents.append("api_call")
        elif "list_issues" in allowed:
            intents.append("list_issues")
        elif "create_issue" in allowed:
            intents.append("create_issue")

    deduped: list[str] = []
    seen: set[str] = set()
    for item in intents:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _self_heal_action_plan(
    *,
    request: str,
    provider: str,
    source_type: str,
    intent: str,
    params: dict[str, Any],
    capabilities: dict[str, Any],
    error_message: str,
    model: str | None,
    timeout: int,
) -> dict[str, Any]:
    token = _copilot_token() or ""
    prompt = (
        "Return strict JSON with shape: {\"reasoning\": string, \"paramsPatch\": object}.\n"
        f"Request: {request}\nProvider: {provider}\nSourceType: {source_type}\nIntent: {intent}\n"
        f"Capabilities: {json.dumps(capabilities)}\nCurrentParams: {json.dumps(params)}\nError: {error_message}"
    )
    try:
        result = complete_chat(
            token=token,
            user_prompt=prompt,
            system_prompt="You repair failed API/MCP action plans by suggesting minimal parameter patches.",
            options=CompletionOptions(
                model=(model or os.environ.get("AGENT_COPILOT_MODEL") or "gpt-5.3-codex"),
                temperature=None,
                max_tokens=None,
                reasoning_effort=None,
                thinking_budget=None,
                timeout_seconds=max(10, int(timeout)),
                dry_run=False,
            ),
        )
        parsed = _extract_json_object(result.text)
        if isinstance(parsed, dict):
            patch = parsed.get("paramsPatch") if isinstance(parsed.get("paramsPatch"), dict) else {}
            return {"reasoning": str(parsed.get("reasoning") or "I prepared a retry patch."), "paramsPatch": patch}
    except Exception:
        pass
    return {"reasoning": "I prepared a conservative retry patch.", "paramsPatch": {}}


def _normalized_api_auth_type(raw_auth_type: str) -> str:
    normalized = str(raw_auth_type or "none").strip().lower()
    if normalized == "oauth":
        return "bearer"
    if normalized in {"bearer", "basic", "header", "query", "none"}:
        return normalized
    return "none"


def _api_auth_attempt_order(preferred_auth_type: str) -> list[str]:
    preferred = _normalized_api_auth_type(preferred_auth_type)
    ordered = [preferred, *API_AUTH_TYPES]
    deduped: list[str] = []
    seen: set[str] = set()
    for item in ordered:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _append_query_param(url: str, key: str, value: str) -> str:
    from urllib.parse import urlencode

    query_text = urlencode({key: value}, doseq=True)
    if not query_text:
        return url
    return f"{url}{'&' if '?' in url else '?'}{query_text}"


def _apply_api_auth_type(
    *,
    auth_type: str,
    source_api: dict[str, Any],
    credential: str | None,
    base_headers: dict[str, str],
    base_url: str,
) -> tuple[dict[str, str], str]:
    headers = dict(base_headers)
    url = base_url
    if not credential:
        return headers, url

    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {credential}"
    elif auth_type == "basic":
        encoded = base64.b64encode(credential.encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    elif auth_type == "header":
        header_name = str(source_api.get("authHeaderName") or "Authorization").strip() or "Authorization"
        headers[header_name] = credential
    elif auth_type == "query":
        query_name = str(source_api.get("authQueryParam") or "api_key").strip() or "api_key"
        url = _append_query_param(url, query_name, credential)

    return headers, url


def _generic_api_request(
    *,
    workspace_root: Path,
    source_slug: str,
    source_api: dict[str, Any],
    method: str,
    path: str,
    query: dict[str, Any],
    body: dict[str, Any],
    headers: dict[str, Any],
    auth_types_try: list[str] | None = None,
) -> dict[str, Any]:
    base_url = str(source_api.get("baseUrl") or "").strip()
    if not base_url:
        raise RuntimeError("API source is missing baseUrl")

    normalized_method = method.upper().strip() or "GET"
    final_path = path.strip() or "/"
    if not final_path.startswith("/") and not final_path.lower().startswith("http"):
        final_path = "/" + final_path

    if final_path.lower().startswith("http"):
        url = final_path
    else:
        url = base_url.rstrip("/") + final_path

    if query:
        from urllib.parse import urlencode

        query_text = urlencode({k: v for k, v in query.items() if v is not None}, doseq=True)
        if query_text:
            url = f"{url}{'&' if '?' in url else '?'}{query_text}"

    base_headers = {"Accept": "application/json"}
    base_headers.update({str(k): str(v) for k, v in headers.items() if v is not None})

    if isinstance(auth_types_try, list) and auth_types_try:
        auth_attempts = [item for item in auth_types_try if item in set(API_AUTH_TYPES)]
        if not auth_attempts:
            preferred_auth_type = str(source_api.get("authType") or "none").lower()
            auth_attempts = _api_auth_attempt_order(preferred_auth_type)
    else:
        preferred_auth_type = str(source_api.get("authType") or "none").lower()
        auth_attempts = _api_auth_attempt_order(preferred_auth_type)
    credential = _read_cached_credential(workspace_root, source_slug)

    payload_bytes: bytes | None = None
    if normalized_method in {"POST", "PUT", "PATCH", "DELETE"}:
        payload_bytes = json.dumps(body or {}).encode("utf-8")
        base_headers["Content-Type"] = "application/json"

    auth_failures: list[str] = []
    attempted: list[str] = []
    for attempt_auth in auth_attempts:
        if attempt_auth != "none" and not credential:
            continue
        request_headers, final_url = _apply_api_auth_type(
            auth_type=attempt_auth,
            source_api=source_api,
            credential=credential,
            base_headers=base_headers,
            base_url=url,
        )
        request = Request(url=final_url, method=normalized_method, headers=request_headers, data=payload_bytes)
        attempted.append(attempt_auth)
        try:
            with urlopen(request, timeout=30) as response:
                raw_bytes = response.read()
                status = int(getattr(response, "status", 200))
                content_type = str(response.headers.get("Content-Type") or "")
                text = raw_bytes.decode("utf-8", errors="replace")
            if "json" in content_type.lower():
                try:
                    parsed_body: Any = json.loads(text)
                except Exception:
                    parsed_body = text
            else:
                parsed_body = text
            return {
                "status": status,
                "method": normalized_method,
                "url": final_url,
                "contentType": content_type,
                "authTypeUsed": attempt_auth,
                "authTypesTried": attempted,
                "data": parsed_body,
            }
        except HTTPError as error:
            body_text = ""
            try:
                body_text = error.read().decode("utf-8", errors="replace")
            except Exception:
                body_text = ""
            if _http_error_is_auth_failure(int(getattr(error, "code", 0)), body_text):
                auth_failures.append(f"{attempt_auth}: HTTP {error.code} {body_text[:300]}")
                continue
            raise RuntimeError(f"API error (HTTP {error.code}): {body_text[:1200]}") from error
        except URLError as error:
            reason = getattr(error, "reason", error)
            raise RuntimeError(f"API unreachable: {reason}") from error

    if auth_failures:
        tried = ", ".join(attempted) if attempted else "none"
        details = " | ".join(auth_failures[:5])
        raise RuntimeError(
            f"API authorization failed after trying auth types [{tried}]. "
            f"Last errors: {details}"
        )
    raise RuntimeError("API request failed before auth attempts could run.")


def _mcp_build_auth_headers(workspace_root: Path, source_slug: str, mcp_cfg: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
    auth_type = str(mcp_cfg.get("authType") or "none").lower()
    credential = _read_cached_credential(workspace_root, source_slug)
    if auth_type in {"bearer", "oauth"} and credential:
        headers["Authorization"] = f"Bearer {credential}"
    return headers


def _mcp_read_stdio_message(stream) -> dict[str, Any] | None:
    content_length = 0
    while True:
        line = stream.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            break
        header = line.decode("utf-8", errors="replace").strip()
        if header.lower().startswith("content-length:"):
            content_length = int(header.split(":", 1)[1].strip())

    if content_length <= 0:
        return None
    body = stream.read(content_length)
    if not body:
        return None
    return json.loads(body.decode("utf-8", errors="replace"))


def _mcp_write_stdio_message(stream, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    stream.write(header)
    stream.write(body)
    stream.flush()


def _mcp_stdio_request(mcp_cfg: dict[str, Any], method: str, params: dict[str, Any], timeout: int) -> dict[str, Any]:
    command = str(mcp_cfg.get("command") or "").strip()
    if not command:
        raise RuntimeError("MCP stdio source missing mcp.command")
    args = mcp_cfg.get("args") if isinstance(mcp_cfg.get("args"), list) else []
    process = subprocess.Popen(
        [command, *[str(item) for item in args]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise RuntimeError("Failed to open MCP stdio process pipes")

    message_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def _reader() -> None:
        assert process.stdout is not None
        while True:
            try:
                item = _mcp_read_stdio_message(process.stdout)
            except Exception:
                item = None
            message_queue.put(item)
            if item is None:
                break

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    def _next_message(deadline: float) -> dict[str, Any] | None:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                item = message_queue.get(timeout=min(0.2, remaining))
            except queue.Empty:
                if process.poll() is not None:
                    return None
                continue
            return item

    request_id = 1
    try:
        _mcp_write_stdio_message(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "agentctl", "version": "0.1.0"},
                },
            },
        )

        init_deadline = time.monotonic() + max(1, int(timeout))
        while True:
            message = _next_message(init_deadline)
            if message is None:
                break
            if message.get("id") == request_id:
                break

        _mcp_write_stdio_message(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
        )

        request_id += 1
        _mcp_write_stdio_message(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            },
        )

        call_deadline = time.monotonic() + max(1, int(timeout))
        while True:
            message = _next_message(call_deadline)
            if message is None:
                break
            if message.get("id") != request_id:
                continue
            if isinstance(message.get("error"), dict):
                error_obj = message["error"]
                raise RuntimeError(str(error_obj.get("message") or "MCP stdio error"))
            if "result" in message:
                return message["result"] if isinstance(message["result"], dict) else {"result": message["result"]}
            return message

        try:
            process.kill()
        except Exception:
            pass
        raise RuntimeError("MCP stdio timeout/error.")
    finally:
        try:
            process.kill()
        except Exception:
            pass


def _mcp_http_request(
    *,
    workspace_root: Path,
    source_slug: str,
    mcp_cfg: dict[str, Any],
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    url = str(mcp_cfg.get("url") or "").strip()
    if not url:
        raise RuntimeError("MCP HTTP source missing mcp.url")

    headers = _mcp_build_auth_headers(workspace_root, source_slug, mcp_cfg)
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = Request(url=url, method="POST", headers=headers, data=json.dumps(payload).encode("utf-8"))
    try:
        with urlopen(req, timeout=30) as response:
            text = response.read().decode("utf-8", errors="replace")
        response_payload = json.loads(text)
        if isinstance(response_payload.get("error"), dict):
            raise RuntimeError(str(response_payload["error"].get("message") or "MCP HTTP error"))
        result = response_payload.get("result")
        return result if isinstance(result, dict) else {"result": result}
    except HTTPError as error:
        body_text = ""
        try:
            body_text = error.read().decode("utf-8", errors="replace")
        except Exception:
            body_text = ""
        raise RuntimeError(f"MCP HTTP error (HTTP {error.code}): {body_text[:1000]}") from error
    except URLError as error:
        reason = getattr(error, "reason", error)
        raise RuntimeError(f"MCP HTTP unreachable: {reason}") from error


def _mcp_request(
    *,
    workspace_root: Path,
    source_slug: str,
    mcp_cfg: dict[str, Any],
    method: str,
    params: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    transport = str(mcp_cfg.get("transport") or "http").lower()
    if transport == "stdio":
        return _mcp_stdio_request(mcp_cfg, method=method, params=params, timeout=max(10, timeout))
    return _mcp_http_request(
        workspace_root=workspace_root,
        source_slug=source_slug,
        mcp_cfg=mcp_cfg,
        method=method,
        params=params,
    )


def _extract_mcp_tool_names(payload: dict[str, Any]) -> list[str]:
    tools_payload = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    names: list[str] = []
    for item in tools_payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            names.append(name)
    seen: set[str] = set()
    deduped: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        deduped.append(name)
    return deduped


def _probe_source_capabilities(
    workspace_root: Path,
    source_slug: str,
    source: SourceConfig,
    *,
    mcp_probe_mode: str,
    timeout: int,
) -> dict[str, Any]:
    if source.type == "api":
        api = source.api if isinstance(source.api, dict) else {}
        return {
            "sourceType": "api",
            "baseUrl": str(api.get("baseUrl") or ""),
            "authType": str(api.get("authType") or "none"),
            "supportedIntents": ["api_call", "list_issues", "create_issue"],
        }

    if source.type == "mcp":
        mcp_cfg = source.mcp if isinstance(source.mcp, dict) else {}
        transport = str(mcp_cfg.get("transport") or "http").lower()
        capabilities: dict[str, Any] = {
            "sourceType": "mcp",
            "transport": transport,
            "supportedIntents": ["mcp_call"],
            "tools": [],
            "mcpProbeMode": mcp_probe_mode,
        }

        if mcp_probe_mode == "off":
            capabilities["probeSkipped"] = "MCP live probing disabled by --mcp-probe off"
            return capabilities

        if mcp_probe_mode == "cached":
            cached = mcp_cfg.get("tools") if isinstance(mcp_cfg.get("tools"), list) else []
            capabilities["tools"] = [str(item) for item in cached if str(item).strip()]
            capabilities["probeSource"] = "mcp.tools"
            if not capabilities["tools"]:
                capabilities["probeSkipped"] = "No cached tools found in mcp.tools"
            return capabilities

        base_timeout = max(10, int(timeout))
        attempts = [base_timeout, max(base_timeout + 15, base_timeout * 2)]
        last_error: Exception | None = None
        for index, attempt_timeout in enumerate(attempts, start=1):
            try:
                result = _mcp_request(
                    workspace_root=workspace_root,
                    source_slug=source_slug,
                    mcp_cfg=mcp_cfg,
                    method="tools/list",
                    params={},
                    timeout=attempt_timeout,
                )
                names = _extract_mcp_tool_names(result)
                capabilities["tools"] = names
                capabilities["probeLive"] = True
                capabilities["probeSource"] = "tools/list"
                capabilities["probeAttempts"] = index
                capabilities["probeTimeoutSeconds"] = attempt_timeout
                if not names:
                    capabilities["probeWarning"] = "MCP probe succeeded but no tools were returned"
                return capabilities
            except Exception as error:
                last_error = error
                continue

        capabilities["probeLive"] = False
        capabilities["probeAttempts"] = len(attempts)
        capabilities["probeError"] = str(last_error) if last_error else "MCP probe failed"
        return capabilities

    return {"sourceType": source.type, "supportedIntents": []}


def _linear_graphql(
    workspace_root: Path,
    source_slug: str,
    source_api: dict[str, Any],
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    base_url = str(source_api.get("baseUrl") or "https://api.linear.app").rstrip("/")
    url = base_url + "/graphql"
    token = _read_cached_credential(workspace_root, source_slug)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        normalized = str(token).strip()
        if normalized.lower().startswith("bearer "):
            normalized = normalized.split(" ", 1)[1].strip()
        headers["Authorization"] = normalized
    payload = {"query": query, "variables": variables}
    req = Request(url=url, method="POST", headers=headers, data=json.dumps(payload).encode("utf-8"))
    try:
        with urlopen(req, timeout=30) as response:
            text = response.read().decode("utf-8", errors="replace")
        parsed = json.loads(text)
        if isinstance(parsed, dict) and parsed.get("errors"):
            raise RuntimeError(f"Linear API error: {parsed['errors']}")
        return parsed if isinstance(parsed, dict) else {"data": parsed}
    except HTTPError as error:
        body_text = ""
        try:
            body_text = error.read().decode("utf-8", errors="replace")
        except Exception:
            body_text = ""
        raise RuntimeError(f"Linear API error (HTTP {error.code}): {body_text[:1000]}") from error


def _linear_resolve_team_id(
    workspace_root: Path,
    source_slug: str,
    source_api: dict[str, Any],
    team_key: str | None,
) -> tuple[str | None, list[dict[str, Any]]]:
    query = """
    query Teams {
      teams(first: 50) {
        nodes {
          id
          key
          name
        }
      }
    }
    """
    result = _linear_graphql(workspace_root, source_slug, source_api, query, {})
    teams = (((result.get("data") or {}).get("teams") or {}).get("nodes") or [])
    normalized = []
    for item in teams:
        if isinstance(item, dict):
            normalized.append({"id": item.get("id"), "key": item.get("key"), "name": item.get("name")})

    if not normalized:
        return None, normalized

    if not team_key:
        if len(normalized) == 1:
            first = normalized[0]
            return str(first.get("id") or ""), normalized
        return None, normalized

    key_lower = team_key.lower().strip()
    for team in normalized:
        if str(team.get("key") or "").lower() == key_lower:
            return str(team.get("id") or ""), normalized
    return None, normalized


def cmd_act(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    stream_enabled = bool(getattr(args, "stream", False))
    source_slug = str(args.source or "").strip()
    request = str(args.request or "").strip()
    try:
        requested_auth_types = _parse_api_auth_types_arg(getattr(args, "auth_types_try", None))
    except ValueError as error:
        _print_json({"error": str(error)}, stream=sys.stderr)
        return 2
    if not request:
        print("Action request is required", file=sys.stderr)
        return 2

    _emit_runtime_event(enabled=stream_enabled, event="step", message=f"Starting action request: {request}")

    source, resolved_slug = _resolve_source_from_request(workspace, request=request, explicit_source=(source_slug or None))
    source_slug = resolved_slug or source_slug
    if source is None:
        available = [item.slug for item in load_workspace_sources(workspace)]
        _print_json(
            {
                "error": "Could not resolve a source for this request.",
                "hint": "Include provider/source name in your request or pass --source explicitly.",
                "availableSources": available,
            },
            stream=sys.stderr,
        )
        return 1

    if source.type not in {"api", "mcp"}:
        print("Only API and MCP sources are supported for agent actions right now.", file=sys.stderr)
        return 2

    provider = str(source.provider or "").lower() or "custom"
    effective_source_type = str(source.type)
    effective_source_api = source.api if isinstance(source.api, dict) else None
    mcp_probe_mode = str(getattr(args, "mcp_probe", "live") or "live").strip().lower()
    if mcp_probe_mode not in {"live", "cached", "off"}:
        mcp_probe_mode = "live"
    if effective_source_type == "mcp":
        _emit_runtime_event(
            enabled=stream_enabled,
            event="step",
            message=f"Probing MCP tools (mode={mcp_probe_mode})",
        )
    capabilities = _probe_source_capabilities(
        workspace,
        source_slug,
        source,
        mcp_probe_mode=mcp_probe_mode,
        timeout=args.timeout,
    )
    if effective_source_type == "mcp" and isinstance(capabilities.get("probeError"), str):
        _emit_runtime_event(
            enabled=stream_enabled,
            event="error",
            message=f"MCP probing failed: {capabilities.get('probeError')}",
        )
        if mcp_probe_mode == "live" and isinstance(source.mcp, dict):
            cached_tools = source.mcp.get("tools") if isinstance(source.mcp.get("tools"), list) else []
            cached_names = [str(item).strip() for item in cached_tools if str(item).strip()]
            if cached_names:
                capabilities["tools"] = cached_names
                capabilities["probeFallback"] = "cached"
                _emit_runtime_event(
                    enabled=stream_enabled,
                    event="fix",
                    message="Live MCP probe failed; using cached MCP tools from source config.",
                )
        if bool(getattr(args, "api_fallback_on_mcp_failure", True)):
            fallback_source, fallback_slug = _find_api_source_for_provider(workspace, provider, exclude_slug=source_slug)
            if fallback_source is not None and fallback_slug is not None and isinstance(fallback_source.api, dict):
                source = fallback_source
                source_slug = fallback_slug
                provider = str(source.provider or provider).lower() or provider
                effective_source_type = "api"
                effective_source_api = source.api
                capabilities = _probe_source_capabilities(
                    workspace,
                    source_slug,
                    source,
                    mcp_probe_mode="off",
                    timeout=args.timeout,
                )
                capabilities["fallbackFromMcpSlug"] = str(args.source or "") or None
                _emit_runtime_event(
                    enabled=stream_enabled,
                    event="fix",
                    message=f"Switched to API source '{source_slug}' after MCP probe failure.",
                )
            else:
                fallback_api = _provider_api_fallback_config(provider)
                if isinstance(fallback_api, dict):
                    effective_source_type = "api"
                    effective_source_api = fallback_api
                    capabilities = {
                        "sourceType": "api",
                        "baseUrl": str(fallback_api.get("baseUrl") or ""),
                        "authType": str(fallback_api.get("authType") or "none"),
                        "supportedIntents": ["api_call", "list_issues", "create_issue"],
                        "fallbackFromMcpSlug": source_slug,
                        "fallbackMode": "provider_preset",
                    }
                    _emit_runtime_event(
                        enabled=stream_enabled,
                        event="fix",
                        message=f"Switched to provider API fallback for '{provider}' after MCP probe failure.",
                    )
            if _interactive_fix_enabled(args) and mcp_probe_mode == "live":
                selection = _select_interactive_option(
                    "Interactive fix assistant: MCP probe failed. Try one quick fix:",
                    [
                        "Retry using cached MCP tools (--mcp-probe cached)",
                        "Retry without MCP probing (--mcp-probe off)",
                        "Increase timeout and retry live probe",
                    ],
                )
                if selection == 0:
                    _emit_runtime_event(enabled=stream_enabled, event="fix", message="Retrying with --mcp-probe cached")
                    return _retry_cmd_act_with_overrides(args, mcp_probe="cached")
                if selection == 1:
                    _emit_runtime_event(enabled=stream_enabled, event="fix", message="Retrying with --mcp-probe off")
                    return _retry_cmd_act_with_overrides(args, mcp_probe="off")
                if selection == 2:
                    bumped_timeout = max(int(args.timeout) + 15, int(args.timeout) * 2)
                    _emit_runtime_event(
                        enabled=stream_enabled,
                        event="fix",
                        message=f"Retrying with increased timeout={bumped_timeout}s",
                    )
                    return _retry_cmd_act_with_overrides(args, timeout=bumped_timeout)
    capabilities["supportedIntents"] = _contextual_supported_intents(
        request,
        source_type=effective_source_type,
        capabilities=capabilities,
    )
    trace: list[str] = [
        f"I understood your request as: '{request}'.",
        f"I selected source '{source_slug}' ({provider}).",
        f"I probed source capabilities: {json.dumps(capabilities)}",
    ]
    for item in trace:
        _emit_runtime_event(enabled=stream_enabled, event="reasoning", message=item)

    plan, raw_plan = _plan_tool_action(
        request,
        provider=provider,
        source_type=effective_source_type,
        capabilities=capabilities,
        model=args.model,
        timeout=args.timeout,
    )
    intent = str(plan.get("intent") or "unknown")
    reasoning = str(plan.get("reasoning") or "")
    params = plan.get("params") if isinstance(plan.get("params"), dict) else {}
    _append_reasoning(trace, (reasoning or f"I classified the request intent as '{intent}'."), stream=stream_enabled)

    if args.dry_run:
        _emit_runtime_event(enabled=stream_enabled, event="step", message="Action dry-run completed")
        _print_json(
            {
                "mode": "agent-act",
                "dryRun": True,
                "source": source_slug,
                "provider": provider,
                "capabilities": capabilities,
                "agentReasoning": trace,
                "plan": {"intent": intent, "params": params, "raw": raw_plan},
            }
        )
        return 0

    try:
        if effective_source_type == "mcp":
            mcp_payload = source.mcp if isinstance(source.mcp, dict) else {}
            tool_name = str(params.get("toolName") or "").strip()
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            if intent != "mcp_call":
                intent = "mcp_call"
                _append_reasoning(trace, "I mapped this request to an MCP tool invocation.", stream=stream_enabled)

            if not tool_name:
                known_tools = capabilities.get("tools") if isinstance(capabilities.get("tools"), list) else []
                if len(known_tools) == 1:
                    tool_name = str(known_tools[0])
                    _append_reasoning(trace, f"I selected the only available MCP tool: {tool_name}", stream=stream_enabled)
                else:
                    if _interactive_fix_enabled(args):
                        selection = _select_interactive_option(
                            "Interactive fix assistant: MCP tool could not be inferred.",
                            [
                                "Retry with live MCP probing",
                                "Retry with cached MCP tools",
                                "Retry with MCP probing off",
                            ],
                        )
                        if selection == 0:
                            return _retry_cmd_act_with_overrides(args, mcp_probe="live")
                        if selection == 1:
                            return _retry_cmd_act_with_overrides(args, mcp_probe="cached")
                        if selection == 2:
                            return _retry_cmd_act_with_overrides(args, mcp_probe="off")
                    _print_json(
                        {
                            "error": "MCP toolName is required but could not be inferred from request.",
                            "agentReasoning": trace,
                            "availableTools": known_tools,
                        },
                        stream=sys.stderr,
                    )
                    return 2

            max_attempts = max(0, int(args.heal_attempts)) + 1
            last_error: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    _append_step(
                        trace,
                        f"Executing MCP tool call (attempt {attempt}/{max_attempts}): {tool_name}",
                        stream=stream_enabled,
                    )
                    result = _mcp_request(
                        workspace_root=workspace,
                        source_slug=source_slug,
                        mcp_cfg=mcp_payload,
                        method="tools/call",
                        params={"name": tool_name, "arguments": arguments},
                        timeout=max(10, int(args.timeout)),
                    )
                    _print_json(
                        {
                            "mode": "agent-act",
                            "source": source_slug,
                            "provider": provider,
                            "sourceType": "mcp",
                            "intent": intent,
                            "agentReasoning": trace,
                            "request": {"toolName": tool_name, "arguments": arguments},
                            "result": result,
                        }
                    )
                    return 0
                except Exception as error:
                    last_error = error
                    _emit_runtime_event(
                        enabled=stream_enabled,
                        event="error",
                        message=f"MCP call failed on attempt {attempt}/{max_attempts}: {error}",
                    )
                    if attempt >= max_attempts:
                        break
                    heal = _self_heal_action_plan(
                        request=request,
                        provider=provider,
                        source_type="mcp",
                        intent=intent,
                        params={"toolName": tool_name, "arguments": arguments},
                        capabilities=capabilities,
                        error_message=str(error),
                        model=args.model,
                        timeout=args.timeout,
                    )
                    heal_reasoning = str(heal.get("reasoning") or "I analyzed the MCP failure and prepared a retry.")
                    _append_reasoning(trace, heal_reasoning, stream=stream_enabled)
                    patch = heal.get("paramsPatch") if isinstance(heal.get("paramsPatch"), dict) else {}
                    if patch:
                        tool_name = str(patch.get("toolName") or tool_name).strip()
                        if isinstance(patch.get("arguments"), dict):
                            arguments = patch.get("arguments")
                        _emit_runtime_event(
                            enabled=stream_enabled,
                            event="self_heal",
                            message="Applied MCP retry patch.",
                            data={"toolName": tool_name},
                        )

            suggestions = _error_fix_suggestions(
                error_text=str(last_error) if last_error else "MCP action failed",
                source_type="mcp",
                provider=provider,
                intent=intent,
            )
            for suggestion in suggestions:
                _emit_runtime_event(enabled=stream_enabled, event="fix", message=suggestion)
            if _maybe_guarded_auto_apply(
                args,
                workspace_root=workspace,
                source_slug=source_slug,
                provider=provider,
                error_text=str(last_error) if last_error else "MCP action failed",
                suggestions=suggestions,
                stream_enabled=stream_enabled,
            ):
                return _retry_cmd_act_with_overrides(args, guarded_auto_apply=False, fix_mode="suggest", fix_dry_run=False)
            if _interactive_fix_enabled(args):
                selection = _select_interactive_option(
                    "Interactive fix assistant: MCP action failed. Try one quick fix:",
                    [
                        "Retry using cached MCP tools (--mcp-probe cached)",
                        "Retry without MCP probing (--mcp-probe off)",
                        "Increase timeout and retry",
                    ],
                )
                if selection == 0:
                    return _retry_cmd_act_with_overrides(args, mcp_probe="cached")
                if selection == 1:
                    return _retry_cmd_act_with_overrides(args, mcp_probe="off")
                if selection == 2:
                    bumped_timeout = max(int(args.timeout) + 15, int(args.timeout) * 2)
                    return _retry_cmd_act_with_overrides(args, timeout=bumped_timeout)
            _print_json(
                {
                    "error": str(last_error) if last_error else "MCP action failed",
                    "agentReasoning": trace,
                    "fixSuggestions": suggestions,
                },
                stream=sys.stderr,
            )
            return 2

        if not isinstance(effective_source_api, dict):
            print("API source is missing api configuration.", file=sys.stderr)
            return 2

        if intent == "list_issues":
            if provider != "linear":
                intent = "api_call"
                params = {
                    "method": "GET",
                    "path": str(params.get("path") or "/issues"),
                    "query": params.get("query") if isinstance(params.get("query"), dict) else {},
                    "body": {},
                    "headers": {},
                }
                _append_reasoning(trace, "I mapped list_issues to a generic API call for this provider.", stream=stream_enabled)
            else:
                _append_reasoning(trace, "I am listing issues from Linear using a GraphQL query.", stream=stream_enabled)
                query = """
                query Issues {
                  issues(first: 20) {
                    nodes {
                      id
                      identifier
                      title
                      description
                      state { name }
                      url
                    }
                  }
                }
                """
                max_attempts = max(0, int(args.heal_attempts)) + 1
                last_error: Exception | None = None
                for attempt in range(1, max_attempts + 1):
                    try:
                        _append_step(trace, f"Executing Linear issue list (attempt {attempt}/{max_attempts})", stream=stream_enabled)
                        result = _linear_graphql(workspace, source_slug, effective_source_api, query, {})
                        issues = (((result.get("data") or {}).get("issues") or {}).get("nodes") or [])
                        _print_json(
                            {
                                "mode": "agent-act",
                                "source": source_slug,
                                "provider": provider,
                                "intent": intent,
                                "agentReasoning": trace,
                                "result": {"count": len(issues), "issues": issues},
                            }
                        )
                        return 0
                    except Exception as error:
                        last_error = error
                        _emit_runtime_event(
                            enabled=stream_enabled,
                            event="error",
                            message=f"Linear list failed on attempt {attempt}/{max_attempts}: {error}",
                        )
                        if attempt >= max_attempts:
                            break
                        heal = _self_heal_action_plan(
                            request=request,
                            provider=provider,
                            source_type="api",
                            intent=intent,
                            params={"query": "Issues", "variables": {}},
                            capabilities=capabilities,
                            error_message=str(error),
                            model=args.model,
                            timeout=args.timeout,
                        )
                        _append_reasoning(
                            trace,
                            str(heal.get("reasoning") or "I analyzed the Linear failure and prepared a retry."),
                            stream=stream_enabled,
                        )

                suggestions = _error_fix_suggestions(
                    error_text=str(last_error) if last_error else "Linear list action failed",
                    source_type="api",
                    provider=provider,
                    intent=intent,
                )
                for suggestion in suggestions:
                    _emit_runtime_event(enabled=stream_enabled, event="fix", message=suggestion)
                if _maybe_guarded_auto_apply(
                    args,
                    workspace_root=workspace,
                    source_slug=source_slug,
                    provider=provider,
                    error_text=str(last_error) if last_error else "Linear list action failed",
                    suggestions=suggestions,
                    stream_enabled=stream_enabled,
                ):
                    return _retry_cmd_act_with_overrides(args, guarded_auto_apply=False, fix_mode="suggest", fix_dry_run=False)
                if _interactive_fix_enabled(args):
                    selection = _select_interactive_option(
                        "Interactive fix assistant: Linear list failed. Try one quick fix:",
                        [
                            "Increase timeout and retry",
                            "Enter/update credential now and retry",
                        ],
                    )
                    if selection == 0:
                        bumped_timeout = max(int(args.timeout) + 15, int(args.timeout) * 2)
                        return _retry_cmd_act_with_overrides(args, timeout=bumped_timeout)
                    if selection == 1 and _source_requires_auth(source):
                        prompted = _prompt_credential_for_source(source)
                        if prompted:
                            _write_cached_credential(workspace, source_slug, prompted, int(getattr(args, "ttl_hours", 24) or 24))
                            return _retry_cmd_act_with_overrides(args)
                _print_json(
                    {
                        "error": str(last_error) if last_error else "Linear list action failed",
                        "agentReasoning": trace,
                        "fixSuggestions": suggestions,
                    },
                    stream=sys.stderr,
                )
                return 2

        if intent == "create_issue":
            if provider != "linear":
                title = str(params.get("title") or "").strip()
                body = params.get("body") if isinstance(params.get("body"), dict) else {}
                if title and "title" not in body:
                    body["title"] = title
                intent = "api_call"
                params = {
                    "method": "POST",
                    "path": str(params.get("path") or "/issues"),
                    "query": params.get("query") if isinstance(params.get("query"), dict) else {},
                    "body": body,
                    "headers": params.get("headers") if isinstance(params.get("headers"), dict) else {},
                }
                trace.append("I mapped create_issue to a generic API call for this provider.")
            else:
                title = str(params.get("title") or "").strip()
                description = str(params.get("description") or "").strip() or None
                team_id = str(params.get("teamId") or "").strip() or None
                team_key = str(params.get("teamKey") or "").strip() or None

                if not title:
                    _print_json(
                        {
                            "error": "Missing issue title. Try: create an issue in linear titled '<title>'",
                            "agentReasoning": trace,
                        },
                        stream=sys.stderr,
                    )
                    return 2

                if not team_id:
                    _append_reasoning(trace, "I need a team id to create an issue, so I attempted to resolve it from available teams.", stream=stream_enabled)
                    team_id, teams = _linear_resolve_team_id(workspace, source_slug, effective_source_api, team_key=team_key)
                    if not team_id:
                        _print_json(
                            {
                                "error": "Could not determine teamId. Include team key in your request, for example: create issue in team ENG ...",
                                "agentReasoning": trace,
                                "availableTeams": teams,
                            },
                            stream=sys.stderr,
                        )
                        return 2

                _append_reasoning(trace, "I am creating the issue in Linear via GraphQL mutation.", stream=stream_enabled)
                mutation = """
                mutation CreateIssue($input: IssueCreateInput!) {
                  issueCreate(input: $input) {
                    success
                    issue {
                      id
                      identifier
                      title
                      url
                    }
                  }
                }
                """
                variables = {"input": {"teamId": team_id, "title": title}}
                if description:
                    variables["input"]["description"] = description

                result = _linear_graphql(workspace, source_slug, effective_source_api, mutation, variables)
                created = (((result.get("data") or {}).get("issueCreate") or {}).get("issue") or {})
                _print_json(
                    {
                        "mode": "agent-act",
                        "source": source_slug,
                        "provider": provider,
                        "intent": intent,
                        "agentReasoning": trace,
                        "result": {"issue": created},
                    }
                )
                return 0

        if intent == "api_call":
            method = str(params.get("method") or "GET").upper().strip()
            path = str(params.get("path") or "").strip()
            query = params.get("query") if isinstance(params.get("query"), dict) else {}
            body = params.get("body") if isinstance(params.get("body"), dict) else {}
            headers = params.get("headers") if isinstance(params.get("headers"), dict) else {}

            path = re.sub(r"^[^/a-zA-Z0-9]+", "", path)
            if path and not path.startswith("/") and not path.lower().startswith("http"):
                path = "/" + path
            if not path:
                path_hint = re.search(r"(/[-a-zA-Z0-9_./]+)", request)
                path = path_hint.group(1) if path_hint else ""
            if not path:
                _print_json(
                    {
                        "error": "Could not determine API path. Include endpoint in your request, for example: list tickets from /api/v2/tickets",
                        "agentReasoning": trace,
                    },
                    stream=sys.stderr,
                )
                return 2

            max_attempts = max(0, int(args.heal_attempts)) + 1
            last_error: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    _append_step(
                        trace,
                        f"I am executing generic API call (attempt {attempt}/{max_attempts}): {method} {path}",
                        stream=stream_enabled,
                    )
                    result = _generic_api_request(
                        workspace_root=workspace,
                        source_slug=source_slug,
                        source_api=effective_source_api,
                        method=method,
                        path=path,
                        query=query,
                        body=body,
                        headers=headers,
                        auth_types_try=requested_auth_types,
                    )
                    _print_json(
                        {
                            "mode": "agent-act",
                            "source": source_slug,
                            "provider": provider,
                            "intent": intent,
                            "agentReasoning": trace,
                            "request": {
                                "method": method,
                                "path": path,
                                "query": query,
                                "body": body,
                                "authTypesTry": requested_auth_types,
                            },
                            "result": result,
                        }
                    )
                    return 0
                except Exception as error:
                    last_error = error
                    _emit_runtime_event(
                        enabled=stream_enabled,
                        event="error",
                        message=f"API call failed on attempt {attempt}/{max_attempts}: {error}",
                    )
                    if attempt >= max_attempts:
                        break
                    heal = _self_heal_action_plan(
                        request=request,
                        provider=provider,
                        source_type="api",
                        intent=intent,
                        params={"method": method, "path": path, "query": query, "body": body, "headers": headers},
                        capabilities=capabilities,
                        error_message=str(error),
                        model=args.model,
                        timeout=args.timeout,
                    )
                    heal_reasoning = str(heal.get("reasoning") or "I analyzed the failure and attempted self-healing.")
                    _append_reasoning(trace, heal_reasoning, stream=stream_enabled)
                    patch = heal.get("paramsPatch") if isinstance(heal.get("paramsPatch"), dict) else {}
                    if patch:
                        method = str(patch.get("method") or method).upper().strip()
                        path = str(patch.get("path") or path).strip()
                        query = patch.get("query") if isinstance(patch.get("query"), dict) else query
                        body = patch.get("body") if isinstance(patch.get("body"), dict) else body
                        headers = patch.get("headers") if isinstance(patch.get("headers"), dict) else headers
                        _emit_runtime_event(
                            enabled=stream_enabled,
                            event="self_heal",
                            message="Applied API retry patch.",
                            data={"method": method, "path": path},
                        )

            suggestions = _error_fix_suggestions(
                error_text=str(last_error) if last_error else "API action failed",
                source_type="api",
                provider=provider,
                intent=intent,
            )
            for suggestion in suggestions:
                _emit_runtime_event(enabled=stream_enabled, event="fix", message=suggestion)
            if _maybe_guarded_auto_apply(
                args,
                workspace_root=workspace,
                source_slug=source_slug,
                provider=provider,
                error_text=str(last_error) if last_error else "API action failed",
                suggestions=suggestions,
                stream_enabled=stream_enabled,
            ):
                return _retry_cmd_act_with_overrides(args, guarded_auto_apply=False, fix_mode="suggest", fix_dry_run=False)
            if _interactive_fix_enabled(args):
                options = [
                    "Increase timeout and retry",
                    "Enter/update credential now and retry",
                ]
                auth_error = _looks_like_auth_error(str(last_error) if last_error else "")
                if auth_error:
                    options.extend(
                        [
                            "Retry with a specific auth type",
                            "Retry trying all auth types",
                        ]
                    )
                selection = _select_interactive_option(
                    "Interactive fix assistant: API action failed. Try one quick fix:",
                    options,
                )
                if selection == 0:
                    bumped_timeout = max(int(args.timeout) + 15, int(args.timeout) * 2)
                    return _retry_cmd_act_with_overrides(args, timeout=bumped_timeout)
                if selection == 1 and _source_requires_auth(source):
                    prompted = _prompt_credential_for_source(source)
                    if prompted:
                        _write_cached_credential(workspace, source_slug, prompted, int(getattr(args, "ttl_hours", 24) or 24))
                        return _retry_cmd_act_with_overrides(args)
                if auth_error and selection == 2:
                    auth_selection = _select_interactive_option(
                        "Select auth type to retry with:",
                        [auth_type for auth_type in API_AUTH_TYPES],
                    )
                    if auth_selection is not None:
                        chosen = API_AUTH_TYPES[auth_selection]
                        return _retry_cmd_act_with_overrides(args, auth_types_try=chosen)
                if auth_error and selection == 3:
                    return _retry_cmd_act_with_overrides(args, auth_types_try=",".join(API_AUTH_TYPES))
            _print_json(
                {
                    "error": str(last_error) if last_error else "API action failed",
                    "agentReasoning": trace,
                    "fixSuggestions": suggestions,
                },
                stream=sys.stderr,
            )
            return 2

        _print_json(
            {
                "error": f"Unsupported intent: {intent}",
                "agentReasoning": trace,
                "hint": "Try: 'list issues in linear', include an endpoint path for API tools, or specify an MCP tool in the request.",
            },
            stream=sys.stderr,
        )
        return 2
    except Exception as error:
        _emit_runtime_event(enabled=stream_enabled, event="error", message=str(error))
        suggestions = _error_fix_suggestions(
            error_text=str(error),
            source_type=str(source.type) if source is not None else "unknown",
            provider=provider if 'provider' in locals() else "custom",
            intent=intent if 'intent' in locals() else "unknown",
        )
        for suggestion in suggestions:
            _emit_runtime_event(enabled=stream_enabled, event="fix", message=suggestion)
        if (
            source is not None
            and 'source_slug' in locals()
            and _maybe_guarded_auto_apply(
                args,
                workspace_root=workspace,
                source_slug=source_slug,
                provider=provider if 'provider' in locals() else "custom",
                error_text=str(error),
                suggestions=suggestions,
                stream_enabled=stream_enabled,
            )
        ):
            return _retry_cmd_act_with_overrides(args, guarded_auto_apply=False, fix_mode="suggest", fix_dry_run=False)
        _print_json({"error": str(error), "agentReasoning": trace, "fixSuggestions": suggestions}, stream=sys.stderr)
        return 2


def cmd_chat(args: argparse.Namespace) -> int:
    prompt = str(args.prompt or "").strip()
    if not prompt:
        print("Prompt is required", file=sys.stderr)
        return 2

    stream_enabled = bool(getattr(args, "stream", False))
    _emit_runtime_event(enabled=stream_enabled, event="step", message=f"Chat request received: {prompt}")

    if bool(getattr(args, "cli", False)):
        pass

    if re.search(r"\bconnect\s+to\b", prompt, flags=re.IGNORECASE):
        connect_args = argparse.Namespace(
            workspace=args.workspace,
            request=prompt,
            dry_run=bool(args.dry_run),
            provider_type=args.provider_type,
            base_url=args.base_url,
            auth_type=args.auth_type,
            auto_auth=args.auto_auth,
            auth_value=args.auth_value,
            ttl_hours=args.ttl_hours,
            mark_authenticated=args.mark_authenticated,
            model=args.model,
            timeout=args.timeout,
            show_reasoning=True,
            stream=stream_enabled,
        )
        return cmd_connect(connect_args)

    act_args = argparse.Namespace(
        workspace=args.workspace,
        request=prompt,
        source=args.source,
        model=args.model,
        timeout=args.timeout,
        heal_attempts=args.heal_attempts,
        dry_run=bool(args.dry_run),
        stream=stream_enabled,
        mcp_probe=getattr(args, "mcp_probe", "live"),
        interactive_fix=getattr(args, "interactive_fix", True),
        api_fallback_on_mcp_failure=getattr(args, "api_fallback_on_mcp_failure", True),
        guarded_auto_apply=getattr(args, "guarded_auto_apply", False),
        fix_mode=getattr(args, "fix_mode", "suggest"),
        fix_scope=getattr(args, "fix_scope", "runtime"),
        allow_code_patch=getattr(args, "allow_code_patch", False),
        fix_dry_run=getattr(args, "fix_dry_run", False),
        auth_types_try=getattr(args, "auth_types_try", None),
    )
    return cmd_act(act_args)


def _add_completion_args(target: argparse.ArgumentParser) -> None:
    target.add_argument("--model", help="Model name for completion (default: AGENT_COPILOT_MODEL or gpt-5.3-codex)")
    target.add_argument("--temperature", type=float, help="Sampling temperature")
    target.add_argument("--max-tokens", type=int, help="Maximum completion tokens")
    target.add_argument("--reasoning-effort", choices=["low", "medium", "high"], help="Reasoning effort level")
    target.add_argument("--thinking-budget", type=int, help="Thinking token budget for providers that support it")
    target.add_argument("--system", help="Override default system prompt")
    target.add_argument("--timeout", type=int, default=60, help="Request timeout in seconds")
    target.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Stream progress and model output events to stderr in real time.",
    )
    target.add_argument("--dry-run", action="store_true", help="Print request payload without calling model endpoint")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentctl", description="Agent backend CLI with Copilot-style commands")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_workspace_arg(target: argparse.ArgumentParser) -> None:
        target.add_argument("--workspace", default="~/.agent-runtime/workspaces/default", help="Workspace root path")

    list_parser = subparsers.add_parser("list", help="List sources")
    add_workspace_arg(list_parser)
    list_parser.set_defaults(func=cmd_list)

    get_parser = subparsers.add_parser("get", help="Get source by slug")
    add_workspace_arg(get_parser)
    get_parser.add_argument("slug", help="Source slug")
    get_parser.set_defaults(func=cmd_get)

    create_parser = subparsers.add_parser("create", help="Create source")
    add_workspace_arg(create_parser)
    create_parser.add_argument("--name", required=True, help="Display name")
    create_parser.add_argument("--type", required=True, choices=["mcp", "api", "local"], help="Source type")
    create_parser.add_argument("--provider", help="Provider id")
    create_parser.add_argument("--disabled", action="store_true", help="Create as disabled")
    create_parser.add_argument("--icon", help="Emoji or icon URL")
    create_parser.add_argument("--mcp", help="MCP config JSON object")
    create_parser.add_argument("--api", help="API config JSON object")
    create_parser.add_argument("--local", help="Local config JSON object")
    create_parser.set_defaults(func=cmd_create)

    delete_parser = subparsers.add_parser("delete", help="Delete source")
    add_workspace_arg(delete_parser)
    delete_parser.add_argument("slug", help="Source slug")
    delete_parser.set_defaults(func=cmd_delete)

    auth_parser = subparsers.add_parser("mark-authenticated", help="Mark source as authenticated")
    add_workspace_arg(auth_parser)
    auth_parser.add_argument("slug", help="Source slug")
    auth_parser.set_defaults(func=cmd_mark_authenticated)

    doctor_parser = subparsers.add_parser("doctor", help="Show backend path diagnostics")
    add_workspace_arg(doctor_parser)
    doctor_parser.set_defaults(func=cmd_doctor)

    connect_parser = subparsers.add_parser("connect", help="Create a source from a plain-English request")
    add_workspace_arg(connect_parser)
    connect_parser.add_argument("request", help='Plain request, for example: "connect to linear"')
    connect_parser.add_argument("--dry-run", action="store_true", help="Show inferred connection plan without creating source")
    connect_parser.add_argument("--provider-type", choices=["auto", "api", "mcp"], default="auto", help="Provider type hint for discovery")
    connect_parser.add_argument("--base-url", help="Optional explicit API base URL override")
    connect_parser.add_argument("--auth-type", choices=["bearer", "basic", "header", "query", "none"], help="Optional explicit auth type override")
    connect_parser.add_argument(
        "--auto-auth",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Prompt for credentials immediately after connect when authentication is required.",
    )
    connect_parser.add_argument("--auth-value", help="Credential value for auto-auth onboarding (non-interactive use)")
    connect_parser.add_argument("--ttl-hours", type=int, default=24, help="Credential TTL in hours for auto-auth flow")
    connect_parser.add_argument(
        "--mark-authenticated",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Mark source authenticated after successful auto-auth credential capture.",
    )
    connect_parser.add_argument("--model", help="Discovery model (default: AGENT_COPILOT_MODEL or gpt-5.3-codex)")
    connect_parser.add_argument("--timeout", type=int, default=60, help="Discovery timeout in seconds")
    connect_parser.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Stream connection reasoning, steps, and errors to stderr in real time.",
    )
    connect_parser.add_argument(
        "--show-reasoning",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Show step-by-step decision log while processing connection",
    )
    connect_parser.set_defaults(func=cmd_connect)

    credential_root = subparsers.add_parser("credential", help="Manage cached source credentials")
    credential_sub = credential_root.add_subparsers(dest="credential_command", required=True)

    credential_set = credential_sub.add_parser("set", help="Set credential for a source")
    add_workspace_arg(credential_set)
    credential_set.add_argument("--source", required=True, help="Source slug")
    credential_set.add_argument("--value", help="Credential value (token or key). If omitted, prompt interactively.")
    credential_set.add_argument("--ttl-hours", type=int, default=24, help="Credential TTL in hours (default: 24)")
    credential_set.add_argument(
        "--mark-authenticated",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Mark source as authenticated after storing credential (interactive prompt if omitted in TTY).",
    )
    credential_set.add_argument(
        "--show-guide",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Show provider-specific authentication guide before prompting for credential.",
    )
    credential_set.set_defaults(func=cmd_credential_set)

    credential_status = credential_sub.add_parser("status", help="Check whether a credential exists for a source")
    add_workspace_arg(credential_status)
    credential_status.add_argument("--source", required=True, help="Source slug")
    credential_status.set_defaults(func=cmd_credential_status)

    act_parser = subparsers.add_parser("act", help="Execute real tool actions from plain-English requests")
    add_workspace_arg(act_parser)
    act_parser.add_argument("request", help='Action request, for example: "list all issues in linear"')
    act_parser.add_argument("--source", help="Optional source slug override (auto-resolved from request when omitted)")
    act_parser.add_argument("--model", help="Planner model (default: AGENT_COPILOT_MODEL or gpt-5.3-codex)")
    act_parser.add_argument("--timeout", type=int, default=60, help="Planner timeout in seconds")
    act_parser.add_argument("--heal-attempts", type=int, default=2, help="Number of self-healing retries after initial failure")
    act_parser.add_argument(
        "--mcp-probe",
        choices=["live", "cached", "off"],
        default="live",
        help="MCP capability probe mode: live=call tools/list, cached=use mcp.tools from config, off=skip probing.",
    )
    act_parser.add_argument(
        "--api-fallback-on-mcp-failure",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="If MCP probing fails, automatically switch to API mode when an API source/provider fallback is available.",
    )
    act_parser.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Stream action steps, errors, and self-healing reasoning to stderr in real time.",
    )
    act_parser.add_argument(
        "--interactive-fix",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When a failure happens, open an interactive fix assistant and optionally retry automatically.",
    )
    act_parser.add_argument(
        "--guarded-auto-apply",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Preview safe fix patch diffs and apply only after explicit confirmation, then retry automatically.",
    )
    act_parser.add_argument(
        "--fix-mode",
        choices=["suggest", "guarded", "auto"],
        default="suggest",
        help="Failure remediation mode: suggest only, guarded confirm+apply, or automatic apply for supported fixes.",
    )
    act_parser.add_argument(
        "--fix-scope",
        choices=["runtime", "config", "code", "all"],
        default="runtime",
        help="Scope of allowed auto-fixes.",
    )
    act_parser.add_argument(
        "--allow-code-patch",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Allow code-file patch fixes when a supported handler matches (example: timeout default patch).",
    )
    act_parser.add_argument(
        "--fix-dry-run",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Show proposed fix patches without applying them.",
    )
    act_parser.add_argument(
        "--auth-types-try",
        help="Comma-separated API auth types to try on auth failures (choices: bearer,basic,header,query,none).",
    )
    act_parser.add_argument("--dry-run", action="store_true", help="Show agent plan/reasoning without executing action")
    act_parser.set_defaults(func=cmd_act)

    chat_parser = subparsers.add_parser("chat", help="Chat-first orchestration for connect and actions")
    add_workspace_arg(chat_parser)
    chat_parser.add_argument("prompt", help="Natural language request")
    chat_parser.add_argument("--cli", action="store_true", help="Use consolidated CLI orchestration mode")
    chat_parser.add_argument("--source", help="Optional source slug override for action requests")
    chat_parser.add_argument("--provider-type", choices=["auto", "api", "mcp"], default="auto", help="Type hint for connect discovery")
    chat_parser.add_argument("--base-url", help="Optional base URL override for connect requests")
    chat_parser.add_argument("--auth-type", choices=["bearer", "basic", "header", "query", "none"], help="Optional auth type override for connect requests")
    chat_parser.add_argument(
        "--auto-auth",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Prompt for credentials immediately after connect requests when authentication is required.",
    )
    chat_parser.add_argument("--auth-value", help="Credential value for connect auto-auth onboarding (non-interactive use)")
    chat_parser.add_argument("--ttl-hours", type=int, default=24, help="Credential TTL in hours for connect auto-auth")
    chat_parser.add_argument(
        "--mark-authenticated",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Mark source authenticated after successful connect auto-auth credential capture.",
    )
    chat_parser.add_argument("--model", help="Planner/discovery model")
    chat_parser.add_argument("--timeout", type=int, default=60, help="Planner/discovery timeout in seconds")
    chat_parser.add_argument("--heal-attempts", type=int, default=2, help="Number of self-healing retries for action execution")
    chat_parser.add_argument(
        "--mcp-probe",
        choices=["live", "cached", "off"],
        default="live",
        help="MCP capability probe mode for action requests.",
    )
    chat_parser.add_argument(
        "--api-fallback-on-mcp-failure",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="If MCP probing fails, automatically switch to API mode when an API source/provider fallback is available.",
    )
    chat_parser.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Stream orchestration steps, errors, and self-healing reasoning to stderr in real time.",
    )
    chat_parser.add_argument(
        "--interactive-fix",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When a failure happens, open an interactive fix assistant and optionally retry automatically.",
    )
    chat_parser.add_argument(
        "--guarded-auto-apply",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Preview safe fix patch diffs and apply only after explicit confirmation, then retry automatically.",
    )
    chat_parser.add_argument(
        "--fix-mode",
        choices=["suggest", "guarded", "auto"],
        default="suggest",
        help="Failure remediation mode: suggest only, guarded confirm+apply, or automatic apply for supported fixes.",
    )
    chat_parser.add_argument(
        "--fix-scope",
        choices=["runtime", "config", "code", "all"],
        default="runtime",
        help="Scope of allowed auto-fixes.",
    )
    chat_parser.add_argument(
        "--allow-code-patch",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Allow code-file patch fixes when a supported handler matches (example: timeout default patch).",
    )
    chat_parser.add_argument(
        "--fix-dry-run",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Show proposed fix patches without applying them.",
    )
    chat_parser.add_argument(
        "--auth-types-try",
        help="Comma-separated API auth types to try on auth failures (choices: bearer,basic,header,query,none).",
    )
    chat_parser.add_argument("--dry-run", action="store_true", help="Plan-only mode without writing/executing changes")
    chat_parser.set_defaults(func=cmd_chat)

    auth_root = subparsers.add_parser("auth", help="Authentication commands")
    auth_sub = auth_root.add_subparsers(dest="auth_command", required=True)

    auth_login = auth_sub.add_parser("login", help="Authenticate with provider")
    auth_login.add_argument("--provider", default="github-copilot", choices=["github-copilot"])
    auth_login.add_argument("--token", help="GitHub token to store for Copilot auth")
    auth_login.add_argument("--from-gh", action="store_true", help="Use `gh auth token` when token is not explicitly provided")
    auth_login.add_argument("--no-validate", action="store_true", help="Skip GitHub API token validation")
    auth_login.set_defaults(func=cmd_auth_login)

    auth_status = auth_sub.add_parser("status", help="Show auth status")
    auth_status.set_defaults(func=cmd_auth_status)

    auth_guide = auth_sub.add_parser("guide", help="Show authentication flow and docs for a source")
    add_workspace_arg(auth_guide)
    auth_guide.add_argument("--source", required=True, help="Source slug")
    auth_guide.add_argument("--pretty", action="store_true", help="Print a human-friendly guide in addition to JSON")
    auth_guide.set_defaults(func=cmd_auth_guide)

    auth_logout = auth_sub.add_parser("logout", help="Clear saved auth session")
    auth_logout.set_defaults(func=cmd_auth_logout)

    copilot_root = subparsers.add_parser("copilot", help="Copilot-style CLI commands")
    copilot_sub = copilot_root.add_subparsers(dest="copilot_command", required=True)

    copilot_suggest = copilot_sub.add_parser("suggest", help="Suggest a shell command")
    copilot_suggest.add_argument("prompt", help="Prompt to generate command suggestion from")
    copilot_suggest.add_argument("--shell", default="bash", choices=["bash", "zsh", "pwsh"], help="Target shell")
    _add_completion_args(copilot_suggest)
    copilot_suggest.set_defaults(func=cmd_copilot_suggest)

    copilot_explain = copilot_sub.add_parser("explain", help="Explain a shell command")
    copilot_explain.add_argument("--command", required=True, help="Command to explain (quote full command)")
    _add_completion_args(copilot_explain)
    copilot_explain.set_defaults(func=cmd_copilot_explain)

    copilot_chat = copilot_sub.add_parser("chat", help="Copilot-style chat scaffold")
    copilot_chat.add_argument("prompt", help="Prompt to send")
    _add_completion_args(copilot_chat)
    copilot_chat.set_defaults(func=cmd_copilot_chat)

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
