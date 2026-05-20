"""Unit tests for tabular-to-xarray conversion in IntakeXarrayGenerator."""

import pandas as pd
import numpy as np
import pytest
import xarray as xr

from src.generator.intake_xarray import IntakeXarrayGenerator


def _make_tidy_df(n_times=3, n_stations=2):
    """Build a minimal tidy DataFrame matching the expected parquet schema."""
    times = pd.date_range("2021-06-04", periods=n_times, freq="h", tz="UTC")
    rows = []
    for t in times:
        for s_idx in range(n_stations):
            rows.append({
                "time_utc": t,
                "station_id": f"S{s_idx:03d}",
                "simulated_depth_m": float(s_idx) + 0.1,
                "observed_depth_m": float(s_idx) + 0.2,
                "station_x": 300000.0 + s_idx * 100,
                "station_y": 2700000.0 + s_idx * 100,
                "crs": "EPSG:3826",
            })
    return pd.DataFrame(rows)


class TestTabularToXarray:
    def _convert(self, df: pd.DataFrame) -> xr.Dataset:
        gen = IntakeXarrayGenerator.__new__(IntakeXarrayGenerator)
        return gen._tabular_to_xarray(df)

    def test_basic_shape(self):
        df = _make_tidy_df(n_times=3, n_stations=2)
        ds = self._convert(df)
        assert "time" in ds.dims
        assert "station" in ds.dims
        assert ds.sizes["time"] == 3
        assert ds.sizes["station"] == 2

    def test_has_sim_and_obs(self):
        df = _make_tidy_df()
        ds = self._convert(df)
        assert "simulated_depth" in ds.data_vars
        assert "observed_depth" in ds.data_vars

    def test_station_coords_present(self):
        df = _make_tidy_df()
        ds = self._convert(df)
        assert "station_x" in ds
        assert "station_y" in ds

    def test_crs_attr(self):
        df = _make_tidy_df()
        ds = self._convert(df)
        assert ds.attrs["crs"] == "EPSG:3826"

    def test_missing_time_col_raises(self):
        df = _make_tidy_df().drop(columns=["time_utc"])
        with pytest.raises(ValueError, match="time"):
            self._convert(df)

    def test_missing_depth_cols_raises(self):
        df = _make_tidy_df().drop(columns=["simulated_depth_m", "observed_depth_m"])
        with pytest.raises(ValueError, match="depth"):
            self._convert(df)

    def test_obs_only(self):
        df = _make_tidy_df().drop(columns=["simulated_depth_m"])
        ds = self._convert(df)
        assert "observed_depth" in ds.data_vars
        assert "simulated_depth" not in ds.data_vars
