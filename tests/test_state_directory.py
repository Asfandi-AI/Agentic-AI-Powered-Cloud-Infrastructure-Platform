import os
from pathlib import Path

import pytest

from self_service_platform.core.tenant import RegistryUnavailableError
from self_service_platform.statedir import create_state_directory


def _record_chmod(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, int]]:
    recorded: list[tuple[Path, int]] = []

    def chmod(self: Path, mode: int) -> None:
        recorded.append((self, mode))

    monkeypatch.setattr(Path, "chmod", chmod)
    return recorded


def test_create_state_directory_that_loses_the_race_does_not_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "state"
    real_mkdir = Path.mkdir

    def mkdir(
        self: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if self == directory and not self.exists():
            real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)
        real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    recorded = _record_chmod(monkeypatch)

    created = create_state_directory(directory, posix=True)

    assert created is False
    assert directory.is_dir()
    assert recorded == []


def test_create_state_directory_that_creates_it_chmods_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "state"
    recorded = _record_chmod(monkeypatch)

    created = create_state_directory(directory, posix=True)

    assert created is True
    assert directory.is_dir()
    assert recorded == [(directory, 0o700)]


def test_create_state_directory_rejects_a_regular_file(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    directory.write_bytes(b"file")

    with pytest.raises(RegistryUnavailableError) as raised:
        create_state_directory(directory, posix=True)

    assert raised.value.path == directory
    assert directory.read_bytes() == b"file"
    assert not directory.is_dir()


@pytest.mark.skipif(
    os.name == "nt",
    reason="a read-only directory does not block creation on Windows",
)
def test_create_state_directory_rejects_a_read_only_parent(tmp_path: Path) -> None:
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)
    directory = parent / "state"
    try:
        with pytest.raises(RegistryUnavailableError) as raised:
            create_state_directory(directory, posix=True)
    finally:
        parent.chmod(0o700)

    assert raised.value.path == directory
    assert not directory.exists()
