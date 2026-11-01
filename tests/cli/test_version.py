import importlib.metadata
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from self_service_platform.app import app

runner = CliRunner()
_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _project_field(key: str) -> str:
    with _PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    value = data["project"][key]
    assert isinstance(value, str)
    return value


def _pyproject_version() -> str:
    return _project_field("version")


@pytest.fixture(autouse=True)
def _redirect_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    ssp_home = tmp_path / "ssp-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("SSP_HOME", str(ssp_home))
    return home, ssp_home


def test_version_prints_the_pyproject_version_and_exits_0() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"{_pyproject_version()}\n"


def test_version_does_not_create_ssp_home_or_dot_ssp(
    _redirect_state_home: tuple[Path, Path],
) -> None:
    home, ssp_home = _redirect_state_home
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"{_pyproject_version()}\n"
    assert not ssp_home.exists()
    assert not (home / ".ssp").exists()


def test_installed_version_matches_pyproject() -> None:
    name = _project_field("name")
    assert importlib.metadata.version(name) == _pyproject_version()
