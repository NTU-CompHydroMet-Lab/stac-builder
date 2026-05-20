"""Builder settings.

Loaded lazily from ``$STAC_BUILDER_CONFIG`` (or ``config/main.yaml`` next to the
project root) the first time ``get_settings()`` is called. The CLI may also
inject a config path explicitly.

The file is **optional** — if missing or invalid, ``get_settings()`` returns
``None`` and callers should fall back to CLI args / defaults. The engine must
remain runnable without ``main.yaml`` so that deployment repos can pass
catalog paths and project metadata via CLI flags instead.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import yaml
from loguru import logger
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).parent.parent


def _default_config_path() -> Path:
    override = os.environ.get("STAC_BUILDER_CONFIG")
    if override:
        return Path(override).expanduser()
    return PROJECT_ROOT / "config" / "main.yaml"


class ProjectSettings(BaseModel):
    title: str
    id: str
    description: str


class BuildTarget(BaseModel):
    catalog: str
    source: str


class BuildSettings(BaseModel):
    targets: List[BuildTarget] = Field(default_factory=list)


class FilesystemSettings(BaseModel):
    output_dir: str = "stac_catalog"
    link_strategy: str = "absolute"


class ConcurrencySettings(BaseModel):
    max_workers: int = 5


class ProductAliasEntry(BaseModel):
    match: str
    alias: str


class EventAggregationSettings(BaseModel):
    product_aliases: list[ProductAliasEntry] = Field(default_factory=list)


class BuilderSettings(BaseModel):
    project: Optional[ProjectSettings] = None
    build: BuildSettings = Field(default_factory=BuildSettings)
    filesystem: FilesystemSettings = Field(default_factory=FilesystemSettings)
    concurrency: ConcurrencySettings = Field(default_factory=ConcurrencySettings)
    event_aggregation: EventAggregationSettings = Field(default_factory=EventAggregationSettings)


_settings_cache: Optional[BuilderSettings] = None
_settings_loaded = False


def load_settings(config_path: Optional[Path] = None) -> Optional[BuilderSettings]:
    """Load settings from ``config_path`` (or default) and cache the result.

    Returns ``None`` if the file does not exist or fails to parse. Callers must
    handle the ``None`` case — typically by reading CLI args or env vars.
    """
    global _settings_cache, _settings_loaded
    path = config_path or _default_config_path()
    if not path.exists():
        logger.debug(f"No settings file at {path}; running without main.yaml.")
        _settings_cache = None
        _settings_loaded = True
        return None
    try:
        data = yaml.safe_load(path.read_text()) or {}
        _settings_cache = BuilderSettings(**data)
        _settings_loaded = True
        return _settings_cache
    except Exception as e:
        logger.warning(f"Failed to parse {path}: {e}. Continuing without settings.")
        _settings_cache = None
        _settings_loaded = True
        return None


def get_settings() -> Optional[BuilderSettings]:
    """Return cached settings, loading on first access."""
    if not _settings_loaded:
        return load_settings()
    return _settings_cache


def reset_settings_cache() -> None:
    """Clear the cache. Useful for tests."""
    global _settings_cache, _settings_loaded
    _settings_cache = None
    _settings_loaded = False
