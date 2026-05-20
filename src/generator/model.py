from __future__ import annotations
from typing import List, Optional, Any, Dict, Union
from pydantic import BaseModel, Field, HttpUrl, field_validator

class ProviderModel(BaseModel):
    name: str
    roles: List[str] = Field(default_factory=lambda: ["producer"])
    url: Optional[str] = None

class DatasetMetadata(BaseModel):
    """Strict schema for dataset metadata coming from Intake Catalog."""
    
    # Required Fields (Strictness)
    id: str = Field(..., description="Unique dataset identifier")
    description: str = Field(..., description="Detailed description of the dataset")
    
    # Optional Fields with Defaults
    title: Optional[str] = None
    license: str = "CC-BY-4.0"
    keywords: List[str] = Field(default_factory=list)
    
    # STAC Specific mappings
    processing_level: List[str] = Field(
        default_factory=lambda: ["bronze", "silver"],
        alias="processing:level"
    )

    @field_validator("processing_level", mode="before")
    @classmethod
    def _wrap_processing_level(cls, v: Any) -> list:
        if isinstance(v, str):
            return [v]
        return v
    platform: str = "unknown"
    category: str = "DATA"
    group_id: Optional[str] = None  # Explicit grouping ID (replaces auto-splitting)
    group_title: Optional[str] = None # Display title for the group
    group_description: Optional[str] = None # Description for the group catalog
    group_keywords: List[str] = Field(default_factory=list)  # Keywords for the parent Group Catalog
    
    providers: List[ProviderModel] = Field(default_factory=list)
    
    # Assets configuration
    thumbnail_path: Optional[str] = None
    thumbnail_variable: Optional[str] = None
    thumbnail_datetime: Optional[str] = None  # Specific UTC timestamp (ISO 8601) for event-based thumbnail
    example_notebook: Optional[str] = None

    # i18n hook (reserved). Future versions will resolve title/description from
    # an external i18n bundle keyed by this string; today it is accepted but not
    # consumed, so existing catalogs keep working unchanged.
    i18n_key: Optional[str] = None

    # Catch-all for other fields (e.g. sci:doi)
    # We use extra="allow" to pass through scientific extension fields
    model_config = {
        "extra": "allow",
        "populate_by_name": True
    }

    def get_title(self) -> str:
        return self.title or self.id
