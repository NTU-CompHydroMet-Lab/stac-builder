from __future__ import annotations
import pandas as pd
import xarray as xr
import warnings
from typing import Dict, List, Tuple, Optional, Any
import pystac
from shapely.geometry import Polygon, mapping
import antimeridian
from loguru import logger

def format_datetime(dt) -> str:
    """Format a datetime object/string to RFC 3339 string (Z-suffixed)."""
    return pd.to_datetime(dt).strftime('%Y-%m-%dT%H:%M:%SZ')

import cf_xarray

def get_spatial_dims(ds: xr.Dataset) -> Tuple[Optional[xr.DataArray], Optional[xr.DataArray]]:
    """Helper to get longitude and latitude arrays using CF conventions."""
    # Use cf-xarray directly
    try:
        lon = ds.cf["longitude"]
    except KeyError:
        lon = None
        
    try:
        lat = ds.cf["latitude"]
    except KeyError:
        lat = None
        
    return lon, lat

def compute_item_geometry(bbox: List[float]) -> Optional[Dict[str, Any]]:
    """
    Compute a valid GeoJSON Geometry from a BBox, handling Antimeridian splitting.
    
    Args:
        bbox: [west, south, east, north]
    
    Returns:
        GeoJSON dict (Polygon or MultiPolygon) or None.
    """
    try:
        west, south, east, north = bbox
        
        # If west > east, it crosses the Antimeridian (e.g. 80, -60, -160, 60)
        # We construct a Polygon in 0-360 space (unwrapped) or handle splitting.
        # Ideally, we create a Polygon that might exceed 180, then use antimeridian to fix.
        
        if west > east:
            # Shift east coordinate to be > 180 (e.g. -160 -> 200)
            # This makes a valid contiguous box in "unwrapped" space
            east_unwrapped = east + 360
            poly = Polygon([
                (west, south),
                (east_unwrapped, south),
                (east_unwrapped, north),
                (west, north),
                (west, south)
            ])
        else:
            poly = Polygon([
                (west, south),
                (east, south),
                (east, north),
                (west, north),
                (west, south)
            ])
            
        # Fix using Antimeridian package
        # This splits it into a MultiPolygon [-180, east] and [west, 180] if needed
        fixed_poly = antimeridian.fix_polygon(poly)
        
        return mapping(fixed_poly)
        
    except Exception as e:
        warnings.warn(f"Failed to compute geometry: {e}")
        return None

def compute_extent(ds: xr.Dataset) -> pystac.Extent:
    """
    Compute spatial bbox and temporal interval from an ``xarray`` dataset.
    Automatically detects Pacific View (Antimeridian crossing) and returns 
    a valid [West, South, East, North] BBox where West > East.
    """
    try:
        # Use simple try-except or helper, but user code suggests inline
        lon, lat = get_spatial_dims(ds)
        
        if lon is not None and lat is not None:
             # Ensure numpy usage
            if hasattr(lon.data, "compute"):
                lons = lon.values
            else:
                lons = lon.data
                
            lat_min = float(lat.min())
            lat_max = float(lat.max())
            
            # Check for Pacific View Pattern
            has_neg = (lons < -90).any()
            has_pos = (lons > 90).any()
            crosses_greenwich = ((lons > -20) & (lons < 20)).any()
            
            if has_neg and has_pos and not crosses_greenwich:
                logger.debug("Detected Antimeridian Crossing (Pacific View).")
                # West = Min of Positive Segment
                lon_min = float(lons[lons > 0].min())
                # East = Max of Negative Segment
                lon_max = float(lons[lons < 0].max())
                spatial_bbox = [lon_min, lat_min, lon_max, lat_max]
            else:
                 spatial_bbox = [float(lon.min()), lat_min, float(lon.max()), lat_max]
        else:
            warnings.warn("Could not find longitude/latitude coordinates.")
            spatial_bbox = [-180.0, -90.0, 180.0, 90.0]
    except Exception as exc:
        warnings.warn(f"Unable to compute spatial extent: {exc}")
        spatial_bbox = [-180.0, -90.0, 180.0, 90.0]

    try:
        # Use cf-xarray to find time dimension
        try:
            times = ds.cf["time"]
            start = pd.to_datetime(times.min().values)
            end = pd.to_datetime(times.max().values)
            temporal_interval = [[start, end]]
        except KeyError:
             # Fallback
             if "time" in ds.dims:
                 start = pd.to_datetime(ds.time.min().values)
                 end = pd.to_datetime(ds.time.max().values)
                 temporal_interval = [[start, end]]
             else:
                 # Default if no time
                 default = pd.to_datetime("2024-01-01")
                 temporal_interval = [[default, default]]
    except Exception as exc:
        warnings.warn(f"Unable to compute temporal interval: {exc}")
        default = pd.to_datetime("1970-01-01T00:00:00Z")
        temporal_interval = [[default, default]]

    return pystac.Extent(
        spatial=pystac.SpatialExtent([spatial_bbox]),
        temporal=pystac.TemporalExtent(temporal_interval)
    )
