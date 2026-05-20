"""Event-based STAC collection aggregation.

Aggregates per-product STAC collections that share an ``event_id`` into a
single event collection with a unioned extent.

Project-specific cross-asset "fusion" logic is intentionally out of scope here
and should live in deployment repos as a plugin/hook on top of the items this
module produces.
"""

import json
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pystac
from loguru import logger


def get_product_aliases() -> list[tuple[str, str]]:
    """Load product aliases from settings. Returns empty list if none configured.

    Imported lazily so this module can be used without ``config/main.yaml``.
    """
    try:
        from src.settings import get_settings  # lazy

        s = get_settings()
        if s and s.event_aggregation.product_aliases:
            return [(e.match, e.alias) for e in s.event_aggregation.product_aliases]
    except Exception:
        pass
    return []


def resolve_href(base_file: Path, href: str) -> Path:
    p = Path(href)
    if p.is_absolute():
        return p
    return (base_file.parent / p).resolve()


def union_extent(cols: list[pystac.Collection]) -> pystac.Extent:
    west, south, east, north = 180.0, 90.0, -180.0, -90.0
    has_bbox = False
    non_global_bbox = False
    starts: list[datetime] = []
    ends: list[datetime] = []

    for col in cols:
        for b in col.extent.spatial.bboxes:
            if not b or len(b) < 4:
                continue
            if [float(b[0]), float(b[1]), float(b[2]), float(b[3])] == [-180.0, -90.0, 180.0, 90.0]:
                continue
            has_bbox = True
            non_global_bbox = True
            west = min(west, float(b[0]))
            south = min(south, float(b[1]))
            east = max(east, float(b[2]))
            north = max(north, float(b[3]))
        for iv in col.extent.temporal.intervals:
            if not iv or len(iv) < 2:
                continue
            if iv[0] is not None:
                starts.append(iv[0])
            if iv[1] is not None:
                ends.append(iv[1])

    if not has_bbox and not non_global_bbox:
        west, south, east, north = -180.0, -90.0, 180.0, 90.0

    t0 = min(starts) if starts else None
    t1 = max(ends) if ends else None
    return pystac.Extent(
        pystac.SpatialExtent([[west, south, east, north]]),
        pystac.TemporalExtent([[t0, t1]]),
    )


def asset_ext(path: Path) -> str:
    if path.name.endswith(".zarr"):
        return ".zarr"
    return path.suffix or ""


def product_alias(collection_id: str) -> str:
    lid = collection_id.lower()
    for needle, alias in get_product_aliases():
        if needle in lid:
            return alias
    return collection_id


def select_event_assets(alias: str, assets: dict) -> list[tuple[str, dict]]:
    """Select which assets should be carried into the event-level item.

    Default behaviour: pass through every dict-shaped asset unchanged.

    Project-specific selection (e.g. "prefer the parquet for IoT timeseries")
    should be implemented in deployment repos by overriding this function or
    via a future plugin hook; the engine ships with the neutral default only.
    """
    if not isinstance(assets, dict):
        return []
    return [(k, v) for k, v in assets.items() if isinstance(v, dict)]


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def to_iso_utc_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def bbox_to_polygon(bbox: list[float]) -> dict:
    minx, miny, maxx, maxy = bbox
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [minx, miny],
                [maxx, miny],
                [maxx, maxy],
                [minx, maxy],
                [minx, miny],
            ]
        ],
    }


def build_event_collection(
    stac_root: Path,
    event_dir: Path,
    event_id: str,
    event_title: str,
    event_desc: str,
    cols: list[pystac.Collection],
) -> pystac.Collection:
    """Aggregate product collections sharing ``event_id`` into a single event collection.

    Each product collection's items are copied/relinked into ``event_dir/items``
    with rewritten ids/links. Links are written using pystac defaults (relative
    self-contained); any server-specific URL rewriting belongs in the server
    layer, not the builder.
    """
    items_dir = event_dir / "items"
    if items_dir.exists():
        shutil.rmtree(items_dir)
    items_dir.mkdir(parents=True, exist_ok=True)

    # Remove stale event catalog file from previous runs.
    stale_event_catalog = event_dir / "catalog.json"
    if stale_event_catalog.exists():
        stale_event_catalog.unlink()

    first = cols[0]
    event_collection = pystac.Collection(
        id=event_id,
        title=event_title,
        description=event_desc,
        license=first.license or "CC-BY-4.0",
        extent=union_extent(cols),
        providers=first.providers or [],
        keywords=first.keywords or [],
    )
    event_collection.set_self_href(str(event_dir / "collection.json"))
    event_collection.extra_fields["aggregation_type"] = "event_collection"
    if first.extra_fields.get("group_id"):
        event_collection.extra_fields["group_id"] = first.extra_fields.get("group_id")
    event_collection.extra_fields["event_id"] = event_id

    alias_count: dict[str, int] = defaultdict(int)

    for col in sorted(cols, key=lambda c: c.id):
        # Prefer explicit item links from collection, fall back to items/*.json.
        item_paths: list[Path] = []
        col_file = Path(col.get_self_href())
        for l in col.links:
            if l.rel == "item" and l.href:
                item_paths.append(resolve_href(col_file, l.href))

        if not item_paths:
            item_paths = sorted((col_file.parent / "items").glob("*.json"))

        for src_item_path in item_paths:
            if not src_item_path.exists():
                continue
            src = json.loads(src_item_path.read_text(encoding="utf-8"))
            alias = product_alias(col.id)
            alias_count[alias] += 1
            suffix = "" if alias_count[alias] == 1 else f"_{alias_count[alias]}"
            item_id = f"{event_id}__{alias}{suffix}"

            src["id"] = item_id
            src["collection"] = event_id
            src["links"] = [
                {"rel": "root", "href": "../../../catalog.json", "type": "application/json"},
                {"rel": "collection", "href": "../collection.json", "type": "application/json"},
                {"rel": "parent", "href": "../collection.json", "type": "application/json"},
            ]
            src.setdefault("properties", {})
            src["properties"]["event_id"] = event_id
            src["properties"]["product_collection_id"] = col.id
            src["properties"]["product_key"] = alias
            src["properties"]["title"] = col.title or alias

            # Re-link assets into event/items for stable browsing.
            new_assets: dict[str, dict] = {}
            selected_assets = select_event_assets(alias, src.get("assets", {}))
            for key, asset in selected_assets:
                href = asset.get("href")
                if not href:
                    continue
                abs_asset = resolve_href(src_item_path, href)
                ext = asset_ext(abs_asset)
                new_name = f"{item_id}__{key}{ext}"
                new_link = items_dir / new_name
                if new_link.exists() or new_link.is_symlink():
                    new_link.unlink()
                a = dict(asset)
                try:
                    new_link.symlink_to(abs_asset)
                    a["href"] = f"./{new_name}"
                except Exception:
                    a["href"] = str(abs_asset)
                new_assets[key] = a
            src["assets"] = new_assets

            out_item = items_dir / f"{item_id}.json"
            out_item.write_text(json.dumps(src, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            event_collection.add_link(
                pystac.Link(
                    rel="item",
                    target=f"./items/{item_id}.json",
                    media_type="application/json",
                )
            )

    (event_dir / "collection.json").write_text(
        json.dumps(event_collection.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return event_collection
