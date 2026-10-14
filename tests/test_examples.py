import re
from pathlib import Path

from typer.testing import CliRunner

from self_service_platform.cli.main import app

runner = CliRunner()

_ROOT = Path(__file__).resolve().parents[1]
_VALID = _ROOT / "examples" / "valid-manifest.yaml"
_UPDATED = _ROOT / "examples" / "updated-manifest.yaml"
_INVALID = _ROOT / "examples" / "invalid-manifest.yaml"
_README = _ROOT / "README.md"

_ISSUE_LINES = (
    "position 1 uploads: unknown component type no-such-type\n"
    "position 2 jobs: unknown option foo\n"
)

_EXAMPLE_PATH = re.compile(r"examples/[A-Za-z0-9][A-Za-z0-9._-]*\.yaml")


def test_validate_valid_example_exits_0() -> None:
    result = runner.invoke(app, ["validate", str(_VALID)])

    assert result.exit_code == 0
    assert result.stdout == "OK: 2 entries\n"
    assert result.stderr == ""


def test_validate_invalid_example_lists_every_issue() -> None:
    result = runner.invoke(app, ["validate", str(_INVALID)])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == _ISSUE_LINES


def test_estimate_valid_example_is_not_priced() -> None:
    result = runner.invoke(app, ["estimate", str(_VALID)])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert "uploads s3-bucket" in result.stdout
    assert "jobs sqs-queue" in result.stdout
    assert "manifest total: not priced" in result.stdout
    assert "POC heuristic" in result.stdout


def test_estimate_invalid_example_prints_no_cost() -> None:
    result = runner.invoke(app, ["estimate", str(_INVALID)])

    assert result.exit_code == 1
    assert "POC heuristic" not in result.stdout
    assert "total" not in result.stdout
    assert "not priced" not in result.stdout
    assert re.search(r"\d", result.stdout) is None
    assert result.stderr == _ISSUE_LINES


def test_diff_valid_and_updated_example_prints_added_and_removed() -> None:
    result = runner.invoke(app, ["diff", str(_VALID), str(_UPDATED)])

    assert result.exit_code == 0
    assert result.stdout == (
        "added: reports sqs-queue\n"
        "removed: jobs sqs-queue (data in this component will be deleted)\n"
        "summary: 1 added, 1 removed, 0 changed\n"
    )
    assert result.stderr == ""


def test_diff_same_example_prints_no_changes() -> None:
    result = runner.invoke(app, ["diff", str(_VALID), str(_VALID)])

    assert result.exit_code == 0
    assert result.stdout == "no changes\n"
    assert result.stderr == ""


def test_diff_invalid_example_exits_1_with_empty_stdout() -> None:
    result = runner.invoke(app, ["diff", str(_VALID), str(_INVALID)])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"{_INVALID}: position 1 uploads: unknown component type no-such-type\n"
        f"{_INVALID}: position 2 jobs: unknown option foo\n"
    )


def test_readme_example_paths_exist_on_disk() -> None:
    readme = _README.read_text(encoding="utf-8")
    mentioned = set(_EXAMPLE_PATH.findall(readme))

    assert mentioned
    assert "examples/valid-manifest.yaml" in mentioned
    assert "examples/invalid-manifest.yaml" in mentioned
    missing = sorted(path for path in mentioned if not (_ROOT / path).is_file())
    assert missing == []
