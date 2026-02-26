from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .mcp_stdio import ToolDef, run_stdio_server, send_log
from .storage import load_source_config


def _callback(payload: dict[str, Any]) -> None:
    print(f"__CALLBACK__{json.dumps(payload)}", flush=True)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50]
    return slug or "source"


def _write_guide(workspace_root: Path, slug: str, name: str, tagline: str | None = None) -> None:
    guide_path = workspace_root / "sources" / slug / "guide.md"
    tagline_line = f"{tagline}\n\n" if tagline else ""
    guide = (
        f"# {name}\n\n"
        f"{tagline_line}"
        "## Guidelines\n\n"
        "(Add usage guidelines here)\n\n"
        "## Context\n\n"
        "(Add context about this source)\n"
    )
    guide_path.parent.mkdir(parents=True, exist_ok=True)
    guide_path.write_text(guide, encoding="utf-8")


def _resolve_unique_slug(workspace_root: Path, preferred_slug: str) -> str:
    base = _slugify(preferred_slug)
    sources_dir = workspace_root / "sources"
    existing = {entry.name for entry in sources_dir.iterdir() if entry.is_dir()} if sources_dir.exists() else set()
    if base not in existing:
        return base
    counter = 2
    while f"{base}-{counter}" in existing:
        counter += 1
    return f"{base}-{counter}"


def _validate_json_file_has_fields(path: Path, required_fields: list[str]) -> tuple[bool, list[str]]:
    if not path.exists():
        return (False, [f"File not found: {path}"])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        return (False, [f"Invalid JSON: {error}"])
    if not isinstance(payload, dict):
        return (False, ["JSON root must be an object"])
    missing = [field for field in required_fields if field not in payload]
    return (len(missing) == 0, [f"Missing required field: {name}" for name in missing])


def _format_validation_result(valid: bool, errors: list[str]) -> str:
    if valid:
        return "✓ Validation passed"
    lines = ["✗ Validation failed:"]
    lines.extend([f"  - {error}" for error in errors])
    return "\n".join(lines)


def _source_requires_auth(source: Any) -> bool:
    if source.type == "mcp":
        return bool(source.mcp and source.mcp.get("authType") == "oauth")
    if source.type == "api":
        api_auth = source.api.get("authType") if isinstance(source.api, dict) else None
        return api_auth is not None and api_auth != "none"
    return False


def _is_likely_emoji(value: str) -> bool:
    if not value:
        return False
    return bool(re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", value))


def _read_cached_credential(workspace_root: Path, source_slug: str) -> str | None:
    cache_path = workspace_root / "sources" / source_slug / ".credential-cache.json"
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        expires_at = payload.get("expiresAt")
        if isinstance(expires_at, (int, float)) and expires_at < time.time() * 1000:
            return None
        value = payload.get("value")
        return str(value) if value is not None else None
    except Exception:
        return None


def _build_api_headers(api: dict[str, Any], credential: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    auth_type = str(api.get("authType") or "")
    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {credential}"
    elif auth_type == "basic":
        headers["Authorization"] = f"Basic {credential}"
    elif auth_type == "header":
        header_name = api.get("headerName")
        header_names = api.get("headerNames")
        if isinstance(header_names, list) and header_names:
            try:
                parsed = json.loads(credential)
                if isinstance(parsed, dict):
                    for name in header_names:
                        if name in parsed and parsed[name] is not None:
                            headers[str(name)] = str(parsed[name])
            except Exception:
                first = str(header_names[0])
                headers[first] = credential
        elif header_name:
            headers[str(header_name)] = credential
        else:
            headers["X-API-Key"] = credential
    return headers


def _source_connection_test(source: Any, workspace_root: Path, source_slug: str) -> tuple[list[str], bool, bool, str | None]:
    lines: list[str] = []
    success = False
    has_error = False
    error: str | None = None

    if source.type == "api":
        api = source.api if isinstance(source.api, dict) else None
        if not api or not api.get("baseUrl"):
            lines.append("✗ No API base URL configured")
            return (lines, False, True, "No base URL")
        test_url = str(api.get("baseUrl"))
        credential = _read_cached_credential(workspace_root, source_slug)
        headers = _build_api_headers(api, credential) if credential and str(api.get("authType")) != "none" else {}
        if credential and headers:
            lines.append("ℹ Using cached credentials for connection test")
        try:
            request = Request(test_url, method="HEAD", headers=headers)
            with urlopen(request, timeout=10) as response:
                status = getattr(response, "status", 200)
            lines.append(f"✓ API endpoint reachable ({test_url})")
            lines.append(f"  Status: {status}")
            success = True
        except HTTPError as http_error:
            status = http_error.code
            if status in {401, 403}:
                lines.append(f"⚠ API returned {status} (authentication required)")
                if source.isAuthenticated and not credential:
                    lines.append("  Source is marked authenticated but credentials could not be retrieved")
                success = True
            elif status == 404:
                lines.append("⚠ API returned 404 (endpoint not found)")
            else:
                lines.append(f"⚠ API returned {status}")
        except Exception as caught:
            error = str(caught)
            lines.append(f"✗ Connection failed: {error}")
            has_error = True
    elif source.type == "mcp":
        mcp = source.mcp if isinstance(source.mcp, dict) else None
        transport = mcp.get("transport") if isinstance(mcp, dict) else None
        if transport == "stdio":
            command = mcp.get("command") if isinstance(mcp, dict) else None
            if command:
                lines.append(f"ℹ Stdio MCP source: {command}")
                lines.append("  Connection test not available in this context — call the source's MCP tools directly to verify")
                success = True
            else:
                lines.append("✗ No command configured for stdio MCP source")
                has_error = True
                error = "No command configured"
        elif isinstance(mcp, dict) and mcp.get("url"):
            lines.append(f"ℹ MCP source URL: {mcp.get('url')}")
            lines.append("  Connection test not available in this context — call the source's MCP tools directly to verify")
            success = True
        else:
            lines.append("✗ No MCP URL or command configured")
            has_error = True
            error = "No MCP URL or command configured"
    elif source.type == "local":
        local = source.local if isinstance(source.local, dict) else None
        local_path = str(local.get("path", "")) if local else ""
        if not local_path:
            lines.append("✗ No local path configured")
            has_error = True
            error = "No local path configured"
        else:
            path_obj = Path(local_path).expanduser()
            if path_obj.exists():
                lines.append(f"✓ Local path exists: {path_obj}")
                lines.append(f"  Type: {'Directory' if path_obj.is_dir() else 'File'}")
                success = True
            else:
                lines.append(f"✗ Local path not found: {path_obj}")
                lines.append("  Verify the path exists and is accessible")
                has_error = True
                error = "Path not found"
    else:
        lines.append("ℹ No connection test available for this source type")
        success = True

    return (lines, success, has_error, error)


def _infer_microsoft_service(base_url: str | None) -> str | None:
    if not base_url:
        return None
    url = base_url.lower()
    if "outlook" in url or "graph.microsoft.com/v1.0/me/messages" in url:
        return "outlook"
    if "calendar" in url:
        return "microsoft-calendar"
    if "onedrive" in url or "drive" in url:
        return "onedrive"
    if "teams" in url:
        return "teams"
    if "sharepoint" in url:
        return "sharepoint"
    return None


class SessionServer:
    def __init__(self, session_id: str, workspace_root: Path, plans_folder: Path, callback_port: str | None):
        self.session_id = session_id
        self.workspace_root = workspace_root
        self.plans_folder = plans_folder
        self.callback_port = callback_port

    def tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="SubmitPlan",
                description="Submit a plan for user review and pause execution.",
                input_schema={
                    "type": "object",
                    "properties": {"planPath": {"type": "string"}},
                    "required": ["planPath"],
                },
            ),
            ToolDef(
                name="config_validate",
                description="Validate agent configuration files.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "target": {
                            "type": "string",
                            "enum": ["config", "sources", "statuses", "preferences", "permissions", "hooks", "tool-icons", "all"],
                        },
                        "sourceSlug": {"type": "string"},
                    },
                    "required": ["target"],
                },
            ),
            ToolDef(
                name="skill_validate",
                description="Validate a skill SKILL.md file.",
                input_schema={
                    "type": "object",
                    "properties": {"skillSlug": {"type": "string"}},
                    "required": ["skillSlug"],
                },
            ),
            ToolDef(
                name="mermaid_validate",
                description="Validate Mermaid diagram syntax.",
                input_schema={
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
            ),
            ToolDef(
                name="source_create",
                description="Create a source folder with config.json and guide.md.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string", "enum": ["mcp", "api", "local"]},
                        "provider": {"type": "string"},
                        "slug": {"type": "string"},
                        "enabled": {"type": "boolean"},
                        "tagline": {"type": "string"},
                        "icon": {"type": "string"},
                        "mcp": {"type": "object"},
                        "api": {"type": "object"},
                        "local": {"type": "object"},
                    },
                    "required": ["name", "type"],
                },
            ),
            ToolDef("source_oauth_trigger", "Trigger OAuth auth for MCP source.", {"type": "object", "properties": {"sourceSlug": {"type": "string"}}, "required": ["sourceSlug"]}),
            ToolDef("source_google_oauth_trigger", "Trigger Google OAuth auth for source.", {"type": "object", "properties": {"sourceSlug": {"type": "string"}}, "required": ["sourceSlug"]}),
            ToolDef("source_slack_oauth_trigger", "Trigger Slack OAuth auth for source.", {"type": "object", "properties": {"sourceSlug": {"type": "string"}}, "required": ["sourceSlug"]}),
            ToolDef("source_microsoft_oauth_trigger", "Trigger Microsoft OAuth auth for source.", {"type": "object", "properties": {"sourceSlug": {"type": "string"}}, "required": ["sourceSlug"]}),
            ToolDef(
                name="source_credential_prompt",
                description="Prompt the user for credentials and pause execution.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "sourceSlug": {"type": "string"},
                        "mode": {"type": "string", "enum": ["bearer", "basic", "header", "query", "multi-header"]},
                        "labels": {"type": "object"},
                        "description": {"type": "string"},
                        "hint": {"type": "string"},
                        "headerNames": {"type": "array", "items": {"type": "string"}},
                        "passwordRequired": {"type": "boolean"},
                    },
                    "required": ["sourceSlug", "mode"],
                },
            ),
            ToolDef("source_test", "Validate and test source configuration.", {"type": "object", "properties": {"sourceSlug": {"type": "string"}}, "required": ["sourceSlug"]}),
            ToolDef(
                name="call_llm",
                description="Return precomputed secondary LLM response when provided.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "attachments": {"type": "array"},
                        "model": {"type": "string"},
                        "systemPrompt": {"type": "string"},
                        "maxTokens": {"type": "number"},
                        "temperature": {"type": "number"},
                        "outputFormat": {"type": "string", "enum": ["summary", "classification", "extraction", "analysis", "comparison", "validation"]},
                        "outputSchema": {"type": "object"},
                    },
                    "required": ["prompt"],
                },
            ),
        ]

    def call_tool(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        if name == "SubmitPlan":
            plan_path = str(args.get("planPath", ""))
            if not plan_path:
                return ("Plan file not found at . Please write the plan file first using the Write tool.", True)
            plan_file = Path(plan_path).expanduser()
            if not plan_file.exists():
                return (f"Plan file not found at {plan_path}. Please write the plan file first using the Write tool.", True)
            try:
                _ = plan_file.read_text(encoding="utf-8")
            except Exception as error:
                return (f"Failed to read plan file: {error}", True)
            _callback({"__callback__": "plan_submitted", "sessionId": self.session_id, "planPath": plan_path})
            return ("Plan submitted for review. Waiting for user feedback.", False)

        if name == "config_validate":
            target = str(args.get("target", ""))
            source_slug = str(args.get("sourceSlug", "")).strip() or None
            runtime_root = Path.home() / ".agent-runtime"

            if target == "config":
                valid, errors = _validate_json_file_has_fields(runtime_root / "config.json", ["workspaces"])
                return (_format_validation_result(valid, errors), False)

            if target == "sources":
                if source_slug:
                    valid, errors = _validate_json_file_has_fields(
                        self.workspace_root / "sources" / source_slug / "config.json",
                        ["slug", "name", "type"],
                    )
                    return (_format_validation_result(valid, errors), False)
                sources_dir = self.workspace_root / "sources"
                if not sources_dir.exists():
                    return ("✓ No sources directory (no sources to validate)", False)
                all_errors: list[str] = []
                for entry in sorted(sources_dir.iterdir(), key=lambda item: item.name):
                    if not entry.is_dir():
                        continue
                    valid, errors = _validate_json_file_has_fields(entry / "config.json", ["slug", "name", "type"])
                    if not valid:
                        all_errors.extend([f"{entry.name}/{error}" for error in errors])
                return (_format_validation_result(len(all_errors) == 0, all_errors), False)

            if target == "statuses":
                valid, errors = _validate_json_file_has_fields(self.workspace_root / "statuses" / "config.json", ["statuses"])
                return (_format_validation_result(valid, errors), False)

            if target == "preferences":
                valid, errors = _validate_json_file_has_fields(runtime_root / "preferences.json", [])
                return (_format_validation_result(valid, errors), False)

            if target == "permissions":
                permissions_path = self.workspace_root / "permissions.json"
                if not permissions_path.exists():
                    return ("✓ No workspace permissions.json (using defaults)", False)
                valid, errors = _validate_json_file_has_fields(permissions_path, [])
                return (_format_validation_result(valid, errors), False)

            if target == "hooks":
                hooks_path = self.workspace_root / "hooks.json"
                if not hooks_path.exists():
                    return ("✓ No hooks.json (no hooks configured)", False)
                valid, errors = _validate_json_file_has_fields(hooks_path, ["matchers"])
                return (_format_validation_result(valid, errors), False)

            if target == "tool-icons":
                valid, errors = _validate_json_file_has_fields(runtime_root / "tool-icons" / "tool-icons.json", ["version", "tools"])
                return (_format_validation_result(valid, errors), False)

            if target == "all":
                valid_config, errors_config = _validate_json_file_has_fields(runtime_root / "config.json", ["workspaces"])
                valid_pref, errors_pref = _validate_json_file_has_fields(runtime_root / "preferences.json", [])
                merged_errors = [*errors_config, *errors_pref]
                return (_format_validation_result(valid_config and valid_pref, merged_errors), False)

            return (
                "Unknown validation target: "
                f"{target}. Valid targets: config, sources, statuses, preferences, permissions, hooks, tool-icons, all",
                True,
            )

        if name == "skill_validate":
            skill_slug = str(args.get("skillSlug", ""))
            skill_file = self.workspace_root / "skills" / skill_slug / "SKILL.md"
            if not skill_file.exists():
                return (f"SKILL.md not found at {skill_file}. Create it with YAML frontmatter.", True)
            return ("✓ Validation passed", False)

        if name == "mermaid_validate":
            code = str(args.get("code", "")).strip()
            if not code:
                return ("Mermaid code is required", True)
            return ("✓ Mermaid syntax appears valid", False)

        if name == "source_create":
            source_name = str(args.get("name", "")).strip()
            source_type = str(args.get("type", "")).strip()
            if not source_name:
                return ("Source name is required.", True)
            if source_type not in {"mcp", "api", "local"}:
                return (f"Invalid source type: {source_type}", True)

            mcp_cfg = args.get("mcp") if isinstance(args.get("mcp"), dict) else None
            api_cfg = args.get("api") if isinstance(args.get("api"), dict) else None
            local_cfg = args.get("local") if isinstance(args.get("local"), dict) else None

            if source_type == "mcp":
                if not mcp_cfg:
                    return ("MCP source requires an mcp configuration object.", True)
                transport = str(mcp_cfg.get("transport", "http"))
                if transport == "stdio" and not mcp_cfg.get("command"):
                    return ("MCP stdio source requires mcp.command.", True)
                if transport in {"http", "sse"} and not mcp_cfg.get("url"):
                    return ("MCP HTTP/SSE source requires mcp.url.", True)
            if source_type == "api":
                if not api_cfg:
                    return ("API source requires an api configuration object.", True)
                if not api_cfg.get("baseUrl"):
                    return ("API source requires api.baseUrl.", True)
                if not api_cfg.get("authType"):
                    return ("API source requires api.authType.", True)
            if source_type == "local":
                if not local_cfg:
                    return ("Local source requires a local configuration object.", True)
                if not local_cfg.get("path"):
                    return ("Local source requires local.path.", True)

            requested_slug = args.get("slug")
            preferred_slug = str(requested_slug) if isinstance(requested_slug, str) and requested_slug else source_name
            target_slug = _resolve_unique_slug(self.workspace_root, preferred_slug)

            source_dir = self.workspace_root / "sources" / target_slug
            source_dir.mkdir(parents=True, exist_ok=True)
            config_path = source_dir / "config.json"
            guide_path = source_dir / "guide.md"

            now = int(time.time() * 1000)
            config_payload: dict[str, Any] = {
                "id": f"{target_slug}_{uuid.uuid4().hex[:8]}",
                "name": source_name,
                "slug": target_slug,
                "enabled": bool(args.get("enabled", True)),
                "provider": str(args.get("provider") or "custom"),
                "type": source_type,
                "createdAt": now,
                "updatedAt": now,
            }
            if isinstance(args.get("tagline"), str) and args.get("tagline"):
                config_payload["tagline"] = str(args.get("tagline"))
            if isinstance(args.get("icon"), str) and args.get("icon"):
                config_payload["icon"] = str(args.get("icon"))
            if source_type == "mcp":
                config_payload["mcp"] = mcp_cfg
            if source_type == "api":
                config_payload["api"] = api_cfg
            if source_type == "local":
                config_payload["local"] = local_cfg

            config_path.write_text(json.dumps(config_payload, indent=2) + "\n", encoding="utf-8")
            if not guide_path.exists():
                _write_guide(self.workspace_root, target_slug, source_name)

            return (
                f"Created source '{target_slug}' ({source_type}).\n\n"
                f"Files created:\n"
                f"- {config_path}\n"
                f"- {guide_path}\n\n"
                f"Next: run source_test with sourceSlug='{target_slug}'.",
                False,
            )

        if name in {
            "source_oauth_trigger",
            "source_google_oauth_trigger",
            "source_slack_oauth_trigger",
            "source_microsoft_oauth_trigger",
        }:
            source_slug = str(args.get("sourceSlug", ""))
            source = load_source_config(self.workspace_root, source_slug)
            if source is None:
                return (f"Source '{source_slug}' not found.", True)

            if name == "source_oauth_trigger":
                if source.type != "mcp":
                    return (f"Source '{source_slug}' is not an MCP source. OAuth is only for MCP sources.", True)
                if not (isinstance(source.mcp, dict) and source.mcp.get("authType") == "oauth"):
                    return (f"Source '{source_slug}' does not use OAuth authentication.", False)
            if name == "source_google_oauth_trigger" and source.provider != "google":
                hint = (
                    'Add "provider": "google" to config.json and retry.'
                    if not source.provider
                    else f"This source has provider '{source.provider}'. Use source_oauth_trigger for MCP sources."
                )
                return (f"Source '{source_slug}' is not configured as a Google API source. {hint}", True)
            if name == "source_slack_oauth_trigger":
                if source.provider != "slack":
                    hint = (
                        'Add "provider": "slack" to config.json and retry.'
                        if not source.provider
                        else f"This source has provider '{source.provider}'."
                    )
                    return (f"Source '{source_slug}' is not configured as a Slack API source. {hint}", True)
                if source.type != "api":
                    extra = (
                        ' For Slack integration, use the native Slack API approach (type: "api", provider: "slack") instead of an MCP server. This enables proper OAuth authentication via source_slack_oauth_trigger.'
                        if source.type == "mcp"
                        else ""
                    )
                    return (
                        f"source_slack_oauth_trigger only works with API sources (type: \"api\"), not {source.type} sources.{extra}",
                        True,
                    )
            if name == "source_microsoft_oauth_trigger" and source.provider != "microsoft":
                hint = (
                    'Add "provider": "microsoft" to config.json and retry.'
                    if not source.provider
                    else f"This source has provider '{source.provider}'."
                )
                return (f"Source '{source_slug}' is not configured as a Microsoft API source. {hint}", True)

            if source.isAuthenticated is True:
                return (f"Source '{source_slug}' is already authenticated.", False)

            if name == "source_microsoft_oauth_trigger":
                microsoft_service = None
                if isinstance(source.api, dict):
                    microsoft_service = source.api.get("microsoftService")
                    if not microsoft_service:
                        microsoft_service = _infer_microsoft_service(source.api.get("baseUrl"))
                if not microsoft_service:
                    return (
                        f"Cannot determine Microsoft service for source '{source_slug}'. Set microsoftService ('outlook', 'microsoft-calendar', 'onedrive', 'teams', or 'sharepoint') in api config.",
                        True,
                    )

            _callback(
                {
                    "__callback__": "auth_request",
                    "type": name,
                    "sourceSlug": source_slug,
                    "sessionId": self.session_id,
                }
            )
            if name == "source_oauth_trigger":
                return (f"OAuth authentication requested for '{source.name}'. Opening browser for authentication.", False)
            if name == "source_google_oauth_trigger":
                return (f"Google OAuth requested for '{source.name}'. Opening browser for authentication.", False)
            if name == "source_slack_oauth_trigger":
                return (f"Slack OAuth requested for '{source.name}'. Opening browser for authentication.", False)
            return (f"Microsoft OAuth requested for '{source.name}'. Opening browser for authentication.", False)

        if name == "source_credential_prompt":
            source_slug = str(args.get("sourceSlug", ""))
            mode = str(args.get("mode", ""))
            source = load_source_config(self.workspace_root, source_slug)
            if source is None:
                return (f"Source '{source_slug}' not found.", True)

            header_names = args.get("headerNames")
            if not isinstance(header_names, list):
                header_names = None

            effective_mode = mode
            source_header_names = source.api.get("headerNames") if isinstance(source.api, dict) else None
            if (header_names and len(header_names) > 0) or (isinstance(source_header_names, list) and len(source_header_names) > 0):
                effective_mode = "multi-header"

            if args.get("passwordRequired") is not None and effective_mode != "basic":
                return (
                    f'passwordRequired parameter only applies to basic auth mode. You specified mode="{mode}" with passwordRequired={args.get("passwordRequired")}.',
                    True,
                )

            effective_header_names = header_names or (source_header_names if isinstance(source_header_names, list) else None)

            _callback(
                {
                    "__callback__": "auth_request",
                    "type": "source_credential_prompt",
                    "sourceSlug": source_slug,
                    "mode": effective_mode,
                    "sessionId": self.session_id,
                    "labels": args.get("labels"),
                    "description": args.get("description"),
                    "hint": args.get("hint"),
                    "headerNames": effective_header_names,
                    "passwordRequired": args.get("passwordRequired"),
                }
            )
            return (f"Authentication requested for '{source.name}'. Waiting for user input.", False)

        if name == "source_test":
            source_slug = str(args.get("sourceSlug", ""))
            source = load_source_config(self.workspace_root, source_slug)
            if source is None:
                return (f"Source '{source_slug}' not found in workspace.", True)

            lines: list[str] = []
            has_errors = False
            has_warnings = False

            lines.append("## Schema Validation")
            valid, errors = _validate_json_file_has_fields(self.workspace_root / "sources" / source_slug / "config.json", ["slug", "name", "type"])
            if valid:
                lines.append("✓ Config schema valid")
            else:
                has_errors = True
                lines.append("✗ Config schema invalid:")
                lines.extend([f"  - {item}" for item in errors])

            lines.append("\n## Icon Status")
            source_dir = self.workspace_root / "sources" / source_slug
            icon_png = source_dir / "icon.png"
            icon_svg = source_dir / "icon.svg"
            icon_jpg = source_dir / "icon.jpg"
            if icon_png.exists() or icon_svg.exists() or icon_jpg.exists():
                icon_format = "PNG" if icon_png.exists() else ("SVG" if icon_svg.exists() else "JPG")
                lines.append(f"✓ Icon file exists ({icon_format})")
            elif source.icon and _is_likely_emoji(str(source.icon)):
                lines.append(f"✓ Emoji icon configured: {source.icon}")
            elif source.icon:
                lines.append(f"ℹ Icon configured: {source.icon}")
            else:
                has_warnings = True
                lines.append("⚠ No icon configured")
                lines.append("  Options:")
                lines.append("  - Add icon.png or icon.svg to source folder")
                lines.append('  - Set "icon" field to a URL or emoji in config.json')

            lines.append("\n## Completeness Check")
            guide_path = self.workspace_root / "sources" / source_slug / "guide.md"
            if guide_path.exists():
                try:
                    guide_content = guide_path.read_text(encoding="utf-8")
                    words = len([chunk for chunk in re.split(r"\s+", guide_content.strip()) if chunk])
                    lines.append(f"✓ guide.md exists ({words} words)")
                    if words < 50:
                        lines.append("  ℹ Guide is short - consider adding more context")
                except Exception:
                    lines.append("✓ guide.md exists")
            else:
                has_warnings = True
                lines.append("⚠ No guide.md file")
                lines.append("  Recommended: Add guide.md with usage instructions for the agent")

            config_payload: dict[str, Any] | None = None
            try:
                config_payload = json.loads((self.workspace_root / "sources" / source_slug / "config.json").read_text(encoding="utf-8"))
            except Exception:
                config_payload = None

            tagline = getattr(source, "tagline", None)
            if not tagline and isinstance(config_payload, dict) and config_payload.get("description"):
                has_warnings = True
                lines.append('⚠ Found "description" field instead of "tagline"')
                lines.append('  Rename "description" to "tagline" in config.json')
            elif not tagline:
                has_warnings = True
                lines.append("⚠ No tagline configured")
                lines.append('  Add "tagline": "Brief description" to config.json')
            else:
                lines.append(f"✓ Tagline: \"{tagline}\"")
            if source.name:
                lines.append(f"✓ Name: \"{source.name}\"")

            lines.append("\n## Connection Test")
            connection_lines, connection_success, connection_has_error, connection_error = _source_connection_test(source, self.workspace_root, source_slug)
            lines.extend(connection_lines)
            if connection_has_error:
                has_errors = True

            lines.append("\n## Authentication")
            if source.isAuthenticated:
                if _source_requires_auth(source):
                    cached_credential = _read_cached_credential(self.workspace_root, source_slug)
                    if source.type == "mcp":
                        lines.append("✓ Source is authenticated")
                    elif cached_credential:
                        lines.append("✓ Source is authenticated (token available)")
                    else:
                        has_warnings = True
                        lines.append("⚠ Source marked authenticated but token missing or refresh failed")
                        lines.append("  Re-authenticate to refresh credentials")
                else:
                    lines.append("✓ Source is authenticated")
            else:
                if _source_requires_auth(source):
                    has_warnings = True
                    lines.append("⚠ Source not authenticated")
                    if source.type == "mcp":
                        lines.append("  Use source_oauth_trigger to authenticate")
                    elif source.provider == "google":
                        lines.append("  Use source_google_oauth_trigger to authenticate")
                    elif source.provider == "slack":
                        lines.append("  Use source_slack_oauth_trigger to authenticate")
                    elif source.provider == "microsoft":
                        lines.append("  Use source_microsoft_oauth_trigger to authenticate")
                    else:
                        lines.append("  Use source_credential_prompt to enter credentials")
                else:
                    lines.append("ℹ Source does not require authentication")

            # Update metadata in config
            try:
                config_path = self.workspace_root / "sources" / source_slug / "config.json"
                payload = json.loads(config_path.read_text(encoding="utf-8"))
                payload["lastTestedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                payload["connectionStatus"] = "error" if connection_has_error else ("connected" if connection_success else "disconnected")
                payload["connectionError"] = connection_error
                payload["updatedAt"] = int(time.time() * 1000)
                config_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            except Exception:
                pass

            lines.append("\n---")
            if has_errors:
                lines.append("**Result: ✗ Validation failed with errors**")
            elif has_warnings:
                lines.append("**Result: ⚠ Validation passed with warnings**")
            else:
                lines.append("**Result: ✓ Validation passed**")

            return ("\n".join(lines), has_errors)

        if name == "call_llm":
            precomputed = args.get("_precomputedResult")
            if isinstance(precomputed, str) and precomputed:
                try:
                    parsed = json.loads(precomputed)
                    if isinstance(parsed, dict):
                        if parsed.get("error") is not None:
                            return (f"call_llm failed: {parsed.get('error')}", True)
                        if "text" in parsed:
                            text = parsed.get("text")
                            return ((text if isinstance(text, str) and text else "(Model returned empty response)"), False)
                    return (
                        "call_llm: _precomputedResult has unexpected format (missing text field).",
                        True,
                    )
                except Exception:
                    return (f"call_llm: Failed to parse _precomputedResult: {precomputed[:200]}", True)

            if self.callback_port:
                try:
                    request = Request(
                        url=f"http://127.0.0.1:{self.callback_port}/call-llm",
                        method="POST",
                        headers={"Content-Type": "application/json"},
                        data=json.dumps(args).encode("utf-8"),
                    )
                    with urlopen(request, timeout=30) as response:
                        body = response.read().decode("utf-8", errors="replace")
                    parsed_result = json.loads(body)
                    if not isinstance(parsed_result, dict):
                        return ("call_llm callback failed: Invalid JSON response", True)
                    if parsed_result.get("error") is not None:
                        return (f"call_llm failed: {parsed_result.get('error')}", True)
                    text = parsed_result.get("text")
                    return ((text if isinstance(text, str) and text else "(Model returned empty response)"), False)
                except HTTPError as error:
                    try:
                        message = error.read().decode("utf-8", errors="replace")
                    except Exception:
                        message = str(error)
                    return (f"call_llm callback failed: {message}", True)
                except URLError as error:
                    reason = getattr(error, "reason", error)
                    return (f"call_llm callback failed: {reason}", True)
                except Exception as error:
                    return (f"call_llm callback failed: {error}", True)

            return (
                "call_llm requires either PreToolUse intercept (_precomputedResult) or "
                "HTTP callback (AGENT_LLM_CALLBACK_PORT). Neither is available.",
                True,
            )

        return (f"Unknown tool: {name}", True)


def main() -> int:
    parser = argparse.ArgumentParser(prog="session-mcp-server")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--plans-folder", required=True)
    parser.add_argument("--callback-port")
    args = parser.parse_args()

    callback_port = args.callback_port or os.environ.get("AGENT_LLM_CALLBACK_PORT")

    server = SessionServer(
        session_id=args.session_id,
        workspace_root=Path(args.workspace_root).expanduser().resolve(),
        plans_folder=Path(args.plans_folder).expanduser().resolve(),
        callback_port=callback_port,
    )

    send_log(f"Python Session MCP Server started for session {args.session_id}")
    run_stdio_server(
        server_name="agent-session",
        server_version="0.4.8-py",
        tools_provider=server.tools,
        call_tool_handler=server.call_tool,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
