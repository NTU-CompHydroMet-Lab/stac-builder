"""Tests for the describe subcommand's core logic.

``describe`` is a planning / dry-run tool. It parses a catalog YAML and
returns a structured summary of what a build *would* produce, without ever
touching the data files or generating any STAC JSON.

Use case: you have written a catalog YAML for a dataset whose zarr is still
being processed (e.g. Himawari). You want to (a) verify the YAML structure
and metadata fields, (b) preview what STAC paths/ids will be produced, and
(c) know which urlpaths are not yet present so you can monitor data
production. ``build`` will still refuse to run until data is present —
``describe`` is the doc/preview layer, not a stub-output mode.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.core.describe import (
    CatalogDescription,
    SourceDescription,
    describe_catalog_file,
)


def _write(path: Path, payload: dict) -> Path:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _himawari_like_source(urlpath, source_id="himawari_clp_cwa_2016"):
    return {
        "driver": "zarr",
        "args": {"urlpath": urlpath, "consolidated": False},
        "metadata": {
            "id": source_id,
            "description": "Himawari L2 cloud products",
            "thumbnail_variable": "cldt",
            "thumbnail_datetime": "2016-07-08T06:00:00",
        },
    }


class TestCatalogDescription:
    def test_missing_file_returns_empty_with_error(self, tmp_path: Path):
        result = describe_catalog_file(tmp_path / "nope.yaml")
        assert isinstance(result, CatalogDescription)
        assert result.catalog_path == tmp_path / "nope.yaml"
        assert result.sources == []
        assert result.errors != []
        assert any("not found" in e.lower() for e in result.errors)

    def test_invalid_yaml_returns_errors(self, tmp_path: Path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("this: is: : not yaml\n[\n", encoding="utf-8")
        result = describe_catalog_file(bad)
        assert result.sources == []
        assert any("yaml" in e.lower() for e in result.errors)

    def test_group_id_derived_from_catalog_name(self, tmp_path: Path):
        data = tmp_path / "data.zarr"
        data.mkdir()
        cat = _write(
            tmp_path / "c.yaml",
            {
                "metadata": {"catalog_name": "HIMAWARI", "description": "..."},
                "sources": {"s1": _himawari_like_source(str(data))},
            },
        )
        result = describe_catalog_file(cat)
        assert result.group_id == "himawari"
        # group_title preserves original casing
        assert result.group_title == "HIMAWARI"


class TestSourceDescription:
    def test_basic_fields_populated(self, tmp_path: Path):
        data = tmp_path / "data.zarr"
        data.mkdir()
        cat = _write(
            tmp_path / "c.yaml",
            {"sources": {"s1": _himawari_like_source(str(data))}},
        )
        result = describe_catalog_file(cat)
        assert len(result.sources) == 1
        src = result.sources[0]
        assert isinstance(src, SourceDescription)
        assert src.source_id == "s1"
        assert src.driver == "zarr"
        assert src.collection_id == "himawari_clp_cwa_2016"
        assert src.generator_kind == "xarray"

    def test_relative_output_path_is_useful(self, tmp_path: Path):
        data = tmp_path / "data.zarr"
        data.mkdir()
        cat = _write(
            tmp_path / "c.yaml",
            {
                "metadata": {"catalog_name": "HIMAWARI"},
                "sources": {"s1": _himawari_like_source(str(data))},
            },
        )
        result = describe_catalog_file(cat)
        src = result.sources[0]
        # The user wants to know "where will this end up under stac_output/"
        assert "himawari" in src.expected_output_path
        assert "himawari_clp_cwa_2016" in src.expected_output_path

    def test_thumbnail_fields_surfaced(self, tmp_path: Path):
        data = tmp_path / "data.zarr"
        data.mkdir()
        cat = _write(
            tmp_path / "c.yaml",
            {"sources": {"s1": _himawari_like_source(str(data))}},
        )
        result = describe_catalog_file(cat)
        src = result.sources[0]
        assert src.thumbnail_variable == "cldt"
        assert src.thumbnail_datetime == "2016-07-08T06:00:00"


class TestUrlpathDataPresence:
    """Existing data -> no warning; missing data -> warning, never an error."""

    def test_existing_string_path_reports_present(self, tmp_path: Path):
        data = tmp_path / "data.zarr"
        data.mkdir()
        cat = _write(
            tmp_path / "c.yaml",
            {"sources": {"s1": _himawari_like_source(str(data))}},
        )
        result = describe_catalog_file(cat)
        src = result.sources[0]
        assert src.urlpaths_present == 1
        assert src.urlpaths_missing == []

    def test_missing_string_path_listed_in_missing(self, tmp_path: Path):
        cat = _write(
            tmp_path / "c.yaml",
            {"sources": {"s1": _himawari_like_source("/nonexistent/x.zarr")}},
        )
        result = describe_catalog_file(cat)
        src = result.sources[0]
        assert src.urlpaths_present == 0
        assert src.urlpaths_missing == ["/nonexistent/x.zarr"]
        # describe never returns errors for missing data — that's a build concern.
        assert result.errors == []

    def test_glob_no_match_listed_in_missing(self, tmp_path: Path):
        cat = _write(
            tmp_path / "c.yaml",
            {"sources": {"s1": _himawari_like_source(str(tmp_path / "*.zarr"))}},
        )
        result = describe_catalog_file(cat)
        src = result.sources[0]
        assert src.urlpaths_present == 0
        assert len(src.urlpaths_missing) == 1

    def test_glob_partial_match_counts_present(self, tmp_path: Path):
        (tmp_path / "a.zarr").mkdir()
        (tmp_path / "b.zarr").mkdir()
        cat = _write(
            tmp_path / "c.yaml",
            {"sources": {"s1": _himawari_like_source(str(tmp_path / "*.zarr"))}},
        )
        result = describe_catalog_file(cat)
        src = result.sources[0]
        assert src.urlpaths_present == 2
        assert src.urlpaths_missing == []

    def test_list_partial_present(self, tmp_path: Path):
        present = tmp_path / "a.zarr"
        present.mkdir()
        cat = _write(
            tmp_path / "c.yaml",
            {
                "sources": {
                    "s1": _himawari_like_source(
                        [str(present), "/nonexistent/b.zarr", "/nonexistent/c.zarr"]
                    )
                }
            },
        )
        result = describe_catalog_file(cat)
        src = result.sources[0]
        assert src.urlpaths_present == 1
        assert len(src.urlpaths_missing) == 2


class TestMissingMetadataIsHardError:
    """If id/description are missing the YAML is unusable; describe must say so."""

    def test_missing_id_is_error_not_warning(self, tmp_path: Path):
        bad_src = _himawari_like_source(str(tmp_path / "x.zarr"))
        (tmp_path / "x.zarr").mkdir()
        del bad_src["metadata"]["id"]
        cat = _write(tmp_path / "c.yaml", {"sources": {"s1": bad_src}})
        result = describe_catalog_file(cat)
        assert result.errors != []
        assert any("id" in e.lower() for e in result.errors)
