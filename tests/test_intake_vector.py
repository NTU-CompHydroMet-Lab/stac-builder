"""Integration test for IntakeVectorGenerator using synthetic Shapefile."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pytest
import yaml
from shapely.geometry import Polygon

from src.generator.intake_vector import IntakeVectorGenerator


@pytest.fixture
def vector_catalog(tmp_path: Path) -> tuple[Path, Path]:
    polygon = Polygon([(250000, 2700000), (250200, 2700000), (250200, 2700200), (250000, 2700200)])
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[polygon], crs="EPSG:3826")
    shp_path = tmp_path / "aoi.shp"
    gdf.to_file(shp_path)

    catalog = {
        "metadata": {"version": 1, "description": "Synthetic vector catalog", "generator": "vector"},
        "sources": {
            "test_vector": {
                "driver": "vector",
                "args": {"urlpath": str(shp_path), "convert_to_parquet": True},
                "metadata": {
                    "id": "test_vector_v1",
                    "title": "Synthetic AOI",
                    "description": "Synthetic AOI for testing.",
                    "datetime": "2025-08-25T00:00:00Z",
                    "group_id": "test_group",
                    "group_title": "Test Group",
                    "providers": [{"name": "Test Lab", "roles": ["host"]}],
                },
            }
        },
    }
    catalog_path = tmp_path / "vector_catalog.yaml"
    catalog_path.write_text(yaml.safe_dump(catalog))
    return catalog_path, tmp_path / "out"


def test_intake_vector_generates_single_aoi_item(vector_catalog: tuple[Path, Path]) -> None:
    catalog_path, output_dir = vector_catalog
    gen = IntakeVectorGenerator(output_dir=output_dir / "test_vector", catalog_path=catalog_path)
    gen.generate(source_name="test_vector")

    items_dir = output_dir / "test_vector" / "items"
    items = sorted(items_dir.glob("*.json"))
    assert len(items) == 1
    item = json.loads(items[0].read_text())
    assert item["id"] == "test_vector_v1_aoi"
    assert any("projection" in ext for ext in item.get("stac_extensions", []))
    assert item["properties"].get("proj:code") == "EPSG:3826"


def test_intake_vector_writes_geoparquet_alongside_item(vector_catalog: tuple[Path, Path]) -> None:
    catalog_path, output_dir = vector_catalog
    gen = IntakeVectorGenerator(output_dir=output_dir / "test_vector", catalog_path=catalog_path)
    gen.generate(source_name="test_vector")
    items_dir = output_dir / "test_vector" / "items"
    parquet_files = list(items_dir.glob("*.parquet"))
    assert len(parquet_files) == 1
    gdf = gpd.read_parquet(str(parquet_files[0]))
    assert len(gdf) == 1
    assert gdf.crs.to_epsg() == 3826


def test_intake_vector_bbox_in_taiwan_range(vector_catalog: tuple[Path, Path]) -> None:
    catalog_path, output_dir = vector_catalog
    gen = IntakeVectorGenerator(output_dir=output_dir / "test_vector", catalog_path=catalog_path)
    gen.generate(source_name="test_vector")
    collection = json.loads((output_dir / "test_vector" / "collection.json").read_text())
    bbox = collection["extent"]["spatial"]["bbox"][0]
    assert 119.0 <= bbox[0] <= 123.0
    assert 21.0 <= bbox[1] <= 26.0
