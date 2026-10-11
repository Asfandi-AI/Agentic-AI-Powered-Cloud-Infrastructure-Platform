from pydantic import BaseModel, ConfigDict

from self_service_platform.core.validator import ValidatedEntry, ValidatedManifest

_ABSENT = object()


class AddedEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    component_type: str
    options: BaseModel


class RemovedEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    component_type: str
    options: BaseModel


class ChangedEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    component_type: str
    changed_options: tuple[str, ...]
    replacement_forcing: bool


class ManifestDiff(BaseModel):
    model_config = ConfigDict(frozen=True)

    added: tuple[AddedEntry, ...]
    removed: tuple[RemovedEntry, ...]
    changed: tuple[ChangedEntry, ...]

    @property
    def is_empty(self) -> bool:
        return not self.added and not self.removed and not self.changed

    @property
    def removals(self) -> tuple[RemovedEntry, ...]:
        return self.removed

    @property
    def replacements(self) -> tuple[ChangedEntry, ...]:
        return tuple(entry for entry in self.changed if entry.replacement_forcing)


def compute_diff(
    applied: ValidatedManifest,
    proposed: ValidatedManifest,
) -> ManifestDiff:
    applied_by_name = _by_name(applied)
    proposed_by_name = _by_name(proposed)
    return ManifestDiff(
        added=_added(proposed, applied_by_name),
        removed=_removed(applied, proposed_by_name),
        changed=_changed(proposed, applied_by_name),
    )


def _by_name(manifest: ValidatedManifest) -> dict[str, ValidatedEntry]:
    return {entry.name: entry for entry in manifest.entries}


def _added(
    proposed: ValidatedManifest,
    applied_by_name: dict[str, ValidatedEntry],
) -> tuple[AddedEntry, ...]:
    entries = [
        _added_entry(entry)
        for entry in proposed.entries
        if _is_different_type(entry, applied_by_name)
    ]
    return tuple(entries)


def _removed(
    applied: ValidatedManifest,
    proposed_by_name: dict[str, ValidatedEntry],
) -> tuple[RemovedEntry, ...]:
    entries = [
        _removed_entry(entry)
        for entry in applied.entries
        if _is_different_type(entry, proposed_by_name)
    ]
    return tuple(entries)


def _changed(
    proposed: ValidatedManifest,
    applied_by_name: dict[str, ValidatedEntry],
) -> tuple[ChangedEntry, ...]:
    entries: list[ChangedEntry] = []
    for entry in proposed.entries:
        previous = applied_by_name.get(entry.name)
        if previous is None:
            continue
        if previous.component.component_type != entry.component.component_type:
            continue
        names = _changed_option_names(previous.options, entry.options)
        if len(names) == 0:
            continue
        entries.append(_changed_entry(entry, names))
    return tuple(entries)


def _is_different_type(
    entry: ValidatedEntry,
    others: dict[str, ValidatedEntry],
) -> bool:
    other = others.get(entry.name)
    if other is None:
        return True
    return other.component.component_type != entry.component.component_type


def _added_entry(entry: ValidatedEntry) -> AddedEntry:
    return AddedEntry(
        name=entry.name,
        component_type=entry.component.component_type,
        options=entry.options,
    )


def _removed_entry(entry: ValidatedEntry) -> RemovedEntry:
    return RemovedEntry(
        name=entry.name,
        component_type=entry.component.component_type,
        options=entry.options,
    )


def _changed_entry(entry: ValidatedEntry, names: tuple[str, ...]) -> ChangedEntry:
    forcing = entry.component.replacement_forcing_options
    return ChangedEntry(
        name=entry.name,
        component_type=entry.component.component_type,
        changed_options=names,
        replacement_forcing=any(name in forcing for name in names),
    )


def _changed_option_names(applied: BaseModel, proposed: BaseModel) -> tuple[str, ...]:
    names = tuple(type(proposed).model_fields)
    return tuple(
        name
        for name in names
        if _option_value(applied, name) != _option_value(proposed, name)
    )


def _option_value(options: BaseModel, name: str) -> object:
    if name not in type(options).model_fields:
        return _ABSENT
    return getattr(options, name)
