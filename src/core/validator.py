from pathlib import Path
from loguru import logger

try:
    from stac_validator.stac_validator import StacValidate
    HAS_VALIDATOR = True
except ImportError:
    HAS_VALIDATOR = False

def validate_catalog(catalog_path: Path) -> bool:
    """
    Run STAC validation on the generated catalog.
    Returns True if valid, False otherwise.
    """
    if not HAS_VALIDATOR:
        logger.warning("stac-validator not installed. Skipping validation.")
        logger.info("Install with: uv add stac-validator")
        return True

    logger.info(f"Validating catalog at {catalog_path}...")
    
    stac = StacValidate(str(catalog_path), recursive=True)
    stac.run()
    
    # stac.message contains the result dictionary
    # Example structure: [{'path': ..., 'valid_stac': True, ...}, ...]
    # Or for single file: {'valid_stac': True, ...}
    
    # Check if all checked items are valid
    is_valid = True
    messages = stac.message
    
    # stac-validator return structure varies by version/recursive flag
    # If recursive, it returns a list of result dicts
    if isinstance(messages, list):
        filtered_errors = [m for m in messages if isinstance(m, dict) and not m.get("valid_stac", False)]
        if filtered_errors:
            is_valid = False
            for err in filtered_errors:
                logger.error(f"Validation Invalid: {err.get('path')} - {err.get('error_message')}")
    elif isinstance(messages, dict):
         if not messages.get("valid_stac", False):
            is_valid = False
            logger.error(f"Validation Invalid: {messages.get('error_message')}")
            
    if is_valid:
        logger.success("STAC Validation Passed (100% Valid).")
        return True
    else:
        logger.error("STAC Validation Failed.")
        return False
