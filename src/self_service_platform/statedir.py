import os
from pathlib import Path

from self_service_platform.core.tenant import RegistryUnavailableError


def create_state_directory(
    directory: Path, *, posix: bool = os.name == "posix"
) -> bool:
    try:
        directory.mkdir(parents=True, mode=0o700)
    except FileExistsError as exc:
        if directory.is_dir():
            return False
        raise RegistryUnavailableError(directory) from exc
    except OSError as exc:
        raise RegistryUnavailableError(directory) from exc
    if posix:
        try:
            directory.chmod(0o700)
        except OSError as exc:
            raise RegistryUnavailableError(directory) from exc
    return True
