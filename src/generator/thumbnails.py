from __future__ import annotations
import matplotlib.pyplot as plt
import xarray as xr
from pathlib import Path
from typing import Dict, Optional
from loguru import logger
from .utils import get_spatial_dims

# Set non-interactive backend for matplotlib
plt.switch_backend('Agg')

import pystac


def generate_raster_thumbnail(
    src_raster: Path,
    item_id: str,
    items_dir: Path,
    cmap: str = "terrain",
    facecolor: str = "black",
) -> Optional[Dict[str, object]]:
    """Render a PNG thumbnail from a single-band GeoTIFF / COG using matplotlib.

    Returns the asset dict (compatible with ``pystac.Asset.from_dict``) or
    ``None`` on failure.
    """
    import numpy as np
    import rasterio

    try:
        with rasterio.open(src_raster) as ds:
            data = ds.read(1).astype(float)
            nodata = ds.nodata

        if nodata is not None:
            data = np.ma.masked_equal(data, nodata)

        thumb_name = f"{item_id}_thumb.png"
        thumb_path = items_dir / thumb_name

        fig, ax = plt.subplots(figsize=(4, 4), dpi=100)
        ax.imshow(data, cmap=cmap, interpolation="bilinear")
        ax.set_axis_off()
        fig.patch.set_facecolor(facecolor)
        plt.tight_layout(pad=0)
        fig.savefig(thumb_path, bbox_inches="tight", pad_inches=0, dpi=100, facecolor=facecolor)
        plt.close(fig)
        logger.info(f"Raster thumbnail: {thumb_name}")

        return {
            "href": f"./items/{thumb_name}",
            "type": "image/png",
            "roles": ["thumbnail"],
            "title": f"Thumbnail for {item_id}",
        }
    except Exception as e:
        logger.warning(f"Raster thumbnail generation failed for {src_raster}: {e}")
        return None

def generate_thumbnail(
    ds: xr.Dataset, 
    item_id: str, 
    items_dir: Path, 
    target_var: Optional[str] = None,
    target_datetime: Optional[str] = None
) -> Optional[Dict[str, object]]:
    """
    Generate a simple map thumbnail (PNG) for the dataset.
    
    Args:
        target_datetime: ISO8601 string (UTC). Picks nearest time. 
                         If None, defaults to the middle timestep.
    """
    import pandas as pd
    
    logger.info(f"Generating thumbnail for {item_id} (time={target_datetime or 'middle'})...")
    try:
        if not target_var or target_var not in ds.data_vars:
            logger.warning(f"Target variable '{target_var}' not found in dataset. Skipping.")
            return None
            
        var_name = target_var
        da = ds[var_name]
        
        # Select data based on Time
        if "time" in ds.dims:
            t_size = ds.sizes["time"]
            
            # 1. Event Time (Nearest)
            if target_datetime:
                try:
                    dt = pd.to_datetime(target_datetime)
                    da = da.sel(time=dt, method="nearest")
                    actual_time = da.time.values
                    logger.info(f"Detail thumbnail: {dt} -> {actual_time}")
                except Exception as time_err:
                    logger.warning(f"Failed to select target time {target_datetime}: {time_err}. Fallback to middle.")
                    target_datetime = None 

            # 2. Fallback (Middle)
            if not target_datetime:
                mid_idx = t_size // 2
                da = da.isel(time=mid_idx)
                logger.debug(f"Thumbnail using middle index: {mid_idx}")
        
        # Check validity
        if da.isnull().all():
            logger.warning(f"Thumbnail data for {item_id} is empty. Skipping.")
            return None
            
        # Plot
        lon, lat = get_spatial_dims(ds)
        if lon is None or lat is None:
            return None
            
        # [Fix] Handle Dateline Wrapping for Pacific-centered data (e.g. Himawari)
        # If we have data at both ends (-180, 180) but a gap at 0, unwrap to 0-360.
        try:
            lons = da[lon.name]
            has_neg_edge = (lons < -90).any()
            has_pos_edge = (lons > 90).any()
            has_center = ((lons > -20) & (lons < 20)).any()
            
            if has_neg_edge and has_pos_edge and not has_center:
                logger.info(f"Detected Pacific view for {item_id}. Unwrapping 0-360 for thumbnail.")
                da = da.assign_coords({lon.name: (da[lon.name] % 360)})
                da = da.sortby(lon.name)
        except Exception as e:
            logger.warning(f"Failed to unwrap coordinates: {e}")

        fig, ax = plt.subplots(figsize=(4, 4))
        da.plot(
            ax=ax, 
            x=lon.name, 
            y=lat.name, 
            add_colorbar=False, 
            add_labels=False, 
            cmap='viridis', 
            robust=True
        )
        ax.set_axis_off()
        plt.tight_layout(pad=0)
        
        # Save
        thumb_name = f"{item_id}_thumb.png"
        thumb_path = items_dir / thumb_name
        plt.savefig(thumb_path, transparent=True, bbox_inches='tight', pad_inches=0)
        plt.close(fig)
        
        # Return Asset Dict (Sender expects dict to create Asset, or we returned asset object?)
        # Base.py currently expects a dict: pystac.Asset.from_dict(...)
        # We can return the dict structure which is compatible.
        return {
            "href": f"./items/{thumb_name}",
            "type": "image/png",
            "roles": ["thumbnail"],
            "title": f"Thumbnail for {item_id}"
        }

    except Exception as e:
        logger.warning(f"Thumbnail generation failed: {e}")
        return None
