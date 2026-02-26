from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]

    def as_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def _read_message() -> dict[str, Any] | None:
    content_length = 0

    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None

        stripped = line.strip()
        if not stripped:
            break

        header = line.decode("utf-8", errors="replace").strip()
        if header.lower().startswith("content-length:"):
            value = header.split(":", 1)[1].strip()
            content_length = int(value)

    if content_length <= 0:
        return None

    body = sys.stdin.buffer.read(content_length)
    if not body:
        return None

    return json.loads(body.decode("utf-8"))


def _write_message(payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    sys.stdout.buffer.write(header)
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def send_log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run_stdio_server(
    *,
    server_name: str,
    server_version: str,
    tools_provider: callable,
    call_tool_handler: callable,
) -> None:
    while True:
        message = _read_message()
        if message is None:
            return

        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if request_id is None:
            continue

        try:
            if method == "initialize":
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": server_name, "version": server_version},
                        },
                    }
                )
                continue

            if method == "tools/list":
                tools = [tool.as_mcp() for tool in tools_provider()]
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"tools": tools},
                    }
                )
                continue

            if method == "tools/call":
                name = str(params.get("name", ""))
                arguments = params.get("arguments") or {}
                content, is_error = call_tool_handler(name, arguments)
                if is_error and isinstance(content, str) and not content.startswith("[ERROR]"):
                    content = f"[ERROR] {content}"
                response_payload = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": content}],
                        "isError": bool(is_error),
                    },
                }
                _write_message(response_payload)
                continue

            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}",
                    },
                }
            )
        except Exception as error:
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": str(error),
                    },
                }
            )
