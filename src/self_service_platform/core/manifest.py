from pathlib import Path
from typing import Annotated, Any, Self

import yaml
from pydantic import BaseModel, Field, RootModel, ValidationError, model_validator

_MAX_ENTRIES = 40


class ManifestLoadError(Exception):
    """Raised when a manifest document cannot be loaded."""


class ManifestEntry(BaseModel):
    type: str
    name: str
    options: dict[str, Any] = Field(default_factory=dict)


class Manifest(
    RootModel[Annotated[list[ManifestEntry], Field(max_length=_MAX_ENTRIES)]]
):
    @model_validator(mode="after")
    def _reject_invalid_names(self) -> Self:
        seen: set[str] = set()
        for index, entry in enumerate(self.root):
            if entry.name.strip(" ") == "":
                raise ValueError(f"position {index + 1} has an empty name")
            if entry.name in seen:
                raise ValueError(f"duplicate name {entry.name}")
            seen.add(entry.name)
        return self


def default_manifest() -> Manifest:
    return Manifest.model_validate(
        [
            {"type": "s3-bucket", "name": "bucket", "options": {}},
            {"type": "lambda-function", "name": "function", "options": {}},
            {"type": "log-group", "name": "logs", "options": {}},
        ]
    )


def load_manifest_from_file(path: Path) -> Manifest:
    return load_manifest_from_string(path.read_text(encoding="utf-8-sig"))


def load_manifest_from_string(text: str) -> Manifest:
    data = _parse_yaml(text)
    if data is None:
        raise ManifestLoadError("document is empty")
    try:
        return Manifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestLoadError(str(exc)) from exc


def _parse_yaml(text: str) -> object:
    try:
        loaded: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestLoadError("malformed YAML") from exc
    return loaded
