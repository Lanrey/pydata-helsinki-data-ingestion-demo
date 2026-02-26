from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

SourceType = Literal["mcp", "api", "local"]
ConnectionStatus = Literal["connected", "needs_auth", "failed", "untested", "local_disabled"]


@dataclass(slots=True)
class SourceConfig:
    id: str
    name: str
    slug: str
    enabled: bool
    provider: str | None
    type: SourceType
    createdAt: int
    updatedAt: int
    tagline: str | None = None
    isAuthenticated: bool | None = None
    connectionStatus: ConnectionStatus | None = None
    connectionError: str | None = None
    mcp: dict[str, Any] | None = None
    api: dict[str, Any] | None = None
    local: dict[str, Any] | None = None
    icon: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "SourceConfig":
        return SourceConfig(
            id=str(payload["id"]),
            name=str(payload["name"]),
            slug=str(payload["slug"]),
            enabled=bool(payload.get("enabled", True)),
            provider=(str(payload["provider"]) if payload.get("provider") is not None else None),
            type=payload["type"],
            tagline=(str(payload["tagline"]) if payload.get("tagline") is not None else None),
            createdAt=int(payload["createdAt"]),
            updatedAt=int(payload["updatedAt"]),
            isAuthenticated=(
                bool(payload["isAuthenticated"]) if payload.get("isAuthenticated") is not None else None
            ),
            connectionStatus=payload.get("connectionStatus"),
            connectionError=(
                str(payload["connectionError"]) if payload.get("connectionError") is not None else None
            ),
            mcp=payload.get("mcp"),
            api=payload.get("api"),
            local=payload.get("local"),
            icon=(str(payload["icon"]) if payload.get("icon") is not None else None),
        )
