# Deployment Guide

For people writing the **deployment repo** that wraps stac-builder in a
container and schedules it.

## What to put in your deployment repo

```
my-deploy-repo/
├─ Dockerfile                ← yours, builds an image around stac-builder
├─ podman-compose.yml        ← or systemd .container/.timer files
├─ catalogs/                 ← your lab's actual catalog YAMLs
│   ├─ era5.yaml
│   ├─ imerg.yaml
│   └─ ...
├─ main.yaml                 ← optional; lists which sources to build
└─ scripts/                  ← optional helpers
```

## Minimal Dockerfile

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgdal-dev g++ gcc libhdf5-dev git \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir uv

# Clone the engine at a pinned commit/tag.
ARG STAC_BUILDER_REF=main
RUN git clone --depth 1 --branch ${STAC_BUILDER_REF} \
        https://github.com/<org>/stac-builder.git .
RUN uv sync --frozen --no-dev

ENTRYPOINT ["uv", "run", "python", "-m", "src.cli"]
CMD ["--help"]
```

## Running build from a deployment

Option A — one-shot CLI args:

```bash
podman run --rm \
  -v $(pwd)/catalogs:/catalogs:ro \
  -v /home/NAS:/home/NAS:ro \
  -v stac_output:/output \
  my-stac-builder \
  build --catalog /catalogs/era5.yaml \
        --source era5_east_asia \
        --output /output
```

Option B — config-driven (one command, multi-source):

```bash
podman run --rm \
  -v $(pwd)/main.yaml:/app/config/main.yaml:ro \
  -v $(pwd)/catalogs:/catalogs:ro \
  -v /home/NAS:/home/NAS:ro \
  -v stac_output:/output \
  -e STAC_BUILDER_CONFIG=/app/config/main.yaml \
  my-stac-builder \
  build --output /output
```

## Scheduling

Choose one — the engine does not care:

- **cron** inside the container (less typical)
- **systemd .timer** that runs the `podman run …` command above
- **podman quadlet** with a `.timer` unit
- **GitHub Actions / GitLab CI** scheduled job that pushes to a volume

## How preflight helps in a scheduled context

Every scheduled run starts with preflight. If urlpaths are stale (lab data
moved) or schema is wrong (someone edited a YAML), the build exits with code
2 and prints a structured report **before** any work. Pipe that into your
alerting (Slack webhook on non-zero exit, etc.) and you have a working
"catalog health monitor" for free.

## Server side (a teaser)

The static output is just JSON files; serving it is a separate concern. The
companion **stac-server** repo will provide an nginx-or-FastAPI image that
mounts the same volume read-only and exposes the catalog over HTTP. Keep the
two repos versioned independently.
