import shutil
import sys
from pathlib import Path
import xarray as xr
import pandas as pd
import numpy as np
import pytest

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.generator.base import StacGenerator

# Mock Subclass for Testing
class DummyGenerator(StacGenerator):
    def load_source(self, source_name: str):
        return "dummy_source"
    
    def extract_metadata(self, source):
        return {
            "id": "dummy-collection",
            "title": "Dummy STAC Collection",
            "description": "Testing generator logic",
            "processing:level": "bronze",
            "platform": "Simulated",
            "providers": [{"name": "Test Provider", "roles": ["producer"]}]
        }
    
    def get_dataset(self, source):
        # Create a dummy in-memory dataset
        dates = pd.date_range("2020-01-01", periods=5, freq="D")
        ds = xr.Dataset(
            data_vars={"temp": (("time", "lat", "lon"), np.random.rand(5, 10, 10))},
            coords={
                "time": dates,
                "lat": np.linspace(-10, 10, 10),
                "lon": np.linspace(100, 120, 10)
            }
        )
        ds.encoding["source"] = "/tmp/dummy_source.nc" # Fake source path
        return ds

@pytest.fixture
def clean_output():
    out_dir = Path("tests/output")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    return out_dir

def test_generator_execution(clean_output):
    """Test that the generator runs and creates JSON files."""
    gen = DummyGenerator(clean_output)
    gen.generate("dummy_source")
    
    # Verify Collection
    coll_path = clean_output / "collection.json"
    assert coll_path.exists()
    
    # Verify Items (we expect items for year 2020)
    items_dir = clean_output / "items"
    assert items_dir.exists()
    assert len(list(items_dir.glob("*.json"))) >= 1
