
from __future__ import annotations

import abc
import json
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
import warnings
from loguru import logger

import pandas as pd
import xarray as xr
import pystac

# Import refactored modules
from .utils import compute_extent, format_datetime, get_spatial_dims, compute_item_geometry
from .thumbnails import generate_thumbnail
from .assets import process_example_notebook, create_data_asset, create_supplemental_asset
from .model import DatasetMetadata


class StacGenerator(abc.ABC):
    """Base class that defines the workflow for generating a static STAC catalog.

    Concrete generators implement source loading and item iteration. Default
    item iteration assumes an ``xarray.Dataset`` with a ``time`` dimension and
    emits one item per calendar year, but subclasses are free to override
    ``_iter_items`` to yield items for non-time-indexed sources (e.g. raster,
    vector).
    """

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.collection_path = self.output_dir / "collection.json"
        self.items_dir = self.output_dir / "items"
        self.items_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Abstract API – concrete implementations must provide these
    # ------------------------------------------------------------------
    @abc.abstractmethod
    def load_source(self, source_name: str) -> object:
        """Return the raw source object (e.g. an Intake entry)."""

    @abc.abstractmethod
    def extract_metadata(self, source: object) -> Dict[str, object]:
        """Extract a dictionary of metadata from the source."""

    def get_dataset(self, source: object) -> Optional[xr.Dataset]:
        """Return an ``xarray.Dataset`` for the source, or ``None`` for non-xarray sources.

        xarray-backed subclasses MUST override this and return a real Dataset
        so that ``_load_or_create_collection`` can compute extents, generate
        thumbnails, and enrich metadata via xstac. Non-xarray subclasses (raster,
        vector) can return ``None`` and provide their own ``_compute_extent``
        / ``_iter_items`` overrides.
        """
        return None

    def _enrich_collection_metadata(self, collection: pystac.Collection, ds: Optional[xr.Dataset]) -> pystac.Collection:
        """Hook to enrich collection metadata. Default implementation does nothing."""
        return collection

    def _enrich_item_metadata(self, item: pystac.Item, ds: Optional[xr.Dataset]) -> pystac.Item:
        """Hook to enrich item metadata. Default implementation does nothing."""
        return item

    def _compute_extent(self, source: object, meta: Dict[str, object], ds: Optional[xr.Dataset]) -> pystac.Extent:
        """Compute the collection's spatial/temporal extent.

        Default implementation requires an ``xarray.Dataset`` (the original
        behaviour). Subclasses that work without xarray must override this.
        """
        if ds is None:
            raise NotImplementedError(
                "Default _compute_extent requires an xarray.Dataset. "
                "Override _compute_extent in subclasses that don't produce one."
            )
        return compute_extent(ds)


    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------
    def _load_or_create_collection(
        self,
        meta: Dict[str, object],
        ds: Optional[xr.Dataset],
        extent: Optional[pystac.Extent] = None,
        source: Optional[object] = None,
    ) -> pystac.Collection:
        """Load an existing collection.json or create a minimal one.

        ``meta`` is the metadata dict extracted from the source.
        ``ds`` is used to compute the real extent for xarray-based sources.
        ``extent`` may be pre-computed by subclasses that don't use xarray.
        """
        # Validate metadata using Pydantic
        # This will raise ValidationError if strict fields (id, description) are missing
        meta_obj = DatasetMetadata(**meta)

        if extent is None:
            extent = self._compute_extent(source, meta, ds)
        
        # Prepare Providers
        providers = []
        for p in meta_obj.providers:
            providers.append(pystac.Provider(
                name=p.name,
                roles=p.roles,
                url=p.url
            ))

        collection = pystac.Collection(
            id=meta_obj.id,
            title=meta_obj.get_title(),
            description=meta_obj.description,
            license=meta_obj.license,
            extent=extent,
            providers=providers,
            keywords=meta_obj.keywords
        )
        
        # --- Handle Summaries, Scientific Extension & Top-level Props ---
        collection.summaries = pystac.Summaries({
            "processing:level": meta_obj.processing_level,
            "platform": [meta_obj.platform],
            "category": [meta_obj.category],
        })
        
        sci_ext_url = "https://stac-extensions.github.io/scientific/v1.0.0/schema.json"
        
        # Handle extra fields (passed through by Pydantic)
        # We access the raw extra fields from the model
        extra_fields = {}
        for key, value in meta_obj.model_extra.items() if meta_obj.model_extra else {}:
            if key.startswith("sci:"):
                extra_fields[key] = value
                if sci_ext_url not in collection.stac_extensions:
                    collection.stac_extensions.append(sci_ext_url)
            if key in ["terms_of_use", "gee:terms_of_use"]:
                 extra_fields["terms_of_use"] = value
            if key in ["event_id", "event_title", "event_description"]:
                extra_fields[key] = value
        
        if extra_fields:
            collection.extra_fields.update(extra_fields)

        # --- Grouping ID (For Root Catalog) ---
        if meta_obj.group_id:
            collection.extra_fields["group_id"] = meta_obj.group_id

        if meta_obj.group_title:
             collection.extra_fields["group_title"] = meta_obj.group_title

        if meta_obj.group_description:
             collection.extra_fields["group_description"] = meta_obj.group_description

        if meta_obj.group_keywords:
             collection.extra_fields["group_keywords"] = meta_obj.group_keywords

        # --- Example Notebook Handling (Refactored) ---
        if meta_obj.example_notebook:
            logger.info(f"Processing example notebook: {meta_obj.example_notebook}")
            nb_asset = process_example_notebook(meta_obj.example_notebook, self.output_dir)
            if nb_asset:
                collection.add_asset("example_notebook", nb_asset)

        # --- Collection Thumbnail Generation (Refactored) ---
        if meta_obj.thumbnail_path:
            try:
                src_thumb = Path(meta_obj.thumbnail_path)
                if src_thumb.exists():
                    logger.info(f"Using provided thumbnail: {src_thumb}")
                    dest_thumb = self.items_dir / f"{collection.id}_thumb{src_thumb.suffix}"
                    shutil.copy2(src_thumb, dest_thumb)
                    
                    collection.add_asset("thumbnail", pystac.Asset(
                        href=f"./items/{dest_thumb.name}",
                        media_type="image/png" if src_thumb.suffix == ".png" else "image/jpeg",
                        roles=["thumbnail"],
                        title=f"Thumbnail for {collection.id}"
                    ))
                else:
                    logger.warning(f"Provided thumbnail path not found: {src_thumb}")
            except Exception as e:
                logger.error(f"Failed to copy provided thumbnail: {e}")
        if meta_obj.thumbnail_variable and ds is not None:
            thumb_asset_dict = generate_thumbnail(
                ds,
                collection.id,
                self.items_dir,
                target_var=meta_obj.thumbnail_variable,
                target_datetime=meta_obj.thumbnail_datetime
            )
            if thumb_asset_dict:
                 collection.add_asset("thumbnail", pystac.Asset.from_dict(thumb_asset_dict))

        # Subclass-specific enrichment (e.g. xstac datacube extension for xarray).
        # Subclasses that don't produce a Dataset can still extend metadata via
        # this hook (ds may be None).
        collection = self._enrich_collection_metadata(collection, ds)
        
        # [NEW] Patch cube:dimensions to match unwrapped BBox (if applicable)
        self._patch_cube_dimensions(collection)
        
        # VALIDATE
        try:
            collection.validate()
        except pystac.STACValidationError as e:
            logger.error(f"Collection validation failed: {e}")
            raise

        self.collection_path.parent.mkdir(parents=True, exist_ok=True)
        # Manually write using pystac structure
        self.collection_path.write_text(json.dumps(collection.to_dict(), indent=2))
        return collection
        
    def _patch_cube_dimensions(self, collection: pystac.Collection) -> None:
        """Patch cube:dimensions to show unwrapped extent (e.g. 80 to 200) if West > East."""
        if not collection.extent.spatial.bboxes:
            return
            
        bbox = collection.extent.spatial.bboxes[0]
        west, east = bbox[0], bbox[2]
        
        # Only patch if Antimeridian crossing (West > East) AND cube extension present
        if west > east and "cube:dimensions" in collection.extra_fields:
            cubes = collection.extra_fields["cube:dimensions"]
            # Find longitude dim
            for name in ["lon", "longitude", "x"]:
                if name in cubes:
                    if "extent" in cubes[name]:
                        logger.info(f"Patching cube:dimensions.{name} to unwrapped [{west}, {east + 360}]")
                        cubes[name]["extent"] = [west, east + 360]
                    break

    def _write_collection(self, collection: pystac.Collection) -> None:
        self.collection_path.write_text(json.dumps(collection.to_dict(), indent=2))

    def _write_item(self, item: pystac.Item) -> None:
        item_path = self.items_dir / f"{item.id}.json"
        
        # Ensure validation before writing
        try:
            item.validate()
        except pystac.STACValidationError as e:
            logger.warning(f"Item {item.id} validation failed: {e}")
            raise

        item_path.write_text(json.dumps(item.to_dict(), indent=2))

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def generate(self, source_name: str) -> None:
        """Generate the collection and items for ``source_name``."""
        source = self.load_source(source_name)
        meta = self.extract_metadata(source)
        ds = self.get_dataset(source)

        # Cleanup
        if self.items_dir.exists():
            logger.info(f"Cleaning existing items: {self.items_dir}")
            shutil.rmtree(self.items_dir)
        self.items_dir.mkdir(parents=True, exist_ok=True)

        if self.collection_path.exists():
            self.collection_path.unlink()

        # Ensure Collection ID
        if "id" not in meta:
            meta["id"] = source_name

        collection = self._load_or_create_collection(meta, ds, source=source)

        # Write collection immediately so items can validate against it
        self._write_collection(collection)

        for item in self._iter_items(source, meta, ds, collection):
            link = pystac.Link(
                rel="item",
                target=f"./items/{item.id}.json",
                media_type="application/json",
            )
            collection.add_link(link)
            self._write_item(item)
            logger.info(f"Generated Item: {item.id}")

        self._finalize_collection(collection, source, meta, ds)
        self._write_collection(collection)
        logger.info(f"Collection written to {self.collection_path}")

    def _finalize_collection(
        self,
        collection: pystac.Collection,
        source: object,
        meta: Dict[str, object],
        ds: Optional[xr.Dataset],
    ) -> None:
        """Hook called after all items are written, before the final collection write.

        Subclasses can attach collection-level assets here (e.g. thumbnails
        derived from items produced during iteration).
        """
        return None

    def _iter_items(
        self,
        source: object,
        meta: Dict[str, object],
        ds: Optional[xr.Dataset],
        collection: Optional[pystac.Collection] = None,
    ) -> Iterable[pystac.Item]:
        """Yield STAC items for the source.

        Default implementation: per-year split of an xarray Dataset's time
        dimension (legacy IntakeXarray behaviour). Subclasses that don't
        produce a time-indexed Dataset must override this.
        """
        if ds is None:
            raise NotImplementedError(
                "Default _iter_items requires an xarray.Dataset. "
                "Override _iter_items in subclasses that don't produce one."
            )
        years = sorted(set(pd.DatetimeIndex(ds.time.values).year))
        for yr in years:
            ds_year = ds.sel(time=str(yr))
            yield self._make_item(ds_year, yr, meta)

    # ------------------------------------------------------------------
    # Item creation
    # ------------------------------------------------------------------
    def _make_item(self, ds_year: xr.Dataset, year: int, meta: Dict[str, object], collection_obj: Optional[pystac.Collection] = None) -> pystac.Item:
        lon, lat = get_spatial_dims(ds_year)
        if lon is not None and lat is not None:
            lon_min = float(lon.min())
            lon_max = float(lon.max())
            lat_min = float(lat.min())
            lat_max = float(lat.max())
            bbox = [lon_min, lat_min, lon_max, lat_max]
        else:
            bbox = [-180.0, -90.0, 180.0, 90.0]
        
        # PySTAC requires datetime objects
        start = pd.to_datetime(ds_year.time.min().values)
        end = pd.to_datetime(ds_year.time.max().values)
        mid_time = start + (end - start) / 2
        
        collection_id = meta.get('id', 'generated_collection')
        item_id = f"{collection_id}-{year}"
        
        # Compute Geometry (using antimeridian if needed)
        geometry = compute_item_geometry(bbox)

        item = pystac.Item(
            id=item_id,
            geometry=geometry,
            bbox=bbox,
            datetime=mid_time,
            properties={
                "title": meta.get("collection_name", item_id),
                "start_datetime": format_datetime(start),
                "end_datetime": format_datetime(end),
                "platform": meta.get("platform", "unknown"),
                "variables": list(ds_year.data_vars),
            },
            collection=collection_id
        )
        
        # Set absolute self href for validation resolution
        item.set_self_href(str(self.items_dir / f"{item_id}.json"))

        # --- Dynamic Asset Generation (Refactored) ---
        source_path = ds_year.encoding.get("source")
        if source_path:
             # Store strict absolute path for user reference (e.g. Copy Path)
             item.properties["source_path"] = str(source_path)
             # create_data_asset returns pystac.Asset
             data_asset = create_data_asset(source_path, item_id, self.items_dir)
             item.add_asset("data", data_asset)
        else:
             # Fallback
             item.add_asset("zarr", pystac.Asset(
                href=f"./{item_id}.zarr",
                media_type="application/vnd+zarr",
                roles=["data"],
                title=f"Data Store for {item_id}"
             ))

        # Optional additional sidecar assets declared in metadata.
        supplemental_assets = meta.get("supplemental_assets", [])
        if isinstance(supplemental_assets, list):
            for entry in supplemental_assets:
                if not isinstance(entry, dict):
                    continue
                key = entry.get("key")
                path = entry.get("path")
                if not key or not path:
                    continue
                asset = create_supplemental_asset(
                    source_path=str(path),
                    item_id=item_id,
                    items_dir=self.items_dir,
                    asset_key=str(key),
                    media_type=entry.get("media_type"),
                    roles=entry.get("roles"),
                    title=entry.get("title"),
                )
                item.add_asset(str(key), asset)

        # Add Links (Collection, Root)
        # Using explicit links to match previous behavior for static browsing
        item.add_link(pystac.Link(rel="root", target="../collection.json", media_type="application/json"))
        item.add_link(pystac.Link(rel="collection", target="../collection.json", media_type="application/json"))
        item.add_link(pystac.Link(rel="parent", target="../collection.json", media_type="application/json"))

        item = self._enrich_item_metadata(item, ds_year)
        
        return item
