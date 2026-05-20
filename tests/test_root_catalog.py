"""Tests for src.core.root_catalog.update_root_catalog.

Regression coverage for the duplicate-child-link bug: running the root
catalog generator more than once over the same output directory must
not multiply the number of child links in any group catalog.
"""
from __future__ import annotations

import json
from pathlib import Path

import pystac

from src.core.root_catalog import update_root_catalog


def _make_collection(
    output_dir: Path,
    col_id: str,
    group_id: str,
) -> None:
    """Write a minimal valid STAC Collection at output_dir/<col_id>/collection.json.

    Mirrors what the per-source generators produce before update_root_catalog
    moves the directory under the group folder.
    """
    col_dir = output_dir / col_id
    col_dir.mkdir(parents=True, exist_ok=True)

    col = pystac.Collection(
        id=col_id,
        description=f"test collection {col_id}",
        extent=pystac.Extent(
            spatial=pystac.SpatialExtent([[-180.0, -90.0, 180.0, 90.0]]),
            temporal=pystac.TemporalExtent([[None, None]]),
        ),
        license="proprietary",
    )
    col.extra_fields["group_id"] = group_id
    col.set_self_href(str(col_dir / "collection.json"))
    col.save_object(include_self_link=False)


def _count_child_links(catalog_json_path: Path) -> int:
    payload = json.loads(catalog_json_path.read_text())
    return sum(1 for link in payload.get("links", []) if link.get("rel") == "child")


def test_group_catalog_has_one_link_per_collection(tmp_path: Path) -> None:
    """Single run: a group with 2 sources produces 2 child links, not 4."""
    _make_collection(tmp_path, "imerg_final_v07", group_id="imerg")
    _make_collection(tmp_path, "imerg_early_v07", group_id="imerg")

    update_root_catalog(tmp_path)

    group_catalog = tmp_path / "imerg" / "catalog.json"
    assert group_catalog.exists(), "group catalog was not written"
    assert _count_child_links(group_catalog) == 2, (
        f"expected 2 child links, got {_count_child_links(group_catalog)} — "
        f"links: {json.loads(group_catalog.read_text())['links']}"
    )


def test_rerunning_does_not_duplicate_child_links(tmp_path: Path) -> None:
    """Re-running the full pipeline over a reused output dir must not duplicate.

    Reproduces the production symptom: the builder pipeline reuses
    ``lab_stac_catalog/`` across runs (no --clean). Per-source generators
    always emit at the flat path ``<output>/<col_id>/collection.json``,
    while ``update_root_catalog`` moves the directory into
    ``<output>/<group>/<col_id>/`` on each run. So on every run after the
    first, ``rglob("collection.json")`` sees BOTH locations and every
    collection gets ``add_child``'d twice.
    """
    # Run 1: per-source generators emit flat collections; root catalog
    # builder moves them under the group dir.
    _make_collection(tmp_path, "imerg_final_v07", group_id="imerg")
    _make_collection(tmp_path, "imerg_early_v07", group_id="imerg")
    update_root_catalog(tmp_path)

    # Run 2: per-source generators emit at the flat path again (they
    # don't know about group structure). The previous run's nested
    # copies under tmp_path/imerg/* are still there.
    _make_collection(tmp_path, "imerg_final_v07", group_id="imerg")
    _make_collection(tmp_path, "imerg_early_v07", group_id="imerg")
    update_root_catalog(tmp_path)

    group_catalog = tmp_path / "imerg" / "catalog.json"
    assert _count_child_links(group_catalog) == 2, (
        f"second run inflated child links to {_count_child_links(group_catalog)} — "
        f"hrefs: {[l['href'] for l in json.loads(group_catalog.read_text())['links'] if l['rel'] == 'child']}"
    )
