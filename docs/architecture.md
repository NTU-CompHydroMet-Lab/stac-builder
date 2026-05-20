# Architecture

Three-layer split that the codebase honours:

```
┌─────────────── cli.py ───────────────┐  user-facing entry, arg parsing
│                                       │
├─────────── src/core/ ────────────────┤  orchestration: dispatcher,
│  builder, preflight, root_catalog,   │  preflight, root catalog walk
│  event_collection                    │
│                                       │
└──────── src/generator/ ──────────────┘  per-format generators implementing
   base.py + intake_{xarray,raster,    │  one StacGenerator ABC
                     vector}.py        │
```

## StacGenerator hooks

`src/generator/base.py` defines the workflow. Concrete generators override:

| Hook                          | Required | Purpose                                       |
| ----------------------------- | -------- | --------------------------------------------- |
| `load_source(name)`           | yes      | Return raw source object                       |
| `extract_metadata(source)`    | yes      | Return flat metadata dict                      |
| `get_dataset(source)`         | optional | xarray.Dataset or None                         |
| `_compute_extent(...)`        | optional | Default = `compute_extent(ds)` for xarray      |
| `_iter_items(...)`            | optional | Default = per-year split for xarray            |
| `_finalize_collection(...)`   | optional | Attach collection-level assets (e.g. thumb)    |
| `_enrich_collection_metadata` | optional | e.g. xstac datacube extension                  |
| `_enrich_item_metadata`       | optional | e.g. projection extension fields               |

This shape lets raster / vector generators sit alongside the xarray one
without inheritance gymnastics: they return `None` from `get_dataset` and
override the two extent / iteration hooks.

## Why preflight is its own module

Catalog YAMLs change weekly but `cli.py` and `core/builder.py` change rarely.
Putting fail-fast checks in `core/preflight.py` keeps the CLI thin (it just
formats the report) and the build pipeline pure (it assumes inputs are valid
because preflight already ran).

The check functions return `PreflightReport`, never raise. The CLI decides
exit codes after reading the report.

## What's intentionally NOT in this repo

- HTTP serving (different repo: stac-server)
- Dockerfile (different repo: deployment repos)
- Lab dataset catalogs with hard-coded paths (different repo: deployment repos)
- Fusion items / cross-product analysis logic (will move to a plugin hook
  when the second project needs it; until then deployment repos write that
  layer themselves)
