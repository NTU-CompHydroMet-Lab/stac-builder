"""Tests for urlpath normalization in IntakeXarray.

Regression coverage for a pre-existing bug carried over from
wangup-stac-manager: when ``args.urlpath`` is a YAML list (one zarr per year),
the old code stringified the whole list as the "source path", producing
``source_path: ".../['/path/2019.zarr', '/path/2020.zarr', ...]"`` and symlinks
named ``foo-2019.zarr']``. The fix is to normalize list-form urlpaths down to
a single representative path; that representative is what gets recorded as
``source_path`` and used to build the data asset symlink.
"""

from __future__ import annotations

from src.generator.intake_xarray import normalize_urlpath_to_single


class TestNormalizeUrlpath:
    def test_string_returned_unchanged(self):
        assert normalize_urlpath_to_single("/data/foo.zarr") == "/data/foo.zarr"

    def test_none_returns_none(self):
        assert normalize_urlpath_to_single(None) is None

    def test_empty_string_returns_none(self):
        assert normalize_urlpath_to_single("") is None

    def test_empty_list_returns_none(self):
        assert normalize_urlpath_to_single([]) is None

    def test_list_picks_first_entry(self):
        urlpaths = [
            "/data/era5_2019.zarr",
            "/data/era5_2020.zarr",
            "/data/era5_2021.zarr",
        ]
        assert normalize_urlpath_to_single(urlpaths) == "/data/era5_2019.zarr"

    def test_list_with_single_entry(self):
        assert normalize_urlpath_to_single(["/only/one.zarr"]) == "/only/one.zarr"

    def test_glob_string_returned_unchanged(self):
        # Globs are resolved later by intake itself; normalization should not touch them.
        assert normalize_urlpath_to_single("/data/*.zarr") == "/data/*.zarr"

    def test_no_list_repr_leaks_into_output(self):
        """The pre-existing bug: list became string '[\"...\", \"...\"]'. Guard against any future regression."""
        urlpaths = ["/data/a.zarr", "/data/b.zarr"]
        result = normalize_urlpath_to_single(urlpaths)
        assert "[" not in (result or "")
        assert "]" not in (result or "")
        assert "'" not in (result or "")

    def test_list_of_non_strings_coerces(self):
        # Pathlib paths or anything str-able should also work.
        from pathlib import Path
        result = normalize_urlpath_to_single([Path("/data/a.zarr"), Path("/data/b.zarr")])
        assert result == "/data/a.zarr"
