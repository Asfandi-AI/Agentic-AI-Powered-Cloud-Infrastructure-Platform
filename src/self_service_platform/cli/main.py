import getpass
import sys
from collections.abc import Callable
from datetime import datetime
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
from self_service_platform.core.registry import Registry
from self_service_platform.core.service import (
    ConfirmationRequiredError,
    CreatePlan,
    DestroyFailedError,
    DestroyNotAllowedError,
    DestroyPlan,
    PlatformService,
    ProvisionFailedError,
    StalePlanError,
    StoredManifestInvalidError,
    UpdateConfirmations,
    UpdateFailedError,
    UpdateNotAllowedError,
    UpdatePlan,
)
from self_service_platform.core.tenant import (
    DuplicateTenantError,
    Initiator,
    InitiatorKind,
    RegistryUnavailableError,
    SspHomeError,
    TenantNotFoundError,
    TenantRecord,
    TenantType,
    TransitionRejectedError,
)
from self_service_platform.core.validator import (
    ManifestValidationError,
    ManifestValidationIssue,
    ValidatedManifest,
    validate_manifest,
)


def create_app(
    catalog: Catalog,
    open_registry: Callable[[], Registry] | None = None,
    open_service: Callable[[], PlatformService] | None = None,
) -> typer.Typer:
    app = typer.Typer(
        add_completion=False,
        rich_markup_mode=None,
        pretty_exceptions_enable=False,
    )
    registry_factory = open_registry
    service_factory = open_service

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

    @app.command("tenants")
    def tenants_command() -> None:
        """List every tenant."""
        registry = _open_state(registry_factory)
        try:
            records = _list_tenants(registry)
        finally:
            _close_opened(registry)
        _echo_tenants(records)

    @app.command("status")
    def status_command(tenant_id: str) -> None:
        """Show one tenant."""
        registry = _open_state(registry_factory)
        try:
            record = _load_tenant(registry, tenant_id)
        finally:
            _close_opened(registry)
        _echo_status(record)

    @app.command("create")
    def create_command(
        tenant_id: str,
        manifest_path: Path,
        tenant_type: TenantType = _TENANT_TYPE,
        owner: str = _OWNER,
        yes: bool = _YES,
        plan_only: bool = _PLAN_ONLY,
    ) -> None:
        """Create a tenant from a manifest."""
        _reject_tenant_id(tenant_id)
        _reject_owner(owner)
        username = _operating_system_user()
        manifest = _load_manifest(manifest_path)
        _require_valid_manifest(manifest, catalog)
        service = _open_service(service_factory)
        plan = _plan_create(service, tenant_id, tenant_type, owner, manifest)
        _print_create_plan(plan)
        if plan_only:
            typer.echo("plan only: nothing was written")
            return
        if not _create_confirmed(yes):
            raise typer.Exit(1)
        record = _apply_create(service, plan, _cli_initiator(username))
        _echo_created(record)

    @app.command("update")
    def update_command(
        tenant_id: str,
        manifest_path: Path,
        yes: bool = _YES,
        confirm_removals: bool = _CONFIRM_REMOVALS,
        confirm_replacement: list[str] | None = _CONFIRM_REPLACEMENT,
        plan_only: bool = _PLAN_ONLY,
    ) -> None:
        """Update a tenant from a manifest."""
        username = _operating_system_user()
        manifest = _load_manifest(manifest_path)
        _require_valid_manifest(manifest, catalog)
        service = _open_service(service_factory)
        plan = _plan_update(service, tenant_id, manifest)
        status = _stored_status(registry_factory, tenant_id)
        _print_update_plan(plan, status)
        if plan.nothing_to_change or plan_only:
            return
        confirmations = _update_confirmations(
            plan,
            yes,
            confirm_removals,
            _replacement_names(confirm_replacement),
        )
        record = _apply_update_command(
            service,
            plan,
            confirmations,
            _cli_initiator(username),
        )
        _echo_updated(record)

    @app.command("destroy")
    def destroy_command(
        tenant_id: str,
        yes: bool = _YES,
        plan_only: bool = _PLAN_ONLY,
    ) -> None:
        """Destroy a tenant."""
        username = _operating_system_user()
        service = _open_service(service_factory)
        plan = _plan_destroy(service, tenant_id)
        _print_destroy_plan(plan)
        if plan_only:
            return
        _destroy_confirmed(plan.tenant_id, yes)
        record = _apply_destroy_command(service, plan, _cli_initiator(username))
        _echo_destroyed(record)

    return app


def _open_state(factory: Callable[[], Registry] | None) -> Registry:
    if factory is None:
        raise RuntimeError("registry is not configured")
    try:
        return factory()
    except SspHomeError as exc:
        typer.echo(f"SSP_HOME must be absolute: {exc.value}", err=True)
        raise typer.Exit(2) from None
    except RegistryUnavailableError as exc:
        _reject_unreadable_state(exc)


def _list_tenants(registry: Registry) -> tuple[TenantRecord, ...]:
    try:
        return registry.list_tenants()
    except RegistryUnavailableError as exc:
        _reject_unreadable_state(exc)


def _load_tenant(registry: Registry, tenant_id: str) -> TenantRecord:
    try:
        return registry.get(tenant_id)
    except TenantNotFoundError:
        typer.echo(f"no such tenant: {_escape_stored(tenant_id)}", err=True)
        raise typer.Exit(1) from None
    except RegistryUnavailableError as exc:
        _reject_unreadable_state(exc)


def _reject_unreadable_state(exc: RegistryUnavailableError) -> NoReturn:
    typer.echo(f"cannot read state: {exc.path}", err=True)
    raise typer.Exit(2) from None


def _close_opened(registry: Registry) -> None:
    close = getattr(registry, "close", None)
    if callable(close):
        close()


def _echo_tenants(records: tuple[TenantRecord, ...]) -> None:
    if not records:
        typer.echo("no tenants")
        return
    for record in records:
        typer.echo(_tenant_line(record))


def _tenant_line(record: TenantRecord) -> str:
    return " ".join(
        (
            _escape_stored(record.tenant_id),
            _escape_stored(record.status.value),
            _escape_stored(record.tenant_type.value),
            _escape_stored(record.owner),
            _expires(record.expires_at),
        )
    )


def _echo_status(record: TenantRecord) -> None:
    for line in _status_lines(record):
        typer.echo(line)


def _status_lines(record: TenantRecord) -> tuple[str, ...]:
    initiator = record.initiator
    lines = [
        f"status: {_escape_stored(record.status.value)}",
        f"tenant_type: {_escape_stored(record.tenant_type.value)}",
        f"owner: {_escape_stored(record.owner)}",
        (
            "initiator: "
            f"{_escape_stored(initiator.kind.value)} {_escape_stored(initiator.owner)}"
        ),
        f"catalog_version: {_escape_stored(record.catalog_version)}",
        f"expires_at: {_expires(record.expires_at)}",
        f"last_error: {_error(record.last_error)}",
    ]
    components = tuple(
        f"{_escape_stored(entry.name)} {_escape_stored(entry.type)}"
        for entry in record.manifest
    )
    return tuple(lines) + components


def _expires(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.isoformat()


def _error(value: str | None) -> str:
    if value is None:
        return "-"
    return _escape_stored(value)


def _escape_stored(text: str) -> str:
    return "".join(_escape_character(character) for character in text)


def _escape_character(character: str) -> str:
    if character == "\\":
        return "\\\\"
    if character == "\n":
        return "\\n"
    if character == "\r":
        return "\\r"
    if character == "\t":
        return "\\t"
    code = ord(character)
    if code < 32 or code == 127:
        return f"\\x{code:02x}"
    if code > 126:
        if code <= 0xFFFF:
            return f"\\u{code:04x}"
        return f"\\U{code:08x}"
    return character


def _reject_tenant_id(tenant_id: str) -> None:
    if _rejected_text(tenant_id):
        typer.echo(f"invalid tenant id: {_escape_stored(tenant_id)}", err=True)
        raise typer.Exit(2)


def _reject_owner(owner: str) -> None:
    if _rejected_text(owner):
        typer.echo(f"invalid owner: {_escape_stored(owner)}", err=True)
        raise typer.Exit(2)


def _rejected_text(value: str) -> bool:
    if value.strip() == "":
        return True
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _operating_system_user() -> str:
    try:
        username = getpass.getuser()
    except (OSError, KeyError, ImportError):
        _reject_unknown_user()
    if username.strip() == "":
        _reject_unknown_user()
    return username


def _reject_unknown_user() -> NoReturn:
    typer.echo("cannot determine the operating system user", err=True)
    raise typer.Exit(2)


def _stdin_is_terminal() -> bool:
    return sys.stdin.isatty()


def _open_service(factory: Callable[[], PlatformService] | None) -> PlatformService:
    if factory is None:
        raise RuntimeError("service is not configured")
    try:
        return factory()
    except SspHomeError as exc:
        typer.echo(f"SSP_HOME must be absolute: {exc.value}", err=True)
        raise typer.Exit(2) from None


def _require_valid_manifest(manifest: Manifest, catalog: Catalog) -> None:
    try:
        validate_manifest(manifest, catalog)
    except ManifestValidationError as exc:
        _report_create_issues(exc)


def _plan_create(
    service: PlatformService,
    tenant_id: str,
    tenant_type: TenantType,
    owner: str,
    manifest: Manifest,
) -> CreatePlan:
    try:
        return service.plan_create(tenant_id, tenant_type, owner, manifest)
    except ManifestValidationError as exc:
        _report_create_issues(exc)


def _print_create_plan(plan: CreatePlan) -> None:
    typer.echo(f"tenant_id: {_escape_stored(plan.tenant_id)}")
    typer.echo(f"tenant_type: {_escape_stored(plan.tenant_type.value)}")
    typer.echo(f"owner: {_escape_stored(plan.owner)}")
    _print_plan_estimate(plan.estimated_cost)
    typer.echo(f"expires_at: {_expires(plan.expires_at)}")


def _print_plan_estimate(estimate: EstimatedCost) -> None:
    for component in estimate.components:
        name = _escape_stored(component.name)
        component_type = _escape_stored(component.component_type)
        typer.echo(f"{name} {component_type}")
        for item in component.line_items:
            description = _escape_stored(item.description)
            typer.echo(f"  {description} {_format_amount(item.amount)}")
        typer.echo(f"  total: {_format_amount(component.total)}")
    typer.echo(f"manifest total: {_format_amount(estimate.total)}")
    typer.echo(estimate.label)


def _create_confirmed(yes: bool) -> bool:
    if yes:
        return True
    if not _stdin_is_terminal():
        typer.echo("confirmation required; pass --yes", err=True)
        raise typer.Exit(1)
    typer.echo("Create this tenant? [y/N]")
    try:
        answer = input()
    except EOFError:
        return False
    return answer.strip().casefold() in {"y", "yes"}


def _cli_initiator(username: str) -> Initiator:
    return Initiator(kind=InitiatorKind.CLI, owner=username)


def _apply_create(
    service: PlatformService,
    plan: CreatePlan,
    initiator: Initiator,
) -> TenantRecord:
    try:
        return service.apply_create(plan, True, initiator).record
    except ManifestValidationError as exc:
        _report_create_issues(exc)
    except (ConfirmationRequiredError, StalePlanError, DuplicateTenantError) as exc:
        typer.echo(_escape_stored(str(exc)), err=True)
        raise typer.Exit(1) from None
    except ProvisionFailedError as exc:
        typer.echo(_escape_stored(str(exc)), err=True)
        tenant_id = _escape_stored(plan.tenant_id)
        typer.echo(
            f"tenant is left in status creating; see ssp status {tenant_id}",
            err=True,
        )
        raise typer.Exit(1) from None
    except RegistryUnavailableError as exc:
        typer.echo(f"cannot write state: {exc.path}", err=True)
        raise typer.Exit(2) from None


def _echo_created(record: TenantRecord) -> None:
    tenant_id = _escape_stored(record.tenant_id)
    status = _escape_stored(record.status.value)
    typer.echo(f"created: {tenant_id} status {status}")
    _echo_status(record)


def _report_create_issues(error: ManifestValidationError) -> NoReturn:
    _report_issues(error.issues)


def _report_issues(issues: list[ManifestValidationIssue]) -> NoReturn:
    for issue in issues:
        name = _escape_stored(issue.name)
        message = _escape_stored(issue.message)
        typer.echo(f"position {issue.position} {name}: {message}", err=True)
    raise typer.Exit(1)


def _stored_status(factory: Callable[[], Registry] | None, tenant_id: str) -> str:
    registry = _open_state(factory)
    try:
        record = _load_tenant(registry, tenant_id)
    finally:
        _close_opened(registry)
    return record.status.value


def _plan_update(service: PlatformService, tenant_id: str, manifest: Manifest) -> UpdatePlan:
    try:
        return service.plan_update(tenant_id, manifest)
    except TenantNotFoundError:
        _missing_tenant(tenant_id)
    except UpdateNotAllowedError as exc:
        _echo_service_error(exc)
    except StoredManifestInvalidError as exc:
        _report_issues(exc.issues)
    except ManifestValidationError as exc:
        _report_issues(exc.issues)
    except RegistryUnavailableError as exc:
        _reject_unwritable_state(exc)


def _print_update_plan(plan: UpdatePlan, status: str) -> None:
    typer.echo(f"tenant_id: {_escape_stored(plan.tenant_id)}")
    typer.echo(f"status: {_escape_stored(status)}")
    _print_update_diff(plan.diff)
    _print_plan_estimate(plan.estimated_cost)
    typer.echo(f"removals: {_names_or_dash(plan.removals)}")
    typer.echo(f"replacements: {_names_or_dash(plan.replacements)}")


def _print_update_diff(diff: ManifestDiff) -> None:
    if diff.is_empty:
        typer.echo("no changes")
        return
    for added_entry in diff.added:
        name = _escape_stored(added_entry.name)
        component_type = _escape_stored(added_entry.component_type)
        typer.echo(f"added: {name} {component_type}")
    for removed_entry in diff.removed:
        name = _escape_stored(removed_entry.name)
        component_type = _escape_stored(removed_entry.component_type)
        typer.echo(
            f"removed: {name} {component_type} "
            "(data in this component will be deleted)"
        )
    for changed_entry in diff.changed:
        options = ", ".join(
            _escape_stored(option) for option in changed_entry.changed_options
        )
        name = _escape_stored(changed_entry.name)
        component_type = _escape_stored(changed_entry.component_type)
        line = f"changed: {name} {component_type} options: {options}"
        if changed_entry.replacement_forcing:
            line = f"{line} [REPLACEMENT]"
        typer.echo(line)
    added = len(diff.added)
    removed = len(diff.removed)
    changed = len(diff.changed)
    typer.echo(f"summary: {added} added, {removed} removed, {changed} changed")


def _names_or_dash(names: tuple[str, ...]) -> str:
    if len(names) == 0:
        return "-"
    return ", ".join(_escape_stored(name) for name in names)


def _replacement_names(value: list[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _update_confirmations(
    plan: UpdatePlan,
    yes: bool,
    confirm_removals: bool,
    replacement_names: tuple[str, ...],
) -> UpdateConfirmations:
    confirmed = set(replacement_names)
    if not _stdin_is_terminal():
        missing = _missing_update_flags(plan, yes, confirm_removals, confirmed)
        if len(missing) > 0:
            typer.echo(f"confirmation required; pass {' '.join(missing)}", err=True)
            raise typer.Exit(1)
        return _confirmations(yes, confirm_removals, confirmed)
    update_ok = yes
    if not update_ok:
        typer.echo("Apply this update? [y/N]")
        if not _answer_is_yes():
            raise typer.Exit(1)
        update_ok = True
    removals_ok = confirm_removals
    if plan.removals and not removals_ok:
        shown = _names_or_dash(plan.removals)
        typer.echo(f"Delete the data of these components: {shown}? [y/N]")
        if not _answer_is_yes():
            raise typer.Exit(1)
        removals_ok = True
    for name in plan.replacements:
        if name in confirmed:
            continue
        typer.echo(f"Recreate {_escape_stored(name)} (its data is lost)? [y/N]")
        if not _answer_is_yes():
            raise typer.Exit(1)
        confirmed.add(name)
    return _confirmations(update_ok, removals_ok, confirmed)


def _missing_update_flags(
    plan: UpdatePlan,
    yes: bool,
    confirm_removals: bool,
    confirmed: set[str],
) -> list[str]:
    missing: list[str] = []
    if not yes:
        missing.append("--yes")
    if plan.removals and not confirm_removals:
        missing.append("--confirm-removals")
    missing.extend(
        f"--confirm-replacement {_escape_stored(name)}"
        for name in plan.replacements
        if name not in confirmed
    )
    return missing


def _confirmations(
    update: bool,
    removals: bool,
    confirmed: set[str],
) -> UpdateConfirmations:
    if update:
        update_flag = True
    else:
        update_flag = False
    if removals:
        removals_flag = True
    else:
        removals_flag = False
    return UpdateConfirmations(
        update=update_flag,
        removals=removals_flag,
        replacements=frozenset(confirmed),
    )


def _answer_is_yes() -> bool:
    try:
        answer = input()
    except EOFError:
        return False
    return answer.strip().casefold() in {"y", "yes"}


def _apply_update_command(
    service: PlatformService,
    plan: UpdatePlan,
    confirmations: UpdateConfirmations,
    initiator: Initiator,
) -> TenantRecord:
    try:
        return service.apply_update(plan, confirmations, initiator).record
    except TenantNotFoundError:
        _missing_tenant(plan.tenant_id)
    except (UpdateNotAllowedError, ConfirmationRequiredError, StalePlanError) as exc:
        _echo_service_error(exc)
    except StoredManifestInvalidError as exc:
        _report_issues(exc.issues)
    except ManifestValidationError as exc:
        _report_issues(exc.issues)
    except UpdateFailedError as exc:
        _echo_failed_update(plan.tenant_id, exc)
    except TransitionRejectedError:
        _lost_write()
    except RegistryUnavailableError as exc:
        _reject_unwritable_state(exc)


def _echo_updated(record: TenantRecord) -> None:
    tenant_id = _escape_stored(record.tenant_id)
    status = _escape_stored(record.status.value)
    typer.echo(f"updated: {tenant_id} status {status}")
    _echo_status(record)


def _echo_failed_update(tenant_id: str, exc: UpdateFailedError) -> NoReturn:
    typer.echo(_escape_stored(str(exc)), err=True)
    shown = _escape_stored(tenant_id)
    typer.echo(
        f"tenant is in status update_failed; see ssp status {shown}",
        err=True,
    )
    raise typer.Exit(1) from None


def _plan_destroy(service: PlatformService, tenant_id: str) -> DestroyPlan:
    try:
        return service.plan_destroy(tenant_id)
    except TenantNotFoundError:
        _missing_tenant(tenant_id)
    except DestroyNotAllowedError as exc:
        _echo_service_error(exc)
    except RegistryUnavailableError as exc:
        _reject_unwritable_state(exc)


def _print_destroy_plan(plan: DestroyPlan) -> None:
    typer.echo(f"tenant_id: {_escape_stored(plan.tenant_id)}")
    typer.echo(f"tenant_type: {_escape_stored(plan.tenant_type.value)}")
    typer.echo(f"status: {_escape_stored(plan.status.value)}")
    for name in plan.component_names:
        typer.echo(_escape_stored(name))
    typer.echo(plan.statement)


def _destroy_confirmed(tenant_id: str, yes: bool) -> None:
    if yes:
        return
    if not _printable_ascii(tenant_id):
        typer.echo(
            "cannot confirm this tenant id interactively; pass --yes",
            err=True,
        )
        raise typer.Exit(1)
    if not _stdin_is_terminal():
        typer.echo("confirmation required; pass --yes", err=True)
        raise typer.Exit(1)
    typer.echo("Type the tenant id to confirm destroy:")
    try:
        answer = input()
    except EOFError:
        raise typer.Exit(1) from None
    if answer != tenant_id:
        raise typer.Exit(1)


def _printable_ascii(value: str) -> bool:
    return all(32 <= ord(character) <= 126 for character in value)


def _apply_destroy_command(
    service: PlatformService,
    plan: DestroyPlan,
    initiator: Initiator,
) -> TenantRecord:
    try:
        return service.apply_destroy(plan, True, initiator).record
    except TenantNotFoundError:
        _missing_tenant(plan.tenant_id)
    except (DestroyNotAllowedError, ConfirmationRequiredError, StalePlanError) as exc:
        _echo_service_error(exc)
    except DestroyFailedError as exc:
        _echo_failed_destroy(plan.tenant_id, exc)
    except TransitionRejectedError:
        _lost_write()
    except RegistryUnavailableError as exc:
        _reject_unwritable_state(exc)


def _echo_destroyed(record: TenantRecord) -> None:
    tenant_id = _escape_stored(record.tenant_id)
    status = _escape_stored(record.status.value)
    typer.echo(f"destroyed: {tenant_id} status {status}")
    _echo_status(record)


def _echo_failed_destroy(tenant_id: str, exc: DestroyFailedError) -> NoReturn:
    typer.echo(_escape_stored(str(exc)), err=True)
    shown = _escape_stored(tenant_id)
    typer.echo(
        f"tenant is in status destroy_failed; see ssp status {shown}",
        err=True,
    )
    raise typer.Exit(1) from None


def _missing_tenant(tenant_id: str) -> NoReturn:
    typer.echo(f"no such tenant: {_escape_stored(tenant_id)}", err=True)
    raise typer.Exit(1) from None


def _echo_service_error(exc: Exception) -> NoReturn:
    typer.echo(_escape_stored(str(exc)), err=True)
    raise typer.Exit(1) from None


def _lost_write() -> NoReturn:
    typer.echo(
        "tenant changed while the command was running; "
        "no change was made by this command",
        err=True,
    )
    raise typer.Exit(1) from None


def _reject_unwritable_state(exc: RegistryUnavailableError) -> NoReturn:
    typer.echo(f"cannot write state: {exc.path}", err=True)
    raise typer.Exit(2) from None


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


_TENANT_TYPE = typer.Option(..., "--type")
_OWNER = typer.Option(...)
_YES = typer.Option(False, "--yes")
_CONFIRM_REMOVALS = typer.Option(False, "--confirm-removals")
_CONFIRM_REPLACEMENT = typer.Option(None, "--confirm-replacement")
_PLAN_ONLY = typer.Option(False, "--plan-only")


app = create_app(build_catalog())
