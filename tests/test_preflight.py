"""Tests for the preflight check module.

Preflight runs **before** any STAC generation work. It must:

- Collect every problem it can find, not stop at the first one.
- Report each problem with enough context (file, source, field) for the user
  to fix it without re-running.
- Never raise on validation issues; raising is reserved for the caller after
  it sees the issue list.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.core.preflight import (
    Issue,
    PreflightReport,
    Severity,
    check_catalog_file,
    check_output_dir,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _write(path: Path, payload: dict) -> Path:
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def _minimal_zarr_source(urlpath: str) -> dict:
    return {
        "driver": "zarr",
        "args": {"urlpath": urlpath},
        "metadata": {
            "id": "my_dataset",
            "description": "a dataset",
        },
    }


# ---------------------------------------------------------------------------
# PreflightReport semantics
# ---------------------------------------------------------------------------
class TestPreflightReport:
    def test_empty_report_is_ok(self):
        report = PreflightReport()
        assert report.ok
        assert report.errors == []
        assert report.warnings == []

    def test_error_makes_report_not_ok(self):
        report = PreflightReport()
        report.add(Issue(severity=Severity.ERROR, message="bad", source="x"))
        assert not report.ok

    def test_warning_alone_keeps_report_ok(self):
        report = PreflightReport()
        report.add(Issue(severity=Severity.WARNING, message="meh", source="x"))
        assert report.ok
        assert len(report.warnings) == 1

    def test_format_text_includes_severity_and_source(self):
        report = PreflightReport()
        report.add(Issue(severity=Severity.ERROR, message="missing field", source="cat.yaml :: my_src"))
        rendered = report.format_text()
        assert "ERROR" in rendered
        assert "missing field" in rendered
        assert "my_src" in rendered


# ---------------------------------------------------------------------------
# check_output_dir
# ---------------------------------------------------------------------------
class TestCheckOutputDir:
    def test_writable_existing_dir(self, tmp_path: Path):
        report = check_output_dir(tmp_path)
        assert report.ok

    def test_nonexistent_parent_is_error(self, tmp_path: Path):
        # Output dir's parent does not exist -> we can't create it -> error.
        target = tmp_path / "missing" / "nested" / "deeper" / "out"
        # Force parent missing scenario by passing a deeper path under a file
        bad_parent_file = tmp_path / "afile"
        bad_parent_file.write_text("not a dir")
        target = bad_parent_file / "out"
        report = check_output_dir(target)
        assert not report.ok
        assert any("output" in i.message.lower() for i in report.errors)

    def test_dir_will_be_created_if_missing(self, tmp_path: Path):
        target = tmp_path / "new_output"
        report = check_output_dir(target)
        # Missing-but-creatable is a warning, not an error.
        assert report.ok


# ---------------------------------------------------------------------------
# check_catalog_file - YAML structure
# ---------------------------------------------------------------------------
class TestCheckCatalogFile:
    def test_missing_file_is_error(self, tmp_path: Path):
        report = check_catalog_file(tmp_path / "nope.yaml")
        assert not report.ok
        assert any("not found" in i.message.lower() for i in report.errors)

    def test_invalid_yaml_is_error(self, tmp_path: Path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("this: is: not: valid: yaml: [\n", encoding="utf-8")
        report = check_catalog_file(bad)
        assert not report.ok
        assert any("yaml" in i.message.lower() for i in report.errors)

    def test_missing_sources_key_is_error(self, tmp_path: Path):
        cat = _write(tmp_path / "c.yaml", {"metadata": {"version": 1}})
        report = check_catalog_file(cat)
        assert not report.ok
        assert any("sources" in i.message.lower() for i in report.errors)

    def test_empty_sources_is_error(self, tmp_path: Path):
        cat = _write(tmp_path / "c.yaml", {"sources": {}})
        report = check_catalog_file(cat)
        assert not report.ok

    def test_minimal_valid_catalog_is_ok(self, tmp_path: Path):
        data_file = tmp_path / "data.zarr"
        data_file.mkdir()  # zarr is a directory
        cat = _write(
            tmp_path / "c.yaml",
            {"sources": {"my_dataset": _minimal_zarr_source(str(data_file))}},
        )
        report = check_catalog_file(cat)
        assert report.ok, report.format_text()


# ---------------------------------------------------------------------------
# Source-level checks
# ---------------------------------------------------------------------------
class TestSourceLevelChecks:
    def test_missing_driver_is_error(self, tmp_path: Path):
        source = _minimal_zarr_source(str(tmp_path / "x.zarr"))
        del source["driver"]
        cat = _write(tmp_path / "c.yaml", {"sources": {"s": source}})
        report = check_catalog_file(cat)
        assert not report.ok
        assert any("driver" in i.message.lower() for i in report.errors)

    def test_missing_urlpath_is_error(self, tmp_path: Path):
        source = _minimal_zarr_source(str(tmp_path / "x.zarr"))
        del source["args"]["urlpath"]
        cat = _write(tmp_path / "c.yaml", {"sources": {"s": source}})
        report = check_catalog_file(cat)
        assert not report.ok
        assert any("urlpath" in i.message.lower() for i in report.errors)

    def test_nonexistent_urlpath_is_error(self, tmp_path: Path):
        source = _minimal_zarr_source("/nonexistent/path/data.zarr")
        cat = _write(tmp_path / "c.yaml", {"sources": {"s": source}})
        report = check_catalog_file(cat)
        assert not report.ok
        msgs = " ".join(i.message.lower() for i in report.errors)
        assert "not found" in msgs or "does not exist" in msgs

    def test_glob_urlpath_with_no_matches_is_error(self, tmp_path: Path):
        source = _minimal_zarr_source(str(tmp_path / "*.zarr"))
        cat = _write(tmp_path / "c.yaml", {"sources": {"s": source}})
        report = check_catalog_file(cat)
        assert not report.ok

    def test_glob_urlpath_with_matches_is_ok(self, tmp_path: Path):
        d1 = tmp_path / "a.zarr"
        d1.mkdir()
        d2 = tmp_path / "b.zarr"
        d2.mkdir()
        source = _minimal_zarr_source(str(tmp_path / "*.zarr"))
        cat = _write(tmp_path / "c.yaml", {"sources": {"s": source}})
        report = check_catalog_file(cat)
        assert report.ok, report.format_text()

    def test_urlpath_as_list_checks_each(self, tmp_path: Path):
        d1 = tmp_path / "a.zarr"
        d1.mkdir()
        source = _minimal_zarr_source([str(d1), "/nonexistent/b.zarr"])
        cat = _write(tmp_path / "c.yaml", {"sources": {"s": source}})
        report = check_catalog_file(cat)
        assert not report.ok
        # The good path should not be flagged, only the bad one.
        msgs = " ".join(i.message.lower() for i in report.errors)
        assert "nonexistent" in msgs

    def test_missing_metadata_id_is_error(self, tmp_path: Path):
        source = _minimal_zarr_source(str(tmp_path / "x.zarr"))
        (tmp_path / "x.zarr").mkdir()
        del source["metadata"]["id"]
        cat = _write(tmp_path / "c.yaml", {"sources": {"s": source}})
        report = check_catalog_file(cat)
        assert not report.ok
        assert any("id" in i.message.lower() for i in report.errors)

    def test_missing_metadata_description_is_error(self, tmp_path: Path):
        source = _minimal_zarr_source(str(tmp_path / "x.zarr"))
        (tmp_path / "x.zarr").mkdir()
        del source["metadata"]["description"]
        cat = _write(tmp_path / "c.yaml", {"sources": {"s": source}})
        report = check_catalog_file(cat)
        assert not report.ok
        assert any("description" in i.message.lower() for i in report.errors)


# ---------------------------------------------------------------------------
# Multi-error collection (the headline feature)
# ---------------------------------------------------------------------------
class TestCollectAllErrors:
    def test_reports_all_problems_at_once(self, tmp_path: Path):
        """Two broken sources -> two errors in one report, not just the first."""
        cat = _write(
            tmp_path / "c.yaml",
            {
                "sources": {
                    "bad_one": {
                        "driver": "zarr",
                        "args": {"urlpath": "/nonexistent/a.zarr"},
                        "metadata": {"id": "a", "description": "a"},
                    },
                    "bad_two": {
                        "driver": "zarr",
                        "args": {"urlpath": "/nonexistent/b.zarr"},
                        "metadata": {"id": "b", "description": "b"},
                    },
                }
            },
        )
        report = check_catalog_file(cat)
        assert not report.ok
        # Both bad sources should appear in the issue list.
        sources_mentioned = {i.source for i in report.errors}
        assert any("bad_one" in s for s in sources_mentioned)
        assert any("bad_two" in s for s in sources_mentioned)

    def test_source_filter_restricts_checks(self, tmp_path: Path):
        """When a specific source is requested, do not flag the others."""
        good_dir = tmp_path / "good.zarr"
        good_dir.mkdir()
        cat = _write(
            tmp_path / "c.yaml",
            {
                "sources": {
                    "good": _minimal_zarr_source(str(good_dir)),
                    "bad": _minimal_zarr_source("/nonexistent/x.zarr"),
                }
            },
        )
        report = check_catalog_file(cat, only_sources=["good"])
        assert report.ok, report.format_text()


# ---------------------------------------------------------------------------
# Generator-kind specific: raster needs asset_map, vector needs single path
# ---------------------------------------------------------------------------
class TestGeneratorSpecificChecks:
    def test_raster_missing_asset_map_is_error(self, tmp_path: Path):
        tif = tmp_path / "a.tif"
        tif.write_bytes(b"")  # presence only; preflight doesn't open it
        source = {
            "driver": "raster",
            "args": {"urlpath": str(tmp_path / "*.tif")},
            "metadata": {"id": "r", "description": "r", "generator": "raster"},
        }
        cat = _write(
            tmp_path / "c.yaml",
            {"metadata": {"generator": "raster"}, "sources": {"r": source}},
        )
        report = check_catalog_file(cat)
        assert not report.ok
        assert any("asset_map" in i.message.lower() for i in report.errors)
