"""STAC generator for raster (GeoTIFF / COG) sources.

Reads a catalog YAML entry whose ``args`` declare a directory glob of GeoTIFFs
and an ``asset_map`` describing per-file metadata. Each input file becomes one
STAC Item with a single ``data`` asset (the COG or original GeoTIFF).

Catalog shape expected (see ``config/catalogs/template/template_geotiff.yaml``):

.. code-block:: yaml

    metadata:
      generator: raster
    sources:
      my_dataset:
        driver: raster
        args:
          urlpath: "/path/to/data/*.tif"
          skip_cog: false
          thumbnail_asset_key: slope
          asset_map:
            slope_unit:
              key: slope
              title: "Slope Gradient"
              description: "..."
        metadata:
          id: my_dataset
          description: "..."
          ...
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
from .raster_utils import (
    COG_MIME,
    PROJ_EXT,
    TIFF_MIME,
    bbox_to_polygon,
    tiff_info,
    tiff_to_cog,
)
from .thumbnails import generate_raster_thumbnail


class RasterSource:
    """Lightweight container holding raster catalog entry + resolved per-asset info."""

    def __init__(self, source_name: str, entry: Dict[str, Any], catalog_metadata: Dict[str, Any]) -> None:
        self.source_name = source_name
        self.entry = entry
        self.catalog_metadata = catalog_metadata
        self.metadata: Dict[str, Any] = dict(entry.get("metadata") or {})
        args = entry.get("args") or {}
        self.urlpath: str = str(args.get("urlpath", ""))
        self.skip_cog: bool = bool(args.get("skip_cog", False))
        self.thumbnail_asset_key: Optional[str] = args.get("thumbnail_asset_key")
        self.asset_map: Dict[str, Dict[str, Any]] = dict(args.get("asset_map") or {})
        # Resolved later by IntakeRasterGenerator.load_source
        self.asset_infos: Dict[str, Dict[str, Any]] = {}


def _datetime_from_meta(meta: Dict[str, Any]) -> _dt.datetime:
    """Pick a datetime for the STAC item(s).

    Priority: ``meta['datetime']`` (ISO string) > today (UTC).
    """
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


class IntakeRasterGenerator(StacGenerator):
    """Generator for GeoTIFF/COG sources.

    Parameters
    ----------
    output_dir:
        Directory to write ``collection.json`` and ``items/`` into.
    catalog_path:
        Path to the catalog YAML file.
    catalog_metadata:
        Top-level catalog metadata dict (e.g. ``catalog_name``).
    """

    def __init__(
        self,
        output_dir: Path,
        catalog_path: Path,
        catalog_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(output_dir)
        self.catalog_path = catalog_path
        self.catalog_metadata = catalog_metadata or {}

    # ------------------------------------------------------------------
    # Catalog loading
    # ------------------------------------------------------------------
    def _read_catalog(self) -> Dict[str, Any]:
        with open(self.catalog_path, "r", encoding="utf-8") as fp:
            return yaml.safe_load(fp) or {}

    def load_source(self, source_name: str) -> RasterSource:
        catalog = self._read_catalog()
        sources = catalog.get("sources") or {}
        if source_name not in sources:
            raise ValueError(f"Source '{source_name}' not found in {self.catalog_path}")
        # Merge top-level catalog metadata once for downstream use
        cat_meta = catalog.get("metadata") or {}
        merged_cat_meta = {**self.catalog_metadata, **cat_meta}
        source = RasterSource(source_name, sources[source_name], merged_cat_meta)

        # Resolve files: support both glob and directory paths
        urlpath = source.urlpath
        if not urlpath:
            raise ValueError(f"Source '{source_name}' missing args.urlpath")

        url = Path(urlpath).expanduser()
        if any(ch in str(url) for ch in ("*", "?", "[")):
            files = {p.stem: p for p in sorted(Path(str(url.parent)).glob(url.name))}
        elif url.is_dir():
            files = {p.stem: p for p in sorted(url.glob("*.tif"))}
        else:
            files = {url.stem: url}
        logger.info(f"[{source_name}] Found {len(files)} candidate TIFF files under {urlpath}")

        # Build per-asset info
        items_dir = self.items_dir
        asset_infos: Dict[str, Dict[str, Any]] = {}
        for stem, entry in source.asset_map.items():
            src = files.get(stem)
            if src is None:
                logger.warning(f"[{source_name}] Missing TIFF for asset_map key '{stem}', skipping")
                continue
            asset_key = entry.get("key", stem)
            cog_path = items_dir / f"{asset_key}.cog.tif"
            if source.skip_cog:
                info = tiff_info(src)
                file_path = src
                mime = TIFF_MIME
            else:
                tiff_to_cog(src, cog_path)
                info = tiff_info(cog_path)
                file_path = cog_path
                mime = COG_MIME
            asset_infos[asset_key] = {
                "file_path": file_path,
                "info": info,
                "title": entry.get("title", asset_key),
                "description": entry.get("description", ""),
                "mime": mime,
            }
        source.asset_infos = asset_infos
        return source

    def extract_metadata(self, source: RasterSource) -> Dict[str, Any]:
        meta = dict(source.metadata)
        # Default ID to source_name if absent
        if "id" not in meta:
            meta["id"] = source.source_name
        # Inherit group_id / group_title / group_description from catalog top-level
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

    def get_dataset(self, source: RasterSource) -> Optional[xr.Dataset]:
        return None

    # ------------------------------------------------------------------
    # Extent / enrichment overrides
    # ------------------------------------------------------------------
    def _compute_extent(
        self,
        source: Optional[RasterSource],
        meta: Dict[str, Any],
        ds: Optional[xr.Dataset],
    ) -> pystac.Extent:
        if source is None or not source.asset_infos:
            raise ValueError("Raster source has no resolved asset_infos; cannot compute extent.")
        bboxes = [info["info"]["bbox_wgs84"] for info in source.asset_infos.values()]
        west = min(b[0] for b in bboxes)
        south = min(b[1] for b in bboxes)
        east = max(b[2] for b in bboxes)
        north = max(b[3] for b in bboxes)
        merged = [west, south, east, north]
        dt = _datetime_from_meta(meta)
        return pystac.Extent(
            spatial=pystac.SpatialExtent(bboxes=[merged]),
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

    # ------------------------------------------------------------------
    # Item iteration
    # ------------------------------------------------------------------
    def _iter_items(
        self,
        source: RasterSource,
        meta: Dict[str, Any],
        ds: Optional[xr.Dataset],
        collection: Optional[pystac.Collection] = None,
    ) -> Iterable[pystac.Item]:
        collection_id = str(meta.get("id", source.source_name))
        dt = _datetime_from_meta(meta)

        for asset_key, info_bundle in source.asset_infos.items():
            file_path: Path = info_bundle["file_path"]
            info = info_bundle["info"]
            bbox = info["bbox_wgs84"]
            proj_code = info["proj_code"]

            item_id = f"{collection_id}_{asset_key}"
            item = pystac.Item(
                id=item_id,
                geometry=bbox_to_polygon(bbox),
                bbox=bbox,
                datetime=dt,
                properties={
                    "title": info_bundle["title"],
                    "description": info_bundle["description"],
                    "proj:code": proj_code,
                    "processing:level": meta.get("processing_level", "Analysis Ready"),
                    "platform": meta.get("platform", "unknown"),
                },
                stac_extensions=[PROJ_EXT],
                collection=collection_id,
            )
            item.set_self_href(str(self.items_dir / f"{item_id}.json"))
            item.add_asset(
                "data",
                pystac.Asset(
                    href=f"./{file_path.name}",
                    media_type=info_bundle["mime"],
                    title=info_bundle["title"],
                    roles=["data"],
                    extra_fields={
                        "description": info_bundle["description"],
                        "data_type": info["dtype"],
                        "proj:shape": info["shape"],
                        "proj:transform": info["transform"],
                        "nodata": info["nodata"],
                    },
                ),
            )
            item.add_link(pystac.Link(rel="root", target="../collection.json", media_type="application/json"))
            item.add_link(pystac.Link(rel="collection", target="../collection.json", media_type="application/json"))
            item.add_link(pystac.Link(rel="parent", target="../collection.json", media_type="application/json"))
            yield item

    def _finalize_collection(
        self,
        collection: pystac.Collection,
        source: RasterSource,
        meta: Dict[str, Any],
        ds: Optional[xr.Dataset],
    ) -> None:
        """Attach a raster-derived thumbnail to the collection."""
        if not source.asset_infos:
            return
        thumbnail_key = source.thumbnail_asset_key
        chosen_key = thumbnail_key if thumbnail_key in source.asset_infos else next(iter(source.asset_infos))
        file_path: Path = source.asset_infos[chosen_key]["file_path"]
        thumb = generate_raster_thumbnail(file_path, collection.id, self.items_dir)
        if thumb:
            collection.add_asset("thumbnail", pystac.Asset.from_dict(thumb))
