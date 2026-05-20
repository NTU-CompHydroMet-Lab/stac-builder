"""Pure utilities for raster (GeoTIFF/COG) processing.

These functions are independent of the generator/intake plumbing so they can be
reused by other code paths (e.g. data prep scripts in downstream repos).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


COG_MIME = "image/tiff; application=geotiff; profile=cloud-optimized"
TIFF_MIME = "image/tiff; application=geotiff"
GEOPARQUET_MIME = "application/x-parquet"
PROJ_EXT = "https://stac-extensions.github.io/projection/v2.0.0/schema.json"


def tiff_to_cog(src: Path, dst: Path, profile: str = "deflate", overview_level: int = 4) -> None:
    """Convert a GeoTIFF to Cloud Optimized GeoTIFF using rio_cogeo.

    Parameters
    ----------
    src:
        Source GeoTIFF path.
    dst:
        Destination COG path. Parent directory must exist.
    profile:
        rio_cogeo profile name (e.g. ``deflate``, ``lzw``, ``jpeg``).
    overview_level:
        Number of overview levels to generate.
    """
    from rio_cogeo.cogeo import cog_translate
    from rio_cogeo.profiles import cog_profiles

    cog_profile = cog_profiles.get(profile)
    cog_translate(str(src), str(dst), cog_profile, quiet=True, overview_level=overview_level)
    logger.info(f"COG: {src.name} -> {dst.name}")


def reproject_bbox_to_wgs84(
    bounds: Tuple[float, float, float, float],
    src_crs: Any,
) -> List[float]:
    """Reproject a bounding box (left, bottom, right, top) to EPSG:4326.

    ``src_crs`` accepts anything ``pyproj.Transformer.from_crs`` accepts
    (rasterio CRS, EPSG int, WKT string).
    """
    from pyproj import Transformer
    from rasterio.crs import CRS

    crs = src_crs if isinstance(src_crs, CRS) else CRS.from_user_input(src_crs)
    src_epsg = crs.to_epsg()
    left, bottom, right, top = bounds
    if src_epsg == 4326:
        return [float(left), float(bottom), float(right), float(top)]

    transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    x_min, y_min = transformer.transform(left, bottom)
    x_max, y_max = transformer.transform(right, top)
    return [float(x_min), float(y_min), float(x_max), float(y_max)]


def tiff_info(path: Path) -> Dict[str, Any]:
    """Read raster metadata from a GeoTIFF / COG.

    Returns a dict with: ``epsg`` (int or WKT str), ``proj_code`` (``EPSG:<n>``
    or WKT), ``transform`` (6-tuple), ``shape`` (``[h, w]``), ``dtype``,
    ``nodata``, ``bbox_wgs84`` (rounded to 6 decimals).
    """
    import rasterio

    with rasterio.open(path) as ds:
        crs = ds.crs
        transform = list(ds.transform)[:6]
        shape = [ds.height, ds.width]
        dtype = str(ds.dtypes[0])
        nodata = ds.nodata
        bounds = ds.bounds  # left, bottom, right, top

    bbox_wgs84 = reproject_bbox_to_wgs84(
        (bounds.left, bounds.bottom, bounds.right, bounds.top),
        crs,
    )
    src_epsg = crs.to_epsg() if crs is not None else None
    proj_code = f"EPSG:{src_epsg}" if src_epsg else (crs.to_wkt() if crs else None)

    return {
        "epsg": src_epsg or (crs.to_wkt() if crs else None),
        "proj_code": proj_code,
        "transform": transform,
        "shape": shape,
        "dtype": dtype,
        "nodata": nodata,
        "bbox_wgs84": [round(v, 6) for v in bbox_wgs84],
    }


def bbox_to_polygon(bbox: List[float]) -> Dict[str, Any]:
    """Build a GeoJSON Polygon dict from a ``[west, south, east, north]`` bbox."""
    west, south, east, north = bbox
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [west, south],
                [east, south],
                [east, north],
                [west, north],
                [west, south],
            ]
        ],
    }


def shapefile_to_geoparquet(shp_path: Path, out_path: Path) -> Optional[Path]:
    """Convert a Shapefile to GeoParquet using geopandas.

    Returns the output path on success, or ``None`` if the source file is
    missing. Preserves the source CRS.
    """
    import geopandas as gpd

    if not shp_path.exists():
        logger.warning(f"Shapefile not found: {shp_path}")
        return None
    gdf = gpd.read_file(str(shp_path))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(str(out_path))
    logger.info(f"GeoParquet: {shp_path.name} -> {out_path.name}")
    return out_path


def vector_bbox_wgs84(parquet_or_shp_path: Path) -> Tuple[List[float], Dict[str, Any]]:
    """Read a GeoParquet/Shapefile and return ``([w, s, e, n], geojson_geometry)`` in EPSG:4326.

    The returned geometry is the union of all features. Useful for STAC items
    whose footprint is the AOI itself.
    """
    import geopandas as gpd
    from shapely.geometry import mapping

    suffix = parquet_or_shp_path.suffix.lower()
    if suffix == ".parquet":
        gdf = gpd.read_parquet(str(parquet_or_shp_path))
    else:
        gdf = gpd.read_file(str(parquet_or_shp_path))

    gdf_wgs84 = gdf.to_crs("EPSG:4326")
    union_geom = gdf_wgs84.union_all()
    bounds = gdf_wgs84.total_bounds  # [minx, miny, maxx, maxy]
    bbox = [round(float(v), 6) for v in bounds]
    return bbox, mapping(union_geom)
