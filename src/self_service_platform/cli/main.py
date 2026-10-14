from decimal import Decimal
from pathlib import Path
from typing import NoReturn

import typer

from self_service_platform.catalog.catalog import Catalog, build_catalog
from self_service_platform.core.cost import EstimatedCost, estimate_cost
from self_service_platform.core.diff import ManifestDiff, compute_diff
from self_service_platform.core.manifest import (
    Manifest,
    ManifestLoadError,
    load_manifest_from_file,
)
from self_service_platform.core.validator import (
    ManifestValidationError,
    ValidatedManifest,
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

    @app.command("diff")
    def diff_command(old: Path, new: Path) -> None:
        """Show the difference between two manifests."""
        applied = _load_manifest(old, name_load_errors=True)
        proposed = _load_manifest(new, name_load_errors=True)
        old_manifest, old_issues = _validated_or_issues(applied, catalog, old)
        new_manifest, new_issues = _validated_or_issues(proposed, catalog, new)
        issues = [*old_issues, *new_issues]
        if old_manifest is None or new_manifest is None:
            for line in issues:
                typer.echo(line, err=True)
            raise typer.Exit(1)
        _print_diff(compute_diff(applied=old_manifest, proposed=new_manifest))

    return app


def _load_manifest(path: Path, *, name_load_errors: bool = False) -> Manifest:
    try:
        return load_manifest_from_file(path)
    except (OSError, UnicodeDecodeError):
        typer.echo(f"cannot read manifest: {path}", err=True)
        raise typer.Exit(2) from None
    except ManifestLoadError as exc:
        message = f"{path}: {exc}" if name_load_errors else str(exc)
        typer.echo(message, err=True)
        raise typer.Exit(1) from None


def _validated_or_issues(
    manifest: Manifest,
    catalog: Catalog,
    path: Path,
) -> tuple[ValidatedManifest | None, list[str]]:
    try:
        return validate_manifest(manifest, catalog), []
    except ManifestValidationError as exc:
        lines = [
            f"{path}: position {issue.position} {issue.name}: {issue.message}"
            for issue in exc.issues
        ]
        return None, lines


def _print_diff(diff: ManifestDiff) -> None:
    if diff.is_empty:
        typer.echo("no changes")
        return
    for added_entry in diff.added:
        typer.echo(f"added: {added_entry.name} {added_entry.component_type}")
    for removed_entry in diff.removed:
        typer.echo(
            f"removed: {removed_entry.name} {removed_entry.component_type} "
            "(data in this component will be deleted)"
        )
    for changed_entry in diff.changed:
        options = ", ".join(changed_entry.changed_options)
        name = changed_entry.name
        component_type = changed_entry.component_type
        line = f"changed: {name} {component_type} options: {options}"
        if changed_entry.replacement_forcing:
            line = f"{line} [REPLACEMENT]"
        typer.echo(line)
    added = len(diff.added)
    removed = len(diff.removed)
    changed = len(diff.changed)
    typer.echo(f"summary: {added} added, {removed} removed, {changed} changed")


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
