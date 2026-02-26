from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from .models import SourceConfig, SourceType


def _sources_dir(workspace_root: Path) -> Path:
    return workspace_root / "sources"


def ensure_sources_dir(workspace_root: Path) -> Path:
    sources_dir = _sources_dir(workspace_root)
    sources_dir.mkdir(parents=True, exist_ok=True)
    return sources_dir


def get_source_path(workspace_root: Path, source_slug: str) -> Path:
    return _sources_dir(workspace_root) / source_slug


def load_source_config(workspace_root: Path, source_slug: str) -> SourceConfig | None:
    config_path = get_source_path(workspace_root, source_slug) / "config.json"
    if not config_path.exists():
        return None

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    return SourceConfig.from_dict(payload)


def save_source_config(workspace_root: Path, config: SourceConfig) -> None:
    source_dir = get_source_path(workspace_root, config.slug)
    source_dir.mkdir(parents=True, exist_ok=True)
    config.updatedAt = int(time.time() * 1000)
    config_path = source_dir / "config.json"
    config_path.write_text(json.dumps(config.to_dict(), indent=2) + "\n", encoding="utf-8")


def load_workspace_sources(workspace_root: Path) -> list[SourceConfig]:
    sources_dir = ensure_sources_dir(workspace_root)
    sources: list[SourceConfig] = []

    for child in sorted(sources_dir.iterdir(), key=lambda entry: entry.name):
        if not child.is_dir():
            continue
        source = load_source_config(workspace_root, child.name)
        if source is not None:
            sources.append(source)

    return sources


def generate_source_slug(workspace_root: Path, name: str) -> str:
    base_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50]
    if not base_slug:
        base_slug = "source"

    sources_dir = ensure_sources_dir(workspace_root)
    existing = {entry.name for entry in sources_dir.iterdir() if entry.is_dir()}
    if base_slug not in existing:
        return base_slug

    counter = 2
    while f"{base_slug}-{counter}" in existing:
        counter += 1

    return f"{base_slug}-{counter}"


def create_source(
    workspace_root: Path,
    name: str,
    source_type: SourceType,
    provider: str | None = None,
    enabled: bool = True,
    mcp: dict | None = None,
    api: dict | None = None,
    local: dict | None = None,
    icon: str | None = None,
) -> SourceConfig:
    slug = generate_source_slug(workspace_root, name)
    now = int(time.time() * 1000)
    config = SourceConfig(
        id=f"{slug}_{uuid.uuid4().hex[:8]}",
        name=name,
        slug=slug,
        enabled=enabled,
        provider=provider,
        type=source_type,
        createdAt=now,
        updatedAt=now,
        mcp=mcp if source_type == "mcp" else None,
        api=api if source_type == "api" else None,
        local=local if source_type == "local" else None,
        icon=icon,
    )
    save_source_config(workspace_root, config)
    return config


def delete_source(workspace_root: Path, source_slug: str) -> bool:
    source_dir = get_source_path(workspace_root, source_slug)
    config_path = source_dir / "config.json"
    if not config_path.exists():
        return False

    for child in source_dir.iterdir():
        if child.is_file() or child.is_symlink():
            child.unlink()
        elif child.is_dir():
            _delete_tree(child)
    source_dir.rmdir()
    return True


def _delete_tree(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir():
            _delete_tree(child)
        else:
            child.unlink()
    path.rmdir()


def mark_source_authenticated(workspace_root: Path, source_slug: str) -> bool:
    config = load_source_config(workspace_root, source_slug)
    if config is None:
        return False

    config.isAuthenticated = True
    config.connectionStatus = "connected"
    config.connectionError = None
    save_source_config(workspace_root, config)
    return True
