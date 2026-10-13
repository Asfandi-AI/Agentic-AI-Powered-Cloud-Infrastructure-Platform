from decimal import Decimal
from pathlib import Path
from typing import NoReturn

import typer

from self_service_platform.catalog.catalog import Catalog, build_catalog
from self_service_platform.core.cost import EstimatedCost, estimate_cost
from self_service_platform.core.manifest import (
    Manifest,
    ManifestLoadError,
    load_manifest_from_file,
)
from self_service_platform.core.validator import (
    ManifestValidationError,
    validate_manifest,
)


def create_app(catalog: Catalog) -> typer.Typer:
    app = typer.Typer(
        add_completion=False,
        rich_markup_mode=None,
        pretty_exceptions_enable=False,
    )

    @app.command("catalog")
    def catalog_command() -> None:
        """List every component."""
        for component in catalog.list_components():
            service = component.aws_service.value
            typer.echo(f"{component.component_type} {service} {component.description}")
        typer.echo(f"catalog_version: {catalog.catalog_version}")

    @app.command("validate")
    def validate_command(path: Path) -> None:
        """Validate a manifest."""
        manifest = _load_manifest(path)
        try:
            validated = validate_manifest(manifest, catalog)
        except ManifestValidationError as exc:
            _report_validation_error(exc)
        typer.echo(f"OK: {len(validated.entries)} entries")

    @app.command("estimate")
    def estimate_command(path: Path) -> None:
        """Estimate the cost of a manifest."""
        manifest = _load_manifest(path)
        try:
            validated = validate_manifest(manifest, catalog)
        except ManifestValidationError as exc:
            _report_validation_error(exc)
        _print_estimate(estimate_cost(validated))

    return app


def _load_manifest(path: Path) -> Manifest:
    try:
        return load_manifest_from_file(path)
    except (OSError, UnicodeDecodeError):
        typer.echo(f"cannot read manifest: {path}", err=True)
        raise typer.Exit(2) from None
    except ManifestLoadError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


def _report_validation_error(error: ManifestValidationError) -> NoReturn:
    for issue in error.issues:
        typer.echo(f"position {issue.position} {issue.name}: {issue.message}", err=True)
    raise typer.Exit(1)


def _format_amount(amount: Decimal | None) -> str:
    if amount is None:
        return "not priced"
    return format(amount, "f")


def _print_estimate(estimate: EstimatedCost) -> None:
    for component in estimate.components:
        typer.echo(f"{component.name} {component.component_type}")
        for item in component.line_items:
            typer.echo(f"  {item.description} {_format_amount(item.amount)}")
        typer.echo(f"  total: {_format_amount(component.total)}")
    typer.echo(f"manifest total: {_format_amount(estimate.total)}")
    typer.echo(estimate.label)


app = create_app(build_catalog())
