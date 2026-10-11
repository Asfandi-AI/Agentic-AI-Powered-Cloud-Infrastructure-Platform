from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_core import ErrorDetails

from self_service_platform.catalog.catalog import Catalog, CatalogError
from self_service_platform.catalog.component import Component
from self_service_platform.core.manifest import Manifest, ManifestEntry


class ManifestValidationIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    position: int
    name: str
    message: str


class ManifestValidationError(Exception):
    """Raised when a manifest fails catalog or options checks."""

    def __init__(self, issues: list[ManifestValidationIssue]) -> None:
        self.issues = issues
        detail = "; ".join(
            f"position {issue.position} {issue.name}: {issue.message}"
            for issue in issues
        )
        super().__init__(detail)


class ValidatedEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    component: Component
    options: BaseModel


class ValidatedManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    catalog_version: str
    entries: tuple[ValidatedEntry, ...]


def validate_manifest(manifest: Manifest, catalog: Catalog) -> ValidatedManifest:
    issues: list[ManifestValidationIssue] = []
    entries: list[ValidatedEntry] = []
    for position, entry in enumerate(manifest.root, start=1):
        validated, entry_issues = _validate_entry(entry, catalog, position)
        issues.extend(entry_issues)
        if validated is not None:
            entries.append(validated)
    if issues:
        raise ManifestValidationError(issues)
    return ValidatedManifest(
        catalog_version=catalog.catalog_version,
        entries=tuple(entries),
    )


def _validate_entry(
    entry: ManifestEntry,
    catalog: Catalog,
    position: int,
) -> tuple[ValidatedEntry | None, list[ManifestValidationIssue]]:
    try:
        component = catalog.get(entry.type)
    except CatalogError:
        issue = ManifestValidationIssue(
            position=position,
            name=entry.name,
            message=f"unknown component type {entry.type}",
        )
        return None, [issue]
    try:
        options = component.options_model.model_validate(dict(entry.options))
    except ValidationError as exc:
        return None, _option_issues(exc, position, entry.name)
    validated = ValidatedEntry(name=entry.name, component=component, options=options)
    return validated, []


def _option_issues(
    exc: ValidationError,
    position: int,
    name: str,
) -> list[ManifestValidationIssue]:
    return [
        ManifestValidationIssue(
            position=position,
            name=name,
            message=_option_message(error),
        )
        for error in exc.errors()
    ]


def _option_message(error: ErrorDetails) -> str:
    option = _option_name(error.get("loc", ()))
    if option is None:
        return "invalid options"
    if error.get("type") == "extra_forbidden":
        return f"unknown option {option}"
    if error.get("type") == "missing":
        return f"missing option {option}"
    return f"invalid option {option}"


def _option_name(location: tuple[int | str, ...]) -> str | None:
    if not location:
        return None
    head = location[0]
    if isinstance(head, str):
        return head
    return None
