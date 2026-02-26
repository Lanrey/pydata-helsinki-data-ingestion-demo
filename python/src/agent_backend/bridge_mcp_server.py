from __future__ import annotations

import argparse
import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .mcp_stdio import ToolDef, run_stdio_server, send_log


MAX_RESPONSE_SIZE = 60 * 1024


@dataclass(slots=True)
class ApiSourceConfig:
    slug: str
    name: str
    baseUrl: str
    authType: str
    workspaceId: str
    provider: str | None = None
    headerName: str | None = None
    queryParam: str | None = None
    authScheme: str | None = None
    defaultHeaders: dict[str, str] | None = None
    guideRaw: str | None = None


def _credential_cache_path(workspace_id: str, source_slug: str) -> Path:
    return Path.home() / ".agent-runtime" / "workspaces" / workspace_id / "sources" / source_slug / ".credential-cache.json"


def _read_credential(workspace_id: str, source_slug: str) -> str | None:
    path = _credential_cache_path(workspace_id, source_slug)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        expires_at = payload.get("expiresAt")
        if isinstance(expires_at, (int, float)) and expires_at < __import__("time").time() * 1000:
            return None
        value = payload.get("value")
        return str(value) if value is not None else None
    except Exception:
        return None


def _build_url(source: ApiSourceConfig, api_path: str, method: str, params: dict[str, Any], credential: str | None) -> str:
    base = source.baseUrl[:-1] if source.baseUrl.endswith("/") else source.baseUrl
    normalized_path = api_path if api_path.startswith("/") else f"/{api_path}"
    url = f"{base}{normalized_path}"

    query: dict[str, str] = {}
    if source.authType == "query" and source.queryParam and credential:
        query[source.queryParam] = credential

    if method == "GET":
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                query[key] = json.dumps(value)
            else:
                query[key] = str(value)

    if query:
        sep = "&" if "?" in url else "?"
        url += sep + urlencode(query)
    return url


def _build_headers(source: ApiSourceConfig, credential: str | None) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if source.defaultHeaders:
        headers.update(source.defaultHeaders)

    if source.authType == "none" or not credential:
        return headers

    if source.authType == "bearer":
        scheme = source.authScheme or "Bearer"
        headers["Authorization"] = f"{scheme} {credential}" if scheme else credential
    elif source.authType == "header":
        headers[source.headerName or "X-API-Key"] = credential
    elif source.authType == "basic":
        auth_string = credential
        try:
            parsed = json.loads(credential)
            if isinstance(parsed, dict) and parsed.get("username") and parsed.get("password"):
                auth_string = f"{parsed['username']}:{parsed['password']}"
        except json.JSONDecodeError:
            pass
        headers["Authorization"] = "Basic " + base64.b64encode(auth_string.encode("utf-8")).decode("utf-8")

    return headers


class BridgeServer:
    def __init__(self, config_path: Path, session_path: Path | None):
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        self.session_path = session_path
        self.sources: list[ApiSourceConfig] = [ApiSourceConfig(**item) for item in payload.get("sources", [])]
        self.by_tool_name: dict[str, ApiSourceConfig] = {f"api_{source.slug}": source for source in self.sources}

    def tools(self) -> list[ToolDef]:
        tools: list[ToolDef] = []
        for source in self.sources:
            description = f"Make authenticated requests to {source.name} API ({source.baseUrl})"
            if source.guideRaw:
                description += "\n\n" + source.guideRaw[:2000]
            tools.append(
                ToolDef(
                    name=f"api_{source.slug}",
                    description=description,
                    input_schema={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]},
                            "params": {"type": "object", "additionalProperties": True},
                            "_intent": {"type": "string"},
                        },
                        "required": ["path", "method"],
                    },
                )
            )
        return tools

    def call_tool(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        source = self.by_tool_name.get(name)
        if source is None:
            return (f"Unknown tool: {name}", True)

        method = str(args.get("method", "GET")).upper()
        path = str(args.get("path", ""))
        params = args.get("params") if isinstance(args.get("params"), dict) else {}

        credential = _read_credential(source.workspaceId, source.slug)
        if source.authType != "none" and not credential:
            return (f"Authentication required for {source.name}.", True)

        url = _build_url(source, path, method, params, credential)
        headers = _build_headers(source, credential)

        body = None
        if method != "GET" and params:
            body = json.dumps(params).encode("utf-8")

        request = Request(url=url, method=method, headers=headers, data=body)

        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read()
                text = raw.decode("utf-8", errors="replace")

                if len(text) > MAX_RESPONSE_SIZE and self.session_path is not None:
                    responses_dir = self.session_path / "long_responses"
                    responses_dir.mkdir(parents=True, exist_ok=True)
                    safe = path.replace("/", "_").replace("?", "_")[:40] or "root"
                    file_path = responses_dir / f"{name}_{safe}.txt"
                    file_path.write_text(text, encoding="utf-8")
                    preview = text[:2000]
                    return (
                        f"[Response too large ({round(len(text)/1024)}KB)]\\nFull data saved to: {file_path}\\n\\nPreview:\\n{preview}...",
                        False,
                    )

                return (text, False)
        except HTTPError as error:
            response_text = error.read().decode("utf-8", errors="replace")
            return (f"API Error {error.code}: {response_text}", True)
        except URLError as error:
            return (f"Request failed: {error.reason}", True)
        except Exception as error:
            return (f"Request failed: {error}", True)


def main() -> int:
    parser = argparse.ArgumentParser(prog="bridge-mcp-server")
    parser.add_argument("--config", required=True)
    parser.add_argument("--session")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    session_path = Path(args.session).expanduser().resolve() if args.session else None
    server = BridgeServer(config_path=config_path, session_path=session_path)

    send_log(f"Python Bridge MCP Server started with {len(server.sources)} API sources")
    run_stdio_server(
        server_name="agent-api-bridge",
        server_version="0.4.8-py",
        tools_provider=server.tools,
        call_tool_handler=server.call_tool,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
