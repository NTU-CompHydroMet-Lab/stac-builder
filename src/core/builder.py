from pathlib import Path

import intake
import yaml
from loguru import logger

from src.generator.intake_raster import IntakeRasterGenerator
from src.generator.intake_vector import IntakeVectorGenerator
from src.generator.intake_xarray import IntakeXarrayGenerator


def _detect_generator_kind(catalog_path: Path, source_id: str) -> str:
    """Infer which generator backend to use for ``source_id``.

    Resolution order:
      1. Source-level ``metadata.generator`` (raster/vector/xarray).
      2. Catalog-level top ``metadata.generator``.
      3. Source-level ``driver`` (``raster`` / ``vector`` short-circuit).
      4. Default: ``xarray`` (legacy behaviour for netcdf/zarr/parquet).
    """
    try:
        with open(catalog_path, "r", encoding="utf-8") as fp:
            cat = yaml.safe_load(fp) or {}
    except Exception as e:
        logger.warning(f"Could not parse {catalog_path} for generator detection: {e}")
        return "xarray"

    sources = cat.get("sources") or {}
    entry = sources.get(source_id) or {}
    src_meta = entry.get("metadata") or {}
    if isinstance(src_meta.get("generator"), str):
        return src_meta["generator"].strip().lower()

    cat_meta = cat.get("metadata") or {}
    if isinstance(cat_meta.get("generator"), str):
        return cat_meta["generator"].strip().lower()

    driver = str(entry.get("driver", "")).strip().lower()
    if driver in {"raster", "vector"}:
        return driver

    return "xarray"


def build_collection(catalog_path: Path, source_id: str, output_dir: Path) -> None:
    """Generate a static STAC collection + items for a single source.

    Dispatches to the appropriate generator backend based on catalog metadata.
    """
    logger.info(f"Opening catalog: {catalog_path}")
    kind = _detect_generator_kind(catalog_path, source_id)
    logger.info(f"[{source_id}] Using generator backend: {kind}")
    source_output_dir = output_dir / source_id

    try:
        if kind == "raster":
            with open(catalog_path, "r", encoding="utf-8") as fp:
                cat = yaml.safe_load(fp) or {}
            cat_meta = cat.get("metadata") or {}
            generator = IntakeRasterGenerator(
                output_dir=source_output_dir,
                catalog_path=catalog_path,
                catalog_metadata=cat_meta,
            )
            generator.generate(source_name=source_id)
            return

        if kind == "vector":
            with open(catalog_path, "r", encoding="utf-8") as fp:
                cat = yaml.safe_load(fp) or {}
            cat_meta = cat.get("metadata") or {}
            generator = IntakeVectorGenerator(
                output_dir=source_output_dir,
                catalog_path=catalog_path,
                catalog_metadata=cat_meta,
            )
            generator.generate(source_name=source_id)
            return

        # Default: legacy xarray (intake-driven).
        cat = intake.open_catalog(str(catalog_path))
        if source_id not in cat:
            raise ValueError(f"Source '{source_id}' not found in catalog '{catalog_path}'.")
        logger.info(f"Processing source: {source_id}")
        cat_meta = cat.metadata if hasattr(cat, "metadata") else {}
        generator = IntakeXarrayGenerator(
            output_dir=source_output_dir,
            catalog_path=catalog_path,
            catalog_metadata=cat_meta,
        )
        generator.generate(source_name=source_id)
    except Exception as e:
        logger.error(f"Failed to generate STAC for {source_id} from catalog {catalog_path}: {e}")
        raise
