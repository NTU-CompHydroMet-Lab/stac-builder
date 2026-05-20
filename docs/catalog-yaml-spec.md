# Catalog YAML Spec

stac-builder consumes Intake-style YAML files. One file may declare multiple
sources; each source becomes one STAC Collection.

## Minimal shape

```yaml
metadata:                       # top-level catalog metadata (optional)
  catalog_name: "ERA5"          # used to derive group_id for the root catalog
  description: "ERA5 reanalysis"
  catalogs_keywords: ["reanalysis"]

sources:
  era5_east_asia:               # source id (-> STAC collection id default)
    driver: zarr                # zarr | netcdf | parquet | raster | vector
    args:
      urlpath: "/data/era5/*.zarr"
      consolidated: true        # zarr-specific
    metadata:
      id: "era5_east_asia"      # REQUIRED; the STAC collection id
      description: "Hourly..."  # REQUIRED; goes into the collection description
      title: "ERA5 East Asia"
      category: "REANALYSIS"
      keywords: ["era5"]
      providers:
        - name: "ECMWF"
          roles: ["producer"]
      thumbnail_variable: "t2m"    # generator picks one timestep & renders PNG
      thumbnail_datetime: "2024-07-01T00:00:00Z"  # optional
```

## Required fields

Preflight enforces:

| Field                       | Why                                                     |
| --------------------------- | ------------------------------------------------------- |
| `sources` (non-empty dict)  | Nothing to build otherwise                              |
| `sources.<id>.driver`       | Tells generator which backend to use                    |
| `sources.<id>.args.urlpath` | Where the data lives                                    |
| `sources.<id>.metadata.id`  | STAC collection id                                      |
| `sources.<id>.metadata.description` | STAC description                                |

Plus per-generator extras:

- `raster` driver requires `args.asset_map` (one entry per TIFF stem).

## Choosing a generator

Resolution order (first match wins):

1. `sources.<id>.metadata.generator` — explicit, recommended for raster/vector
2. `metadata.generator` — top-level catalog default
3. `sources.<id>.driver` — if `raster` or `vector`, that wins
4. Fallback: `xarray` (handles `zarr`, `netcdf`, `parquet`)

## Grouping

If you set `metadata.catalog_name`, the engine normalises it to a `group_id`
and every source in that file gets grouped under one root-catalog child.
Explicit `metadata.group_id` overrides this.

## Reserved fields

- `i18n_key` — accepted in source metadata; **not yet consumed**. Future
  versions will resolve title/description from an external i18n bundle.
  Adding it today is forward-compatible.

## Templates

See `config/catalogs/template/`:

- `template_netcdf.yaml`
- `template_zarr.yaml`
- `template_geotiff.yaml`
- `template_vector.yaml`
- `station_intake_catalog.yaml` (parquet station data)
