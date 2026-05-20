# stac-builder

Static STAC catalog builder for the NTU CompHydroMet Lab.

This is the **engine repo**: it does one thing — take Intake-style catalog YAML
files plus their underlying data and produce a static STAC JSON catalog. It is
designed to be `git clone`d into a deployment repo's container build; that
deployment repo decides what catalogs to feed in, when to run, and where to
serve the output.

The companion **stac-server** repo (separate, not yet split off) is a thin
FastAPI / nginx layer that mounts the output of this builder and exposes it
over HTTP. **This repo does not serve, run a UI, or watch directories.**

---

## What it does

```
catalog.yaml + data files
         │
         ▼
  stac-builder run
         │
         ▼
stac_catalog/   (collection.json + items/*.json + root catalog.json)
```

Supported source kinds (via the catalog YAML `driver` / `metadata.generator`):

| Kind     | Driver           | Inputs                              |
| -------- | ---------------- | ----------------------------------- |
| `xarray` | `zarr`, `netcdf` | NetCDF / Zarr (default backend)     |
| `xarray` | `parquet`        | Station/tabular parquet             |
| `raster` | `raster`         | GeoTIFF (optionally converted to COG) |
| `vector` | `vector`         | Shapefile / GeoParquet              |

See `config/catalogs/template/` for one self-documenting YAML per kind.

---

## System dependencies (must be present **before** `uv sync`)

The Python deps include `rasterio`, `geopandas`, `pyproj` and `netCDF4`, which
all bind against system libraries. On Debian/Ubuntu:

```bash
apt-get install -y \
    libgdal-dev \
    g++ gcc \
    libhdf5-dev
```

GDAL needs to be discoverable when building `rasterio`. If you see import
errors mentioning `libgdal.so`, double-check that `libgdal-dev` is installed
and matches the `rasterio` wheel's expected ABI (Python 3.11 in our case).

Python `>=3.11` is required.

---

## Install (development)

```bash
git clone <this repo> stac-builder
cd stac-builder
uv sync       # creates .venv with all engine deps
uv run pytest # ~70 tests, should all pass
```

## Install (inside a container / deployment repo)

A deployment repo's Dockerfile typically does:

```dockerfile
RUN apt-get update && apt-get install -y libgdal-dev g++ gcc libhdf5-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv
WORKDIR /app
RUN git clone --depth 1 https://github.com/<org>/stac-builder.git .
RUN uv sync --frozen --no-dev

ENTRYPOINT ["uv", "run", "python", "-m", "src.cli"]
```

`stac-builder` does **not** ship a Dockerfile itself — that lives in
deployment repos so they can decide base image, mount points, scheduling, etc.

---

## CLI

```
stac-builder build    --catalog catalogs/era5.yaml --source era5_east_asia \
                      --output stac_catalog
stac-builder validate catalogs/era5.yaml
stac-builder inspect  stac_catalog
```

### `build`

Runs preflight, then generates STAC for the requested source(s). With no
`--catalog`/`--source`, falls back to `build.targets` in the config file at
`$STAC_BUILDER_CONFIG` (or `config/main.yaml`).

Important flags:

| Flag                | Default     | Notes                                          |
| ------------------- | ----------- | ---------------------------------------------- |
| `--catalog`         | (required pair) | Path to catalog YAML                       |
| `--source`         | (required pair) | Source id within the catalog               |
| `--output`          | `stac_catalog/` | Where to write the static catalog         |
| `--clean`           | off          | `rm -rf` output first                         |
| `--update-root`     | on           | Regenerate top-level `catalog.json`           |
| `--parallel`        | on           | Build multiple sources in subprocesses         |
| `--skip-preflight`  | off          | Skip pre-build validation (not recommended)    |
| `--i18n`            | unset        | Reserved (multi-language bundle, not active yet) |

The CLI runs `preflight` automatically. If any checks fail, it prints **all**
issues (not just the first) and exits with code `2` before any generation
happens. This is the fail-fast contract — a cron container leaves a single
log; the user must be able to fix everything in one round-trip.

### `validate`

Run preflight on a catalog YAML only — no STAC output. Use this in CI or
locally before pushing a new catalog.

### `inspect`

Summarise an existing STAC output directory: collection count, item count,
collection paths. Useful sanity check after a cron build.

---

## Config (optional)

A `main.yaml` is **not required**. If your deployment repo wants to run the
CLI with a single command and no flags, it can provide one and point at it via
`STAC_BUILDER_CONFIG`:

```yaml
# main.yaml (optional)
project:
  id: my-stac-root
  title: "My STAC Catalog"
  description: "..."

filesystem:
  output_dir: "stac_catalog"
  link_strategy: "absolute"   # or "relative"

concurrency:
  max_workers: 5

build:
  targets:
    - catalog: "catalogs/era5.yaml"
      source: "era5_east_asia"
    - catalog: "catalogs/imerg.yaml"
      source: "imerg_final"
```

If absent, all defaults apply and the CLI requires `--catalog` + `--source` on
every invocation.

---

## Repo layout

```
src/
  cli.py            ← typer entry point
  settings.py       ← optional, lazy-loaded config
  core/
    builder.py        ← dispatcher (raster / vector / xarray)
    preflight.py      ← fail-fast checks
    event_collection.py
    root_catalog.py
    validator.py
  generator/
    base.py            ← StacGenerator ABC
    intake_xarray.py   ← Zarr/NetCDF/Parquet
    intake_raster.py   ← GeoTIFF / COG
    intake_vector.py   ← Shapefile / GeoParquet
    raster_utils.py
    thumbnails.py
    assets.py
    model.py           ← Pydantic metadata schema
    utils.py
config/catalogs/template/  ← copy these when adding a new dataset
docs/
tests/                      ← 70 tests, synthetic fixtures only
```

---

## What this repo does **not** do

- ❌ Serve HTTP / run a UI (use stac-server)
- ❌ Ship a Dockerfile (deployment repo decides)
- ❌ Hold lab-specific dataset catalogs (era5/imerg/... live in deployment repos)
- ❌ Watch directories / run as a daemon (run it from cron / a systemd timer)
- ❌ Implement i18n yet (schema reserves `i18n_key`; resolution coming)
