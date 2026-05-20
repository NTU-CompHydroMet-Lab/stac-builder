"""Dry-run planning tool for catalog YAMLs.

``describe_catalog_file`` parses a catalog YAML and returns a structured
summary of what a ``build`` *would* produce — without ever touching the
underlying data or generating STAC JSON.

Two key differences from ``preflight``:

1. Missing data is a **warning**, never an error. Use case: zarr is still
   being processed, the user wants to plan/verify the catalog YAML in
   advance and monitor when files land.
2. The output is structured (dataclass), not a list of issues. The CLI
   formats it into a human-readable preview; downstream tools could
   consume the dataclass directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


def _glob_chars(s: str) -> bool:
    return any(ch in s for ch in ("*", "?", "["))


def _detect_generator_kind(catalog_meta: dict, source: dict) -> str:
    src_meta = source.get("metadata") or {}
    if isinstance(src_meta.get("generator"), str):
        return src_meta["generator"].strip().lower()
    if isinstance(catalog_meta.get("generator"), str):
        return catalog_meta["generator"].strip().lower()
    driver = str(source.get("driver", "")).strip().lower()
    if driver in {"raster", "vector"}:
        return driver
    return "xarray"


def _normalize_group_id(catalog_name: Optional[str]) -> Optional[str]:
    if not catalog_name:
        return None
    return catalog_name.strip().lower().replace(" ", "_").replace("-", "_")


def _check_urlpath_presence(urlpath: Any) -> tuple[int, list[str]]:
    """Return (present_count, missing_paths) for a urlpath spec."""
    if urlpath is None or urlpath == "":
        return 0, []
    paths = urlpath if isinstance(urlpath, list) else [urlpath]
    present = 0
    missing: list[str] = []
    for raw in paths:
        s = str(raw)
        if "://" in s and not s.startswith("file://"):
            # Network URL — we cannot check cheaply; treat as present.
            present += 1
            continue
        p = Path(s).expanduser()
        if _glob_chars(s):
            matches = list(p.parent.glob(p.name))
            if matches:
                present += len(matches)
            else:
                missing.append(s)
        else:
            if p.exists():
                present += 1
            else:
                missing.append(s)
    return present, missing


@dataclass
class SourceDescription:
    source_id: str
    driver: str
    generator_kind: str
    collection_id: str
    expected_output_path: str  # e.g. "{group_id}/{collection_id}"
    urlpaths_present: int = 0
    urlpaths_missing: list[str] = field(default_factory=list)
    thumbnail_variable: Optional[str] = None
    thumbnail_datetime: Optional[str] = None
    description_excerpt: Optional[str] = None


@dataclass
class CatalogDescription:
    catalog_path: Path
    group_id: Optional[str] = None
    group_title: Optional[str] = None
    group_description: Optional[str] = None
    sources: list[SourceDescription] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def format_text(self) -> str:
        lines = [f"Catalog: {self.catalog_path}"]
        if self.errors:
            lines.append("  ERRORS:")
            for e in self.errors:
                lines.append(f"    - {e}")
            if not self.sources:
                return "\n".join(lines)
        if self.group_id:
            lines.append(f"  group_id: {self.group_id}")
            if self.group_title and self.group_title != self.group_id:
                lines.append(f"  group_title: {self.group_title}")
        for src in self.sources:
            lines.append("")
            lines.append(f"  Source: {src.source_id}")
            lines.append(f"    collection_id: {src.collection_id}")
            lines.append(f"    generator: {src.generator_kind}  (driver: {src.driver})")
            lines.append(f"    expected output: {src.expected_output_path}/")
            if src.urlpaths_present or src.urlpaths_missing:
                lines.append(
                    f"    data: {src.urlpaths_present} present"
                    + (
                        f", {len(src.urlpaths_missing)} missing"
                        if src.urlpaths_missing
                        else ""
                    )
                )
            for m in src.urlpaths_missing:
                lines.append(f"      ⚠️  not yet present: {m}")
            if src.thumbnail_variable:
                t = src.thumbnail_variable
                if src.thumbnail_datetime:
                    t += f" @ {src.thumbnail_datetime}"
                lines.append(f"    thumbnail: {t}")
            if src.description_excerpt:
                lines.append(f"    description: {src.description_excerpt}")
        return "\n".join(lines)


def describe_catalog_file(catalog_file: Path) -> CatalogDescription:
    """Parse a catalog YAML and describe what a build would produce."""
    p = Path(catalog_file)
    result = CatalogDescription(catalog_path=p)

    if not p.exists():
        result.errors.append(f"catalog file not found: {p}")
        return result

    try:
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        result.errors.append(f"YAML parse error: {e}")
        return result

    if not isinstance(loaded, dict):
        result.errors.append("catalog root is not a mapping")
        return result

    catalog_meta = loaded.get("metadata") or {}
    catalog_name = catalog_meta.get("catalog_name")
    if isinstance(catalog_name, str) and catalog_name.strip():
        result.group_id = _normalize_group_id(catalog_name)
        result.group_title = catalog_name.strip()
    result.group_description = catalog_meta.get("description")

    sources = loaded.get("sources")
    if not isinstance(sources, dict) or not sources:
        result.errors.append("'sources' must be a non-empty mapping")
        return result

    for source_id, source in sources.items():
        if not isinstance(source, dict):
            result.errors.append(f"source '{source_id}' is not a mapping")
            continue

        meta = source.get("metadata") or {}
        if not isinstance(meta, dict):
            result.errors.append(f"source '{source_id}' metadata is not a mapping")
            continue

        collection_id = meta.get("id")
        if not collection_id:
            result.errors.append(
                f"source '{source_id}' missing required metadata field 'id'"
            )
            continue
        if not meta.get("description"):
            result.errors.append(
                f"source '{source_id}' missing required metadata field 'description'"
            )
            # Still build a partial SourceDescription so users see structure.

        driver = str(source.get("driver", "")).strip().lower()
        kind = _detect_generator_kind(catalog_meta, source)
        args = source.get("args") or {}
        if not isinstance(args, dict):
            args = {}

        urlpath = args.get("urlpath")
        present, missing = _check_urlpath_presence(urlpath)

        if result.group_id:
            expected_output = f"{result.group_id}/{collection_id}"
        else:
            expected_output = f"{collection_id}"

        desc = meta.get("description") or ""
        excerpt = None
        if isinstance(desc, str) and desc.strip():
            first_line = desc.strip().splitlines()[0].strip()
            excerpt = first_line if len(first_line) <= 120 else first_line[:117] + "..."

        result.sources.append(
            SourceDescription(
                source_id=source_id,
                driver=driver or "<missing>",
                generator_kind=kind,
                collection_id=str(collection_id),
                expected_output_path=expected_output,
                urlpaths_present=present,
                urlpaths_missing=missing,
                thumbnail_variable=meta.get("thumbnail_variable"),
                thumbnail_datetime=meta.get("thumbnail_datetime"),
                description_excerpt=excerpt,
            )
        )

    return result
