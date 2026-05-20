"""STAC generator for vector (Shapefile / GeoParquet) sources.

Reads a catalog YAML entry whose ``args.urlpath`` points at a Shapefile or
GeoParquet file. Produces a single-item collection whose item footprint is
the actual feature union (reprojected to EPSG:4326).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pystac
import xarray as xr
import yaml
from loguru import logger

from .base import StacGenerator
from .raster_utils import GEOPARQUET_MIME, PROJ_EXT, shapefile_to_geoparquet, vector_bbox_wgs84


class VectorSource:
    def __init__(self, source_name: str, entry: Dict[str, Any], catalog_metadata: Dict[str, Any]) -> None:
        self.source_name = source_name
        self.entry = entry
        self.catalog_metadata = catalog_metadata
        self.metadata: Dict[str, Any] = dict(entry.get("metadata") or {})
        args = entry.get("args") or {}
        self.urlpath: str = str(args.get("urlpath", ""))
        self.convert_to_parquet: bool = bool(args.get("convert_to_parquet", True))
        # Resolved
        self.src_path: Optional[Path] = None
        self.parquet_path: Optional[Path] = None
        self.bbox_wgs84: Optional[List[float]] = None
        self.geometry: Optional[Dict[str, Any]] = None
        self.src_crs_code: Optional[str] = None


def _datetime_from_meta(meta: Dict[str, Any]) -> _dt.datetime:
    raw = meta.get("datetime")
    if raw:
        if isinstance(raw, _dt.datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=_dt.timezone.utc)
        try:
            parsed = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)
        except Exception:
            logger.warning(f"Could not parse datetime '{raw}', falling back to now.")
    return _dt.datetime.now(_dt.timezone.utc)


class IntakeVectorGenerator(StacGenerator):
    """Generator for Shapefile / GeoParquet sources."""

    def __init__(
        self,
        output_dir: Path,
        catalog_path: Path,
        catalog_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(output_dir)
        self.catalog_path = catalog_path
        self.catalog_metadata = catalog_metadata or {}

    def _read_catalog(self) -> Dict[str, Any]:
        with open(self.catalog_path, "r", encoding="utf-8") as fp:
            return yaml.safe_load(fp) or {}

    def load_source(self, source_name: str) -> VectorSource:
        catalog = self._read_catalog()
        sources = catalog.get("sources") or {}
        if source_name not in sources:
            raise ValueError(f"Source '{source_name}' not found in {self.catalog_path}")
        cat_meta = catalog.get("metadata") or {}
        merged_cat_meta = {**self.catalog_metadata, **cat_meta}
        source = VectorSource(source_name, sources[source_name], merged_cat_meta)

        if not source.urlpath:
            raise ValueError(f"Source '{source_name}' missing args.urlpath")

        src_path = Path(source.urlpath).expanduser()
        if not src_path.exists():
            raise FileNotFoundError(f"Vector source path does not exist: {src_path}")
        source.src_path = src_path

        # Compute bbox/geometry/CRS up-front (does NOT write to items_dir, which
        # base.generate() will clean before iteration).
        bbox, geom = vector_bbox_wgs84(src_path)
        source.bbox_wgs84 = bbox
        source.geometry = geom

        try:
            import geopandas as gpd
            if src_path.suffix.lower() == ".parquet":
                gdf = gpd.read_parquet(str(src_path))
            else:
                gdf = gpd.read_file(str(src_path))
            if gdf.crs is not None:
                epsg = gdf.crs.to_epsg()
                source.src_crs_code = f"EPSG:{epsg}" if epsg else gdf.crs.to_wkt()
        except Exception as e:
            logger.warning(f"Could not extract vector CRS for {src_path}: {e}")

        return source

    def extract_metadata(self, source: VectorSource) -> Dict[str, Any]:
        meta = dict(source.metadata)
        if "id" not in meta:
            meta["id"] = source.source_name
        cat_meta = source.catalog_metadata
        if "group_id" not in meta:
            if "group_id" in cat_meta:
                meta["group_id"] = cat_meta["group_id"]
            elif "catalog_name" in cat_meta:
                raw = cat_meta["catalog_name"]
                meta["group_id"] = raw.strip().lower().replace(" ", "_").replace("-", "_")
                meta["group_title"] = raw.strip()
            if "description" in cat_meta and "group_description" not in meta:
                meta["group_description"] = cat_meta["description"]
        if "group_keywords" not in meta and "catalogs_keywords" in cat_meta:
            meta["group_keywords"] = cat_meta["catalogs_keywords"]
        return meta

    def get_dataset(self, source: VectorSource) -> Optional[xr.Dataset]:
        return None

    def _compute_extent(
        self,
        source: Optional[VectorSource],
        meta: Dict[str, Any],
        ds: Optional[xr.Dataset],
    ) -> pystac.Extent:
        if source is None or source.bbox_wgs84 is None:
            raise ValueError("Vector source has no resolved bbox; cannot compute extent.")
        dt = _datetime_from_meta(meta)
        return pystac.Extent(
            spatial=pystac.SpatialExtent(bboxes=[source.bbox_wgs84]),
            temporal=pystac.TemporalExtent(intervals=[[dt, None]]),
        )

    def _enrich_collection_metadata(
        self,
        collection: pystac.Collection,
        ds: Optional[xr.Dataset],
    ) -> pystac.Collection:
        if PROJ_EXT not in collection.stac_extensions:
            collection.stac_extensions.append(PROJ_EXT)
        return collection

    def _enrich_item_metadata(
        self,
        item: pystac.Item,
        ds: Optional[xr.Dataset],
    ) -> pystac.Item:
        if PROJ_EXT not in item.stac_extensions:
            item.stac_extensions.append(PROJ_EXT)
        return item

    def _iter_items(
        self,
        source: VectorSource,
        meta: Dict[str, Any],
        ds: Optional[xr.Dataset],
        collection: Optional[pystac.Collection] = None,
    ) -> Iterable[pystac.Item]:
        collection_id = str(meta.get("id", source.source_name))
        dt = _datetime_from_meta(meta)
        item_id = f"{collection_id}_aoi"
        assert source.src_path is not None
        assert source.bbox_wgs84 is not None
        assert source.geometry is not None

        # Materialize the parquet next to the item now that items_dir is clean.
        src_path = source.src_path
        suffix = src_path.suffix.lower()
        if suffix == ".parquet":
            source.parquet_path = src_path
        elif source.convert_to_parquet:
            out_name = src_path.stem.replace(" ", "_") + ".parquet"
            target = self.items_dir / out_name
            shapefile_to_geoparquet(src_path, target)
            source.parquet_path = target
        else:
            source.parquet_path = src_path

        properties: Dict[str, Any] = {
            "title": meta.get("title", item_id),
            "description": meta.get("description", ""),
            "processing:level": meta.get("processing_level", "Analysis Ready"),
        }
        if source.src_crs_code:
            properties["proj:code"] = source.src_crs_code

        item = pystac.Item(
            id=item_id,
            geometry=source.geometry,
            bbox=source.bbox_wgs84,
            datetime=dt,
            properties=properties,
            stac_extensions=[PROJ_EXT],
            collection=collection_id,
        )
        item.set_self_href(str(self.items_dir / f"{item_id}.json"))
        item.add_asset(
            "data",
            pystac.Asset(
                href=f"./{source.parquet_path.name}",
                media_type=GEOPARQUET_MIME,
                title=meta.get("title", item_id),
                roles=["data"],
                extra_fields={"proj:code": source.src_crs_code} if source.src_crs_code else {},
            ),
        )
        item.add_link(pystac.Link(rel="root", target="../collection.json", media_type="application/json"))
        item.add_link(pystac.Link(rel="collection", target="../collection.json", media_type="application/json"))
        item.add_link(pystac.Link(rel="parent", target="../collection.json", media_type="application/json"))
        yield item
