"""stac-builder command-line entry point.

Three subcommands:

* ``build``    — run preflight, then generate STAC for one or more sources.
* ``validate`` — run only preflight on a catalog YAML (no generation).
* ``inspect``  — summarise an existing STAC catalog directory.

The CLI intentionally has **no ``serve`` command**: the static STAC catalog is
served by a separate repo (a thin FastAPI / nginx layer that mounts the
generated directory).
"""

from __future__ import annotations

import shutil
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Annotated, List, Optional

import typer
from loguru import logger
from pydantic import BaseModel
from rich.console import Console

from src.core.builder import build_collection
from src.core.preflight import PreflightReport, check_all, check_catalog_file
from src.core.root_catalog import update_root_catalog
from src.settings import get_settings

console = Console()
app = typer.Typer(
    help="stac-builder — generate static STAC catalogs from Intake YAML sources.",
    rich_markup_mode="rich",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class CatalogConfig(BaseModel):
    catalog_path: Path
    source_id: str
    model_config = {"frozen": True}


def _build_wrapper(config: CatalogConfig, output_dir: Path) -> str:
    """Top-level for ProcessPool pickling."""
    try:
        build_collection(config.catalog_path, config.source_id, output_dir)
        return config.source_id
    except Exception as e:
        raise RuntimeError(f"Build failed for {config.source_id}: {e}") from e


def _print_report(report: PreflightReport) -> None:
    if not report.issues:
        console.print("[bold green]Preflight: OK[/bold green]")
        return
    console.print("[bold]Preflight report:[/bold]")
    for issue in report.issues:
        color = "red" if issue.severity.value == "ERROR" else "yellow"
        prefix = f"[{color}]{issue.severity.value}[/{color}]"
        suffix = f"\n    [dim]hint: {issue.hint}[/dim]" if issue.hint else ""
        console.print(f"{prefix} {issue.source}: {issue.message}{suffix}")


def _resolve_targets(
    catalog: Optional[Path],
    source: Optional[str],
) -> List[CatalogConfig]:
    """Resolve build targets from CLI args or fall back to settings.build.targets."""
    if catalog and source:
        return [CatalogConfig(catalog_path=catalog, source_id=source)]
    if catalog or source:
        console.print("[red]Provide BOTH --catalog and --source, or NEITHER.[/red]")
        raise typer.Exit(code=2)

    settings = get_settings()
    if not settings or not settings.build.targets:
        console.print(
            "[red]No build targets found.[/red]\n"
            "Pass --catalog and --source, or provide a config file via "
            "$STAC_BUILDER_CONFIG with a 'build.targets' list."
        )
        raise typer.Exit(code=2)
    return [
        CatalogConfig(catalog_path=Path(t.catalog), source_id=t.source)
        for t in settings.build.targets
    ]


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
@app.command()
def build(
    catalog: Annotated[Optional[Path], typer.Option(help="Catalog YAML path")] = None,
    source: Annotated[Optional[str], typer.Option(help="Source id within the catalog")] = None,
    output: Annotated[Optional[Path], typer.Option(help="Output directory for the STAC catalog")] = None,
    clean: Annotated[bool, typer.Option(help="Wipe output directory before building")] = False,
    update_root: Annotated[bool, typer.Option(help="Update root catalog after building")] = True,
    parallel: Annotated[bool, typer.Option(help="Run multiple sources in parallel")] = True,
    skip_preflight: Annotated[bool, typer.Option(help="Skip preflight checks (not recommended)")] = False,
    i18n: Annotated[Optional[Path], typer.Option(help="(reserved) i18n bundle path; not used yet")] = None,
) -> None:
    """Build static STAC catalog from one or more Intake sources."""
    targets = _resolve_targets(catalog, source)

    settings = get_settings()
    project_root = Path(__file__).parent.parent
    output_dir = (
        output
        or (project_root / (settings.filesystem.output_dir if settings else "stac_catalog"))
    )

    if not skip_preflight:
        catalog_files = sorted({t.catalog_path for t in targets})
        only = [t.source_id for t in targets] if (catalog and source) else None
        report = check_all(catalog_files=catalog_files, output_dir=output_dir, only_sources=only)
        _print_report(report)
        if not report.ok:
            console.print("[red]Preflight failed; aborting build. Use --skip-preflight to force.[/red]")
            raise typer.Exit(code=2)

    if clean and output_dir.exists():
        logger.warning(f"Cleaning output directory: {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Building {len(targets)} target(s)...")
    failed: list[str] = []
    max_workers = (settings.concurrency.max_workers if settings else 5) if parallel else 1

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        future_to_source = {
            executor.submit(_build_wrapper, t, output_dir): t.source_id
            for t in targets
        }
        for fut in as_completed(future_to_source):
            sid = future_to_source[fut]
            try:
                fut.result()
                logger.success(f"[{sid}] Completed.")
            except Exception as e:
                logger.error(f"[{sid}] Failed: {e}")
                failed.append(sid)

    if update_root:
        logger.info("Updating root catalog...")
        try:
            update_root_catalog(output_dir)
            logger.success("Root catalog updated.")
        except Exception as e:
            logger.error(f"Failed to update root catalog: {e}")

    if failed:
        console.print(f"[red]Build failed for: {', '.join(failed)}[/red]")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
@app.command()
def validate(
    catalog: Annotated[Path, typer.Argument(help="Catalog YAML path to validate")],
    source: Annotated[Optional[str], typer.Option(help="Restrict checks to this source id")] = None,
) -> None:
    """Run preflight checks on a catalog YAML without building anything."""
    only = [source] if source else None
    report = check_catalog_file(catalog, only_sources=only)
    _print_report(report)
    if not report.ok:
        raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------
@app.command()
def inspect(
    stac_dir: Annotated[Path, typer.Argument(help="Existing STAC output directory")],
) -> None:
    """Summarise a generated STAC catalog directory (root + collections + items)."""
    root = stac_dir / "catalog.json"
    if not root.exists():
        console.print(f"[red]No catalog.json under {stac_dir}[/red]")
        raise typer.Exit(code=2)

    collections = list(stac_dir.rglob("collection.json"))
    items = list(stac_dir.rglob("items/*.json"))
    console.print(f"[bold]STAC root:[/bold] {root}")
    console.print(f"  collections : {len(collections)}")
    console.print(f"  items       : {len(items)}")
    for c in sorted(collections):
        console.print(f"    - {c.relative_to(stac_dir)}")


if __name__ == "__main__":
    app()
