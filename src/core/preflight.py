"""Pre-build validation that fails fast with all errors at once.

Run before any STAC generation work. Collects every issue it can find and
returns a structured report; never raises on user-fixable problems. The CLI
prints the report and exits with a non-zero code if there are any errors.

Why collect everything: cron jobs running in containers leave behind a single
log; if we stop at the first error the user has to iterate one fix at a time.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import yaml


class Severity(str, enum.Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"


@dataclass
class Issue:
    severity: Severity
    message: str
    source: str = ""  # e.g. "catalogs/foo.yaml :: my_source.args.urlpath"
    hint: str = ""

    def format_text(self) -> str:
        parts = [f"[{self.severity.value}]"]
        if self.source:
            parts.append(self.source)
        parts.append(self.message)
        line = " ".join(parts)
        if self.hint:
            line += f"\n    hint: {self.hint}"
        return line


@dataclass
class PreflightReport:
    issues: list[Issue] = field(default_factory=list)

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)

    def extend(self, issues: Iterable[Issue]) -> None:
        self.issues.extend(issues)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def format_text(self) -> str:
        if not self.issues:
            return "Preflight: OK"
        return "\n".join(i.format_text() for i in self.issues)


# ---------------------------------------------------------------------------
# Output directory check
# ---------------------------------------------------------------------------
def check_output_dir(output_dir: Path) -> PreflightReport:
    """Check that ``output_dir`` exists or can be created and is writable."""
    report = PreflightReport()
    p = Path(output_dir)
    if p.exists():
        if not p.is_dir():
            report.add(Issue(
                severity=Severity.ERROR,
                message=f"output path is not a directory: {p}",
                source=str(p),
            ))
            return report
        # Try a quick write check.
        try:
            probe = p / ".preflight_write_check"
            probe.touch()
            probe.unlink()
        except OSError as e:
            report.add(Issue(
                severity=Severity.ERROR,
                message=f"output directory not writable: {e}",
                source=str(p),
            ))
        return report

    # Does not exist yet — does its parent allow creation?
    parent = p.parent
    if not parent.exists():
        report.add(Issue(
            severity=Severity.ERROR,
            message=f"output directory parent does not exist: {parent}",
            source=str(p),
            hint="create the parent directory or pass --output to a writable location",
        ))
        return report
    if not parent.is_dir():
        report.add(Issue(
            severity=Severity.ERROR,
            message=f"output directory parent is not a directory: {parent}",
            source=str(p),
        ))
        return report
    return report


# ---------------------------------------------------------------------------
# Catalog file checks
# ---------------------------------------------------------------------------
_REQUIRED_METADATA_FIELDS = ("id", "description")


def _glob_chars(s: str) -> bool:
    return any(ch in s for ch in ("*", "?", "["))


def _check_urlpath(
    urlpath: Union[str, list, None],
    src_label: str,
) -> list[Issue]:
    issues: list[Issue] = []
    if urlpath is None or urlpath == "":
        issues.append(Issue(
            severity=Severity.ERROR,
            message="missing 'args.urlpath'",
            source=src_label,
        ))
        return issues

    paths: list[str] = urlpath if isinstance(urlpath, list) else [str(urlpath)]
    for raw in paths:
        s = str(raw)
        # Skip network URLs; we cannot verify them cheaply.
        if "://" in s and not s.startswith("file://"):
            continue
        p = Path(s).expanduser()
        if _glob_chars(s):
            matches = list(p.parent.glob(p.name))
            if not matches:
                issues.append(Issue(
                    severity=Severity.ERROR,
                    message=f"glob matched no files: {s}",
                    source=f"{src_label}.args.urlpath",
                ))
        else:
            if not p.exists():
                issues.append(Issue(
                    severity=Severity.ERROR,
                    message=f"data not found: {s}",
                    source=f"{src_label}.args.urlpath",
                ))
    return issues


def _check_source(
    catalog_file: Path,
    source_id: str,
    source: dict,
    catalog_meta: dict,
) -> list[Issue]:
    src_label = f"{catalog_file.name} :: {source_id}"
    issues: list[Issue] = []

    if not isinstance(source, dict):
        issues.append(Issue(
            severity=Severity.ERROR,
            message="source is not a mapping",
            source=src_label,
        ))
        return issues

    driver = source.get("driver")
    if not driver:
        issues.append(Issue(
            severity=Severity.ERROR,
            message="missing 'driver'",
            source=src_label,
            hint="e.g. driver: zarr | netcdf | parquet | raster | vector",
        ))

    args = source.get("args") or {}
    if not isinstance(args, dict):
        issues.append(Issue(
            severity=Severity.ERROR,
            message="'args' is not a mapping",
            source=src_label,
        ))
        args = {}

    issues.extend(_check_urlpath(args.get("urlpath"), src_label))

    # Metadata schema (require id + description).
    meta = source.get("metadata") or {}
    if not isinstance(meta, dict):
        issues.append(Issue(
            severity=Severity.ERROR,
            message="'metadata' is not a mapping",
            source=src_label,
        ))
        meta = {}
    for field_ in _REQUIRED_METADATA_FIELDS:
        if not meta.get(field_):
            issues.append(Issue(
                severity=Severity.ERROR,
                message=f"missing required metadata field '{field_}'",
                source=src_label,
            ))

    # Generator-specific extras.
    kind = _detect_generator_kind(catalog_meta, source)
    if kind == "raster":
        if not (args.get("asset_map") or {}):
            issues.append(Issue(
                severity=Severity.ERROR,
                message="raster source requires 'args.asset_map'",
                source=src_label,
                hint="map each TIFF stem to an asset descriptor; see template_geotiff.yaml",
            ))

    return issues


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


def check_catalog_file(
    catalog_file: Path,
    only_sources: Optional[list[str]] = None,
) -> PreflightReport:
    """Validate a single catalog YAML file end-to-end.

    Parameters
    ----------
    catalog_file:
        Path to the catalog YAML.
    only_sources:
        If provided, only sources whose key is in this list are checked. Other
        sources are skipped silently (useful for CLI ``--source`` filtering).
    """
    report = PreflightReport()
    p = Path(catalog_file)

    if not p.exists():
        report.add(Issue(
            severity=Severity.ERROR,
            message=f"catalog file not found: {p}",
            source=str(p),
        ))
        return report

    try:
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        report.add(Issue(
            severity=Severity.ERROR,
            message=f"YAML parse error: {e}",
            source=str(p),
        ))
        return report

    if not isinstance(loaded, dict):
        report.add(Issue(
            severity=Severity.ERROR,
            message="catalog root is not a mapping",
            source=str(p),
        ))
        return report

    catalog_meta = loaded.get("metadata") or {}
    sources = loaded.get("sources")
    if sources is None:
        report.add(Issue(
            severity=Severity.ERROR,
            message="missing 'sources' key",
            source=str(p),
        ))
        return report
    if not isinstance(sources, dict) or not sources:
        report.add(Issue(
            severity=Severity.ERROR,
            message="'sources' must be a non-empty mapping",
            source=str(p),
        ))
        return report

    targets = list(sources.keys()) if only_sources is None else [
        s for s in sources.keys() if s in only_sources
    ]

    for source_id in targets:
        issues = _check_source(p, source_id, sources[source_id], catalog_meta)
        report.extend(issues)

    return report


def check_all(
    catalog_files: list[Path],
    output_dir: Optional[Path] = None,
    only_sources: Optional[list[str]] = None,
) -> PreflightReport:
    """Top-level entry point: run every check, return one consolidated report."""
    report = PreflightReport()
    if output_dir is not None:
        report.extend(check_output_dir(output_dir).issues)
    for cat in catalog_files:
        report.extend(check_catalog_file(cat, only_sources=only_sources).issues)
    return report
