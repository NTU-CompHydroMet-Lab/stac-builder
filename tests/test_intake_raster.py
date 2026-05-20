"""Integration test for IntakeRasterGenerator using synthetic GeoTIFFs."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
import yaml
from rasterio.transform import from_origin

from src.generator.intake_raster import IntakeRasterGenerator


def _write_synthetic_geotiff(path: Path, epsg: int = 3826) -> None:
    data = (np.arange(100, dtype="float32") % 50).reshape(10, 10)
    transform = from_origin(250000.0, 2700000.0, 20.0, 20.0)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=10,
        width=10,
        count=1,
        dtype="float32",
        crs=f"EPSG:{epsg}",
        transform=transform,
        nodata=-9999.0,
    ) as dst:
        dst.write(data, 1)


@pytest.fixture
def raster_catalog(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "tiffs"
    data_dir.mkdir()
    _write_synthetic_geotiff(data_dir / "slope_unit.tif")
    _write_synthetic_geotiff(data_dir / "aspect_unit.tif")

    catalog = {
        "metadata": {
            "version": 1,
            "description": "Synthetic test catalog",
            "generator": "raster",
        },
        "sources": {
            "test_raster": {
                "driver": "raster",
                "args": {
                    "urlpath": f"{data_dir}/*.tif",
                    "skip_cog": True,  # Avoid COG round-trip for speed
                    "thumbnail_asset_key": "slope",
                    "asset_map": {
                        "slope_unit": {
                            "key": "slope",
                            "title": "Slope Gradient",
                            "description": "Slope in degrees.",
                        },
                        "aspect_unit": {
                            "key": "aspect",
                            "title": "Slope Aspect",
                            "description": "Aspect direction.",
                        },
                    },
                },
                "metadata": {
                    "id": "test_raster_v1",
                    "description": "Synthetic raster dataset for testing.",
                    "datetime": "2025-08-25T00:00:00Z",
                    "processing_level": "Analysis Ready",
                    "platform": "Synthetic",
                    "category": "TERRAIN",
                    "group_id": "test_group",
                    "group_title": "Test Group",
                    "group_description": "test group",
                    "providers": [
                        {"name": "Test Lab", "roles": ["host"], "url": "https://test.example/"}
                    ],
                },
            }
        },
    }
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(yaml.safe_dump(catalog))
    return catalog_path, tmp_path / "out"


def test_intake_raster_generates_two_items_one_per_asset(raster_catalog: tuple[Path, Path]) -> None:
    catalog_path, output_dir = raster_catalog
    gen = IntakeRasterGenerator(output_dir=output_dir / "test_raster", catalog_path=catalog_path)
    gen.generate(source_name="test_raster")

    items_dir = output_dir / "test_raster" / "items"
    item_files = sorted(items_dir.glob("test_raster_v1_*.json"))
    assert len(item_files) == 2, f"Expected 2 items, got {[p.name for p in item_files]}"

    item_ids = {json.loads(p.read_text())["id"] for p in item_files}
    assert item_ids == {"test_raster_v1_slope", "test_raster_v1_aspect"}


def test_intake_raster_collection_has_thumbnail(raster_catalog: tuple[Path, Path]) -> None:
    catalog_path, output_dir = raster_catalog
    gen = IntakeRasterGenerator(output_dir=output_dir / "test_raster", catalog_path=catalog_path)
    gen.generate(source_name="test_raster")

    collection_path = output_dir / "test_raster" / "collection.json"
    assert collection_path.exists()
    collection = json.loads(collection_path.read_text())
    assets = collection.get("assets") or {}
    assert "thumbnail" in assets
    assert assets["thumbnail"]["type"] == "image/png"


def test_intake_raster_item_has_proj_extension_and_bbox(raster_catalog: tuple[Path, Path]) -> None:
    catalog_path, output_dir = raster_catalog
    gen = IntakeRasterGenerator(output_dir=output_dir / "test_raster", catalog_path=catalog_path)
    gen.generate(source_name="test_raster")

    items_dir = output_dir / "test_raster" / "items"
    item_path = items_dir / "test_raster_v1_slope.json"
    item = json.loads(item_path.read_text())
    assert any("projection" in ext for ext in item.get("stac_extensions", []))
    assert item["properties"].get("proj:code") == "EPSG:3826"
    bbox = item["bbox"]
    assert len(bbox) == 4
    # bbox in Taiwan WGS84 range
    assert 119.0 <= bbox[0] <= 123.0
    assert 21.0 <= bbox[1] <= 26.0


def test_intake_raster_extent_unions_per_asset_bboxes(raster_catalog: tuple[Path, Path]) -> None:
    catalog_path, output_dir = raster_catalog
    gen = IntakeRasterGenerator(output_dir=output_dir / "test_raster", catalog_path=catalog_path)
    gen.generate(source_name="test_raster")
    collection = json.loads((output_dir / "test_raster" / "collection.json").read_text())
    spatial = collection["extent"]["spatial"]["bbox"]
    assert len(spatial) == 1
    bbox = spatial[0]
    assert bbox[0] < bbox[2]
    assert bbox[1] < bbox[3]
