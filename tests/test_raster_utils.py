"""Tests for src.generator.raster_utils — pure functions, synthetic data."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from src.generator.raster_utils import (
    bbox_to_polygon,
    reproject_bbox_to_wgs84,
    shapefile_to_geoparquet,
    tiff_info,
    tiff_to_cog,
    vector_bbox_wgs84,
)


def _write_synthetic_geotiff(
    path: Path,
    epsg: int = 3826,
    width: int = 10,
    height: int = 10,
    origin_x: float = 250000.0,
    origin_y: float = 2700000.0,
    pixel_size: float = 20.0,
    nodata: float = -9999.0,
) -> None:
    """Write a tiny single-band GeoTIFF with a known CRS / transform."""
    data = (np.arange(width * height, dtype="float32") % 100).reshape(height, width)
    transform = from_origin(origin_x, origin_y, pixel_size, pixel_size)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs=f"EPSG:{epsg}",
        transform=transform,
        nodata=nodata,
    ) as dst:
        dst.write(data, 1)


@pytest.fixture
def synthetic_tiff(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic.tif"
    _write_synthetic_geotiff(path)
    return path


def test_tiff_info_returns_expected_keys(synthetic_tiff: Path) -> None:
    info = tiff_info(synthetic_tiff)
    assert set(info.keys()) >= {
        "epsg",
        "proj_code",
        "transform",
        "shape",
        "dtype",
        "nodata",
        "bbox_wgs84",
    }
    assert info["epsg"] == 3826
    assert info["proj_code"] == "EPSG:3826"
    assert info["shape"] == [10, 10]
    assert info["dtype"] == "float32"
    assert info["nodata"] == pytest.approx(-9999.0)
    assert len(info["transform"]) == 6


def test_tiff_info_bbox_wgs84_in_taiwan_range(synthetic_tiff: Path) -> None:
    """TWD97 origin (250000, 2700000) should reproject into Taiwan's longitude/latitude range."""
    info = tiff_info(synthetic_tiff)
    bbox = info["bbox_wgs84"]
    assert len(bbox) == 4
    west, south, east, north = bbox
    assert west < east
    assert south < north
    # Taiwan is roughly between 119–123E, 21–26N.
    assert 119.0 <= west <= 123.0
    assert 119.0 <= east <= 123.0
    assert 21.0 <= south <= 26.0
    assert 21.0 <= north <= 26.0


def test_reproject_bbox_passthrough_for_4326() -> None:
    bounds = (10.0, 20.0, 30.0, 40.0)
    out = reproject_bbox_to_wgs84(bounds, "EPSG:4326")
    assert out == [10.0, 20.0, 30.0, 40.0]


def test_reproject_bbox_3826_to_4326_round_trip() -> None:
    # TWD97 origin used in the synthetic fixture.
    out = reproject_bbox_to_wgs84((250000.0, 2700000.0, 250200.0, 2700200.0), "EPSG:3826")
    west, south, east, north = out
    assert 119.0 <= west <= 123.0
    assert 21.0 <= south <= 26.0
    assert west < east
    assert south < north


def test_bbox_to_polygon_shape() -> None:
    poly = bbox_to_polygon([0.0, 1.0, 2.0, 3.0])
    assert poly["type"] == "Polygon"
    ring = poly["coordinates"][0]
    assert len(ring) == 5
    assert ring[0] == ring[-1]


def test_tiff_to_cog_creates_valid_cog(synthetic_tiff: Path, tmp_path: Path) -> None:
    cog_path = tmp_path / "out.cog.tif"
    tiff_to_cog(synthetic_tiff, cog_path)
    assert cog_path.exists()
    # Sanity: COG is readable and round-trips metadata.
    info = tiff_info(cog_path)
    assert info["shape"] == [10, 10]
    assert info["epsg"] == 3826


def _write_synthetic_shapefile(tmp_path: Path) -> Path:
    """Write a tiny polygon Shapefile in EPSG:3826."""
    import geopandas as gpd
    from shapely.geometry import Polygon

    polygon = Polygon([(250000, 2700000), (250200, 2700000), (250200, 2700200), (250000, 2700200)])
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[polygon], crs="EPSG:3826")
    shp_path = tmp_path / "aoi.shp"
    gdf.to_file(shp_path)
    return shp_path


def test_shapefile_to_geoparquet_roundtrip(tmp_path: Path) -> None:
    import geopandas as gpd

    shp_path = _write_synthetic_shapefile(tmp_path)
    out_path = tmp_path / "aoi.parquet"
    result = shapefile_to_geoparquet(shp_path, out_path)
    assert result == out_path
    gdf = gpd.read_parquet(str(out_path))
    assert len(gdf) == 1
    assert gdf.crs.to_epsg() == 3826


def test_shapefile_to_geoparquet_missing_returns_none(tmp_path: Path) -> None:
    out_path = tmp_path / "missing.parquet"
    assert shapefile_to_geoparquet(tmp_path / "nope.shp", out_path) is None


def test_vector_bbox_wgs84_returns_geometry_and_bbox(tmp_path: Path) -> None:
    shp_path = _write_synthetic_shapefile(tmp_path)
    parquet_path = tmp_path / "aoi.parquet"
    shapefile_to_geoparquet(shp_path, parquet_path)
    bbox, geom = vector_bbox_wgs84(parquet_path)
    assert geom["type"] in {"Polygon", "MultiPolygon"}
    west, south, east, north = bbox
    assert 119.0 <= west <= 123.0
    assert 21.0 <= south <= 26.0
    assert west < east
    assert south < north
