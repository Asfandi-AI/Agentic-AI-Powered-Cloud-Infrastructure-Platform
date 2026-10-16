import ast
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
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _forbidden(names: list[str], modules: frozenset[str], prefixes: tuple[str, ...]) -> bool:
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
