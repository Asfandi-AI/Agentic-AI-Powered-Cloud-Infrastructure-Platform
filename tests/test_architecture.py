import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = _ROOT / "src" / "self_service_platform"

_CORE_MODULES = frozenset({"sqlite3", "boto3", "botocore", "aws_cdk"})
_CORE_PREFIXES = (
    "self_service_platform.executors",
    "self_service_platform.registry",
)
_CATALOG_MODULES = frozenset({"boto3", "botocore", "aws_cdk"})
_CATALOG_PREFIXES = ("self_service_platform.executors",)


def _python_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.rglob("*.py") if path.is_file())


def _import_statements(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    statements: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            segment = ast.get_source_segment(source, node)
            if segment is not None:
                statements.append(segment)
    return statements


def _imported_names(statement: str) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(statement)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.level == 0
        ):
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _forbidden(
    names: list[str], modules: frozenset[str], prefixes: tuple[str, ...]
) -> bool:
    for name in names:
        if name.split(".", 1)[0] in modules:
            return True
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes):
            return True
    return False


def _violations(
    directory: Path,
    modules: frozenset[str],
    prefixes: tuple[str, ...],
) -> list[str]:
    found: list[str] = []
    for path in _python_files(directory):
        relative = path.relative_to(_ROOT).as_posix()
        for statement in _import_statements(path):
            if _forbidden(_imported_names(statement), modules, prefixes):
                found.append(f"{relative}: {statement}")
    return found


def test_core_cli_and_catalog_stay_inside_their_boundaries() -> None:
    violations = [
        *_violations(_PACKAGE / "core", _CORE_MODULES, _CORE_PREFIXES),
        *_violations(_PACKAGE / "cli", _CORE_MODULES, _CORE_PREFIXES),
        *_violations(_PACKAGE / "catalog", _CATALOG_MODULES, _CATALOG_PREFIXES),
    ]
    assert violations == []


_COMPOSITION_MODULES = (
    "self_service_platform.wiring",
    "self_service_platform.app",
)
_ADAPTER_PACKAGES = (
    "self_service_platform.registry",
    "self_service_platform.executors",
)


def _is_named_module(name: str, modules: tuple[str, ...]) -> bool:
    return any(name == module or name.startswith(f"{module}.") for module in modules)


def _composition_violations() -> list[str]:
    directories = (_PACKAGE / "core", _PACKAGE / "catalog", _PACKAGE / "cli")
    found: list[str] = []
    for directory in directories:
        for path in _python_files(directory):
            relative = path.relative_to(_ROOT).as_posix()
            for statement in _import_statements(path):
                names = _imported_names(statement)
                if any(_is_named_module(name, _COMPOSITION_MODULES) for name in names):
                    found.append(f"{relative}: {statement}")
    return found


def _adapter_allowed(path: Path) -> bool:
    relative = path.relative_to(_PACKAGE)
    if relative.parts[0] in {"registry", "executors"}:
        return True
    return relative.as_posix() == "wiring.py"


def _adapter_violations(paths: list[Path] | None = None) -> list[str]:
    chosen = _python_files(_PACKAGE) if paths is None else paths
    found: list[str] = []
    for path in chosen:
        if _adapter_allowed(path):
            continue
        relative = path.relative_to(_ROOT).as_posix()
        for statement in _import_statements(path):
            names = _imported_names(statement)
            if any(_is_named_module(name, _ADAPTER_PACKAGES) for name in names):
                found.append(f"{relative}: {statement}")
    return found


def test_core_catalog_and_cli_do_not_import_wiring_or_app() -> None:
    assert _composition_violations() == []


def test_only_wiring_imports_the_registry_and_executors_packages() -> None:
    assert _adapter_violations() == []


def test_cli_import_of_the_registry_package_is_rejected() -> None:
    probe = _PACKAGE / "cli" / "_architecture_probe.py"
    probe.write_text(
        "import self_service_platform.registry.sqlite\n",
        encoding="utf-8",
    )
    try:
        violations = _adapter_violations([probe])
    finally:
        probe.unlink(missing_ok=True)

    assert violations == [
        (
            "src/self_service_platform/cli/_architecture_probe.py: "
            "import self_service_platform.registry.sqlite"
        )
    ]


def test_core_registry_import_is_allowed_and_the_package_is_not() -> None:
    allowed = _PACKAGE / "cli" / "_architecture_core_registry.py"
    forbidden = _PACKAGE / "cli" / "_architecture_registry_package.py"
    allowed.write_text(
        "import self_service_platform.core.registry\n",
        encoding="utf-8",
    )
    forbidden.write_text(
        "import self_service_platform.registry\n",
        encoding="utf-8",
    )
    try:
        violations = _adapter_violations([allowed, forbidden])
    finally:
        allowed.unlink(missing_ok=True)
        forbidden.unlink(missing_ok=True)

    assert violations == [
        (
            "src/self_service_platform/cli/_architecture_registry_package.py: "
            "import self_service_platform.registry"
        )
    ]


def test_cli_import_of_wiring_is_rejected() -> None:
    probe = _PACKAGE / "cli" / "_architecture_wiring.py"
    probe.write_text("import self_service_platform.wiring\n", encoding="utf-8")
    try:
        violations = _composition_violations()
    finally:
        probe.unlink(missing_ok=True)

    assert violations == [
        (
            "src/self_service_platform/cli/_architecture_wiring.py: "
            "import self_service_platform.wiring"
        )
    ]


def test_cli_import_of_sqlite3_is_rejected() -> None:
    probe = _PACKAGE / "cli" / "_architecture_sqlite.py"
    probe.write_text("import sqlite3\n", encoding="utf-8")
    try:
        violations = _violations(_PACKAGE / "cli", _CORE_MODULES, _CORE_PREFIXES)
    finally:
        probe.unlink(missing_ok=True)

    assert violations == [
        "src/self_service_platform/cli/_architecture_sqlite.py: import sqlite3"
    ]


_SWEEPER_MODULES = frozenset({"sqlite3"})
_SWEEPER_PREFIXES = (
    "self_service_platform.executors",
    "self_service_platform.registry",
)


def _sweeper_import_violations(path: Path) -> list[str]:
    relative = path.relative_to(_ROOT).as_posix()
    found: list[str] = []
    for statement in _import_statements(path):
        names = _imported_names(statement)
        if _forbidden(names, _SWEEPER_MODULES, _SWEEPER_PREFIXES):
            found.append(f"{relative}: {statement}")
    return found


def test_sweeper_does_not_import_executors_registry_or_sqlite() -> None:
    assert _sweeper_import_violations(_PACKAGE / "core" / "sweeper.py") == []


def _adapter_composition_violations(paths: list[Path] | None = None) -> list[str]:
    if paths is None:
        chosen = [
            *_python_files(_PACKAGE / "registry"),
            *_python_files(_PACKAGE / "executors"),
        ]
    else:
        chosen = paths
    found: list[str] = []
    for path in chosen:
        relative = path.relative_to(_ROOT).as_posix()
        for statement in _import_statements(path):
            names = _imported_names(statement)
            if any(_is_named_module(name, _COMPOSITION_MODULES) for name in names):
                found.append(f"{relative}: {statement}")
    return found


def test_registry_and_executors_do_not_import_wiring_or_app() -> None:
    assert _adapter_composition_violations() == []


def test_executor_import_of_wiring_or_app_is_rejected() -> None:
    probe = _PACKAGE / "executors" / "_architecture_wiring.py"
    probe.write_text(
        "import self_service_platform.wiring\nimport self_service_platform.app\n",
        encoding="utf-8",
    )
    try:
        violations = _adapter_composition_violations([probe])
    finally:
        probe.unlink(missing_ok=True)

    assert violations == [
        (
            "src/self_service_platform/executors/_architecture_wiring.py: "
            "import self_service_platform.wiring"
        ),
        (
            "src/self_service_platform/executors/_architecture_wiring.py: "
            "import self_service_platform.app"
        ),
    ]


def _statedir_name_allowed(name: str) -> bool:
    if name == "self_service_platform.core" or name.startswith(
        "self_service_platform.core."
    ):
        return True
    return name.split(".", 1)[0] in sys.stdlib_module_names


def _statedir_import_violations(path: Path) -> list[str]:
    relative = path.relative_to(_ROOT).as_posix()
    found: list[str] = []
    for statement in _import_statements(path):
        names = _imported_names(statement)
        if any(not _statedir_name_allowed(name) for name in names):
            found.append(f"{relative}: {statement}")
    return found


def test_statedir_imports_only_the_standard_library_and_core() -> None:
    assert _statedir_import_violations(_PACKAGE / "statedir.py") == []


def test_statedir_import_outside_stdlib_and_core_is_rejected() -> None:
    probe = _PACKAGE / "_architecture_statedir.py"
    probe.write_text(
        "import self_service_platform.wiring\nimport self_service_platform.registry\n",
        encoding="utf-8",
    )
    try:
        violations = _statedir_import_violations(probe)
    finally:
        probe.unlink(missing_ok=True)

    assert violations == [
        (
            "src/self_service_platform/_architecture_statedir.py: "
            "import self_service_platform.wiring"
        ),
        (
            "src/self_service_platform/_architecture_statedir.py: "
            "import self_service_platform.registry"
        ),
    ]


def test_sweeper_import_of_executors_registry_or_sqlite_is_rejected() -> None:
    probe = _PACKAGE / "core" / "_architecture_sweeper.py"
    probe.write_text(
        "import sqlite3\n"
        "import self_service_platform.executors\n"
        "import self_service_platform.registry\n",
        encoding="utf-8",
    )
    try:
        violations = _sweeper_import_violations(probe)
    finally:
        probe.unlink(missing_ok=True)

    assert violations == [
        "src/self_service_platform/core/_architecture_sweeper.py: import sqlite3",
        (
            "src/self_service_platform/core/_architecture_sweeper.py: "
            "import self_service_platform.executors"
        ),
        (
            "src/self_service_platform/core/_architecture_sweeper.py: "
            "import self_service_platform.registry"
        ),
    ]


_ISSUER_MODULE = "self_service_platform.core.confirmation_issuer"


def _confirmation_issuer_violations(path: Path) -> list[str]:
    relative = path.relative_to(_ROOT).as_posix()
    found: list[str] = []
    for statement in _import_statements(path):
        names = _imported_names(statement)
        if any(
            name == _ISSUER_MODULE or name.startswith(f"{_ISSUER_MODULE}.")
            for name in names
        ):
            found.append(f"{relative}: {statement}")
    return found


def test_confirmation_does_not_import_the_issuer() -> None:
    path = _PACKAGE / "core" / "confirmation.py"
    assert _confirmation_issuer_violations(path) == []


def test_confirmation_import_of_the_issuer_is_rejected() -> None:
    probe = _PACKAGE / "core" / "_architecture_confirmation.py"
    probe.write_text(
        "import self_service_platform.core.confirmation_issuer\n",
        encoding="utf-8",
    )
    try:
        violations = _confirmation_issuer_violations(probe)
    finally:
        probe.unlink(missing_ok=True)

    assert violations == [
        (
            "src/self_service_platform/core/_architecture_confirmation.py: "
            "import self_service_platform.core.confirmation_issuer"
        )
    ]


_AGENT_MODULES = frozenset({"boto3", "botocore", "aws_cdk"})
_AGENT_PREFIXES = (
    "self_service_platform.executors",
    "self_service_platform.registry",
)
_AGENT_PACKAGE = ("self_service_platform.agent",)


def _agent_import_violations(paths: list[Path]) -> list[str]:
    found: list[str] = []
    for path in paths:
        relative = path.relative_to(_ROOT).as_posix()
        for statement in _import_statements(path):
            names = _imported_names(statement)
            if any(_is_named_module(name, _AGENT_PACKAGE) for name in names):
                found.append(f"{relative}: {statement}")
    return found


def test_agent_imports_stay_inside_its_boundary() -> None:
    assert _violations(_PACKAGE / "agent", _AGENT_MODULES, _AGENT_PREFIXES) == []


def test_an_agent_import_of_a_cloud_sdk_or_adapter_is_rejected() -> None:
    directory = _PACKAGE / "agent"
    created = not directory.exists()
    directory.mkdir(exist_ok=True)
    probe = directory / "_architecture_probe.py"
    probe.write_text(
        "import boto3\n"
        "import self_service_platform.executors\n"
        "import self_service_platform.registry\n",
        encoding="utf-8",
    )
    try:
        violations = _violations(directory, _AGENT_MODULES, _AGENT_PREFIXES)
    finally:
        probe.unlink(missing_ok=True)
        if created:
            directory.rmdir()

    assert violations == [
        "src/self_service_platform/agent/_architecture_probe.py: import boto3",
        (
            "src/self_service_platform/agent/_architecture_probe.py: "
            "import self_service_platform.executors"
        ),
        (
            "src/self_service_platform/agent/_architecture_probe.py: "
            "import self_service_platform.registry"
        ),
    ]


def test_core_and_cli_do_not_import_the_agent_package() -> None:
    paths = [
        *_python_files(_PACKAGE / "core"),
        *_python_files(_PACKAGE / "cli"),
    ]
    assert _agent_import_violations(paths) == []


def test_a_core_import_of_the_agent_package_is_rejected() -> None:
    probe = _PACKAGE / "core" / "_architecture_agent.py"
    probe.write_text("import self_service_platform.agent\n", encoding="utf-8")
    try:
        violations = _agent_import_violations([probe])
    finally:
        probe.unlink(missing_ok=True)

    assert violations == [
        (
            "src/self_service_platform/core/_architecture_agent.py: "
            "import self_service_platform.agent"
        )
    ]


_AUDIT_PACKAGE = ("self_service_platform.audit",)


def _audit_import_allowed(path: Path) -> bool:
    relative = path.relative_to(_PACKAGE)
    if relative.as_posix() == "wiring.py":
        return True
    return relative.parts[0] == "audit"


def _audit_import_violations(paths: list[Path] | None = None) -> list[str]:
    chosen = _python_files(_PACKAGE) if paths is None else paths
    found: list[str] = []
    for path in chosen:
        if _audit_import_allowed(path):
            continue
        relative = path.relative_to(_ROOT).as_posix()
        for statement in _import_statements(path):
            names = _imported_names(statement)
            if any(_is_named_module(name, _AUDIT_PACKAGE) for name in names):
                found.append(f"{relative}: {statement}")
    return found


def test_only_wiring_imports_the_audit_package() -> None:
    assert _audit_import_violations() == []


def test_a_core_import_of_the_audit_package_is_rejected() -> None:
    probe = _PACKAGE / "core" / "_architecture_audit.py"
    probe.write_text("import self_service_platform.audit\n", encoding="utf-8")
    try:
        violations = _audit_import_violations([probe])
    finally:
        probe.unlink(missing_ok=True)

    assert violations == [
        (
            "src/self_service_platform/core/_architecture_audit.py: "
            "import self_service_platform.audit"
        )
    ]


def test_a_cli_import_of_the_audit_package_is_rejected() -> None:
    probe = _PACKAGE / "cli" / "_architecture_audit.py"
    probe.write_text(
        "import self_service_platform.audit.cloudwatch\n",
        encoding="utf-8",
    )
    try:
        violations = _audit_import_violations([probe])
    finally:
        probe.unlink(missing_ok=True)

    assert violations == [
        (
            "src/self_service_platform/cli/_architecture_audit.py: "
            "import self_service_platform.audit.cloudwatch"
        )
    ]
