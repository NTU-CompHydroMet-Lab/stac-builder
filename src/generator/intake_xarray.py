"""Concrete STAC generator that reads an Intake‑Xarray source.

The class implements the abstract methods defined in ``src.generator.base.StacGenerator``
so that the high‑level generation logic can be reused for any source type (e.g. a
database, a remote API, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Any

import intake
import xarray as xr
import pandas as pd
import shutil
from loguru import logger

from .utils import format_datetime, get_spatial_dims
from .model import DatasetMetadata

import pystac
import xstac
import cf_xarray

from .base import StacGenerator


class IntakeXarrayGenerator(StacGenerator):
    """Generator for Intake-Xarray sources (NetCDF, Zarr).

    Parameters
    ----------
    output_dir:
        Directory where ``collection.json`` and ``items/`` will be written.
    catalog_path:
        Path to the Intake catalog YAML file.
    """

    def __init__(self, output_dir: Path, catalog_path: Path, catalog_metadata: Dict[str, Any] = None) -> None:
        super().__init__(output_dir)
        self.catalog_path = catalog_path
        self.catalog_metadata = catalog_metadata or {}

    # ------------------------------------------------------------------
    # Abstract API implementation
    # ------------------------------------------------------------------
    def process_source(self, source_name: str) -> None:
        """Process a single source from the catalog."""
        logger.info(f"Processing source: {source_name}")
        source = self.load_source(source_name)

    def load_source(self, source_name: str) -> Any:
        """Open the Intake catalog and return the requested source entry.

        Raises
        ------
        ValueError
        If ``source_name`` is not present in the catalog.
        """
        cat = intake.open_catalog(str(self.catalog_path))
        if source_name not in cat:
            raise ValueError(
                f"Source '{source_name}' not found in {self.catalog_path}"
            )
        return cat[source_name]

    def extract_metadata(self, source: Any) -> Dict[str, Any]:
        """Extract a flat metadata dictionary from an Intake source.

        ``source.metadata`` may be a dict or a nested dict containing a ``metadata``
        key – the method normalises both cases.
        """
        meta = {}
        # Try to get metadata from the source object itself
        if hasattr(source, "metadata") and source.metadata:
            meta = source.metadata
        # Try to get metadata from the catalog entry (common in Intake v2)
        elif hasattr(source, "_entry") and hasattr(source._entry, "_metadata"):
            meta = source._entry._metadata
        # Try to get metadata via describe()
        elif hasattr(source, "describe"):
            try:
                desc = source.describe()
                if "metadata" in desc:
                    meta = desc["metadata"]
            except Exception:
                pass

        if "metadata" in meta:
            meta = meta["metadata"]

        # Map 'collection_name' to 'title' if present (User Request)
        if "collection_name" in meta and "title" not in meta:
             meta["title"] = meta["collection_name"]

            
        # Merge Global Catalog Metadata (e.g. catalog_name -> group_id)
        # Priority: Source > Catalog
        if "group_id" not in meta:
            # Check for direct group_id in catalog
            if "group_id" in self.catalog_metadata:
                 meta["group_id"] = self.catalog_metadata["group_id"]
            # Check for catalog_name alias (User Request)
            elif "catalog_name" in self.catalog_metadata:
                 # Normalize: lowercase, replace spaces with underscores
                 raw_name = self.catalog_metadata["catalog_name"]
                 meta["group_id"] = raw_name.strip().lower().replace(" ", "_").replace("-", "_")
                 meta["group_title"] = raw_name.strip() # Preserve original casing for display
            
            # Extract Group Description from Catalog Metadata
            if "description" in self.catalog_metadata:
                meta["group_description"] = self.catalog_metadata["description"]

        # Merge Global Catalog Keywords
        if "group_keywords" not in meta:
            if "catalogs_keywords" in self.catalog_metadata:
                meta["group_keywords"] = self.catalog_metadata["catalogs_keywords"]

        return meta

    def get_dataset(self, source: Any) -> xr.Dataset:
        """Return a lazy ``xarray.Dataset`` for the source.

        ``to_dask`` is used so that the dataset is not fully loaded into memory
        until we slice it per year. 
        Auto-normalizes coordinates (e.g., 0-360 -> -180/180) to ensure STAC compliance.
        """
        # Fallback path for parquet-first tabular sources when intake parquet plugin is unavailable.
        parquet_path = self._extract_parquet_path(source)
        if parquet_path:
            try:
                df = pd.read_parquet(parquet_path)
                ds = self._tabular_to_xarray(df)
                ds.encoding["source"] = str(parquet_path)
                return ds
            except Exception as e:
                logger.warning(
                    f"Failed to read parquet directly ({parquet_path}): {e}. "
                    "Falling back to Intake source for metadata extraction."
                )

        try:
            obj = source.to_dask()
        except Exception as e:
            logger.warning(f"source.to_dask() failed ({type(source)}): {e}. Falling back to source.read().")
            obj = source.read()
        if isinstance(obj, xr.Dataset):
            ds = self._normalize_dataset(obj)
        elif self._is_tabular(obj):
            ds = self._tabular_to_xarray(obj)
        else:
            raise TypeError(f"Unsupported source object type for STAC generation: {type(obj)}")

        source_path = self._extract_source_path(source)
        # Even if we used fallback table loading, keep parquet as the primary asset if configured.
        if parquet_path:
            source_path = str(parquet_path)
        if source_path:
            ds.encoding["source"] = str(source_path)
        return ds

    def _is_tabular(self, obj: Any) -> bool:
        return hasattr(obj, "columns") and (hasattr(obj, "compute") or hasattr(obj, "to_pandas") or hasattr(obj, "to_dict"))

    def _extract_source_path(self, source: Any) -> str | None:
        """Best-effort extraction of underlying source filepath from Intake entry."""
        entry = getattr(source, "_entry", None)
        if entry is None:
            return None
        kwargs = getattr(entry, "_captured_init_kwargs", {}) or {}
        args = kwargs.get("args", {}) if isinstance(kwargs, dict) else {}
        urlpath = args.get("urlpath") if isinstance(args, dict) else None
        if not urlpath:
            return None
        path = Path(str(urlpath)).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        return str(path)

    def _extract_parquet_path(self, source: Any) -> Path | None:
        """Read parquet path from source metadata (`table_parquet_path`) if provided."""
        entry = getattr(source, "_entry", None)
        if entry is None:
            return None
        meta = getattr(entry, "_metadata", {}) or {}
        parquet_path = meta.get("table_parquet_path")
        if not parquet_path:
            return None
        path = Path(str(parquet_path)).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        return path

    def _tabular_to_xarray(self, table_obj: Any) -> xr.Dataset:
        """Convert a tabular source (pandas/dask dataframe) into a 2D time x station Dataset."""
        if hasattr(table_obj, "compute"):
            df = table_obj.compute()
        elif hasattr(table_obj, "to_pandas"):
            df = table_obj.to_pandas()
        else:
            df = pd.DataFrame(table_obj)

        if not isinstance(df, pd.DataFrame):
            raise TypeError(f"Tabular source is not a DataFrame: {type(df)}")

        time_col = "time_utc" if "time_utc" in df.columns else ("time" if "time" in df.columns else None)
        station_col = "station_id" if "station_id" in df.columns else ("station" if "station" in df.columns else None)
        if not time_col or not station_col:
            raise ValueError("Parquet table must include time/time_utc and station/station_id columns.")

        sim_col = "simulated_depth_m" if "simulated_depth_m" in df.columns else (
            "simulated_depth" if "simulated_depth" in df.columns else None
        )
        obs_col = "observed_depth_m" if "observed_depth_m" in df.columns else (
            "observed_depth" if "observed_depth" in df.columns else None
        )
        if not sim_col and not obs_col:
            raise ValueError("Parquet table must include at least simulated or observed depth column.")

        work = df.copy()
        work[time_col] = pd.to_datetime(work[time_col], utc=True, errors="coerce").dt.tz_localize(None)
        work[station_col] = work[station_col].astype(str)
        work = work.dropna(subset=[time_col, station_col]).sort_values([time_col, station_col])

        times = pd.Index(sorted(work[time_col].unique()), name="time")
        stations = pd.Index(sorted(work[station_col].unique()), name="station")

        data_vars: dict[str, tuple[tuple[str, str], Any]] = {}
        if sim_col:
            sim = (
                work.pivot_table(index=time_col, columns=station_col, values=sim_col, aggfunc="first")
                .reindex(index=times, columns=stations)
                .astype("float32")
            )
            data_vars["simulated_depth"] = (("time", "station"), sim.to_numpy())
        if obs_col:
            obs = (
                work.pivot_table(index=time_col, columns=station_col, values=obs_col, aggfunc="first")
                .reindex(index=times, columns=stations)
                .astype("float32")
            )
            data_vars["observed_depth"] = (("time", "station"), obs.to_numpy())

        ds = xr.Dataset(data_vars=data_vars, coords={"time": times.to_numpy(), "station": stations.to_numpy()})

        x_col = "station_x" if "station_x" in work.columns else ("x" if "x" in work.columns else None)
        y_col = "station_y" if "station_y" in work.columns else ("y" if "y" in work.columns else None)
        if x_col and y_col:
            st_meta = (
                work[[station_col, x_col, y_col]]
                .dropna(subset=[station_col])
                .drop_duplicates(subset=[station_col])
                .set_index(station_col)
                .reindex(stations)
            )
            ds["station_x"] = ("station", pd.to_numeric(st_meta[x_col], errors="coerce").to_numpy(dtype="float64"))
            ds["station_y"] = ("station", pd.to_numeric(st_meta[y_col], errors="coerce").to_numpy(dtype="float64"))
            ds["station_x"].attrs["units"] = "m"
            ds["station_y"].attrs["units"] = "m"
            ds["station_x"].attrs["standard_name"] = "projection_x_coordinate"
            ds["station_y"].attrs["standard_name"] = "projection_y_coordinate"

        if "crs" in work.columns and work["crs"].notna().any():
            ds.attrs["crs"] = str(work["crs"].dropna().iloc[0])

        if "simulated_depth" in ds.data_vars:
            ds["simulated_depth"].attrs["units"] = "m"
        if "observed_depth" in ds.data_vars:
            ds["observed_depth"].attrs["units"] = "m"
        return ds

    def _normalize_dataset(self, ds: xr.Dataset) -> xr.Dataset:
        """Normalize coordinates to standard standards (EPSG:4326 -180/180)."""
        try:
            # 1. Detect Longitude coordinate
            lon_name = None
            if "longitude" in ds.coords:
                lon_name = "longitude"
            elif "lon" in ds.coords:
                lon_name = "lon"
            
            if lon_name:
                # Check if it needs wrapping (e.g. 0-360 range)
                # We use .max() compute because coords are usually small / loaded
                if ds[lon_name].max() > 180:
                    logger.info(f"Normalizing {lon_name} from 0-360 to -180/180 range...")
                    ds.coords[lon_name] = (ds.coords[lon_name] + 180) % 360 - 180
                    ds = ds.sortby(lon_name)
        except Exception as e:
            logger.warning(f"Coordinate normalization failed: {e}")
        
        return ds


    def _enrich_collection_metadata(self, collection: pystac.Collection, ds: xr.Dataset) -> pystac.Collection:
        """Enrich collection metadata using xstac to extract datacube info."""
        logger.info(f"ENTER _enrich_collection_metadata for {collection.id}")
        logger.info("Running xstac to extract rich metadata...")

        kw = {"reference_system": "EPSG:4326"}

        try:
            if ds.cf["time"].name in ds.dims:
                kw["temporal_dimension"] = ds.cf["time"].name
        except KeyError:
            pass

        try:
            if ds.cf["longitude"].name in ds.dims:
                kw["x_dimension"] = ds.cf["longitude"].name
            elif ds.cf["X"].name in ds.dims:
                kw["x_dimension"] = ds.cf["X"].name
        except KeyError:
            pass

        try:
            if ds.cf["latitude"].name in ds.dims:
                kw["y_dimension"] = ds.cf["latitude"].name
            elif ds.cf["Y"].name in ds.dims:
                kw["y_dimension"] = ds.cf["Y"].name
        except KeyError:
            pass

        logger.debug(f"xstac config: {kw}")

        try:
            return xstac.xarray_to_stac(ds, collection, **kw)
        except Exception as e:
            # Some model outputs (e.g., unstructured meshes) don't map cleanly to CF X/Y axes.
            # We still keep a valid STAC collection and continue generation.
            logger.warning(f"Collection xstac enrichment failed: {e}")
            return collection


    def _enrich_item_metadata(self, item: pystac.Item, ds: xr.Dataset) -> pystac.Item:
        """Enrich item metadata using xstac to extract datacube info."""
        try:
            # For Items, xstac needs a Collection template to run against, 
            # then we extract the properties back to the Item.
            
            # Create a dummy template from the item's geometry/time
            template = pystac.Collection(
                id="dummy",
                description="dummy",
                extent=pystac.Extent(
                    pystac.SpatialExtent([item.bbox]),
                    pystac.TemporalExtent([[item.datetime, item.datetime]])
                ),
                license="proprietary"
            )
            
            kw = {"reference_system": "EPSG:4326"}
            try:
                if ds.cf["time"].name in ds.dims: kw["temporal_dimension"] = ds.cf["time"].name
            except KeyError: pass
            
            try:
                if ds.cf["longitude"].name in ds.dims: kw["x_dimension"] = ds.cf["longitude"].name
            except KeyError: pass
            
            try:
                if ds.cf["latitude"].name in ds.dims: kw["y_dimension"] = ds.cf["latitude"].name
            except KeyError: pass
                
            out_col = xstac.xarray_to_stac(ds, template, **kw)
            
            # Now we transfer the extensions and properties from the temporary collection to our Item
            # 1. datacube extension
            if "cube:dimensions" in out_col.extra_fields:
                item.properties["cube:dimensions"] = out_col.extra_fields["cube:dimensions"]
            if "cube:variables" in out_col.extra_fields:
                item.properties["cube:variables"] = out_col.extra_fields["cube:variables"]
                
            # 2. Add schema
            for ext in out_col.stac_extensions:
                item.stac_extensions.append(ext)
            
            return item
            
        except Exception as e:
            logger.warning(f"Item xstac enrichment failed: {e}")
            return item
