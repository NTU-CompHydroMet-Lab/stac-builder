from __future__ import annotations
import shutil
from pathlib import Path
from typing import Optional
from loguru import logger
import pystac

def process_example_notebook(nb_path: str, output_dir: Path) -> Optional[pystac.Asset]:
    """Copy example notebook to output and return Asset object."""
    try:
        src_path = Path(nb_path)
        if not src_path.exists():
            logger.warning(f"Example notebook not found: {src_path}")
            return None
            
        examples_dir = output_dir / "examples"
        examples_dir.mkdir(exist_ok=True)
        
        dest_path = examples_dir / src_path.name
        shutil.copy2(src_path, dest_path)
        
        return pystac.Asset(
            href=f"./examples/{src_path.name}",
            media_type="application/x-ipynb+json",
            roles=["example", "docs"],
            title="Example Usage Notebook"
        )
    except Exception as e:
        logger.error(f"Failed to process example notebook: {e}")
        return None

def create_data_asset(source_path: str, item_id: str, items_dir: Path) -> pystac.Asset:
    """Create a local symlink to the data source and return the pystac.Asset."""
    # --- Local Symlink Strategy ---
    src_path_obj = Path(source_path).expanduser()
    if not src_path_obj.is_absolute():
        src_path_obj = src_path_obj.resolve()
    
    # Determine extension
    ext = ".zarr" if str(source_path).endswith(".zarr") else ".nc"
    if src_path_obj.suffix:
        ext = src_path_obj.suffix
        
    # Asset filename in the items folder
    asset_filename = f"{item_id}{ext}"
    asset_link_path = items_dir / asset_filename
    
    # Create Symlink if not exists (or update)
    if asset_link_path.exists() or asset_link_path.is_symlink():
        asset_link_path.unlink()
    
    media_type_by_ext = {
        ".zarr": "application/vnd+zarr",
        ".nc": "application/x-netcdf",
        ".parquet": "application/vnd.apache.parquet",
    }
    media_type = media_type_by_ext.get(ext.lower(), "application/octet-stream")
    
    try:
        # Check if source exists? If it's on a mounted drive, it should.
        asset_link_path.symlink_to(src_path_obj)
        logger.info(f"Created symlink: {asset_link_path.name} -> {src_path_obj}")
        
        return pystac.Asset(
            href=f"./{asset_filename}",
            media_type=media_type,
            roles=["data"],
            title=f"Data for {item_id}"
        )
    except Exception as e:
        logger.error(f"Failed to symlink asset {src_path_obj}: {e}")
        # Fallback to absolute path
        return pystac.Asset(
            href=str(src_path_obj),
            media_type=media_type,
            roles=["data"],
            title=f"Data for {item_id} (Absolute Path)"
        )


def create_supplemental_asset(
    source_path: str,
    item_id: str,
    items_dir: Path,
    asset_key: str,
    media_type: Optional[str] = None,
    roles: Optional[list[str]] = None,
    title: Optional[str] = None,
) -> pystac.Asset:
    """Create a local symlink for a supplemental file and return a STAC Asset."""
    src_path_obj = Path(source_path).expanduser()
    if not src_path_obj.is_absolute():
        src_path_obj = src_path_obj.resolve()
    ext = src_path_obj.suffix
    if src_path_obj.name.endswith(".zarr"):
        ext = ".zarr"
    asset_filename = f"{item_id}__{asset_key}{ext}"
    asset_link_path = items_dir / asset_filename

    if asset_link_path.exists() or asset_link_path.is_symlink():
        asset_link_path.unlink()

    inferred = {
        ".csv": "text/csv",
        ".json": "application/json",
        ".parquet": "application/vnd.apache.parquet",
        ".gpkg": "application/geopackage+sqlite3",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".nc": "application/x-netcdf",
        ".zarr": "application/vnd+zarr",
    }.get(ext.lower(), "application/octet-stream")

    final_type = media_type or inferred
    final_roles = roles or ["metadata"]
    final_title = title or src_path_obj.name

    try:
        asset_link_path.symlink_to(src_path_obj)
        logger.info(f"Created supplemental symlink: {asset_link_path.name} -> {src_path_obj}")
        return pystac.Asset(
            href=f"./{asset_filename}",
            media_type=final_type,
            roles=final_roles,
            title=final_title,
        )
    except Exception as e:
        logger.error(f"Failed to symlink supplemental asset {src_path_obj}: {e}")
        return pystac.Asset(
            href=str(src_path_obj),
            media_type=final_type,
            roles=final_roles,
            title=f"{final_title} (Absolute Path)",
        )
