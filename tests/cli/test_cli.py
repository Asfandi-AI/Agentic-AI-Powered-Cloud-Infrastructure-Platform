import re
import tomllib
from decimal import Decimal
from pathlib import Path
from textwrap import dedent

from pydantic import BaseModel, ConfigDict
from typer.testing import CliRunner, Result

from self_service_platform.catalog.catalog import Catalog
from self_service_platform.catalog.component import (
    REQUIRED_TAG_KEYS,
    AwsService,
    Component,
    CostLineItem,
    DeletionBehaviour,
)
from self_service_platform.cli.main import create_app

runner = CliRunner()


class _Options(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _component(
    component_type: str,
    description: str,
    aws_service: AwsService,
    line_description: str,
    amount: Decimal | None,
) -> Component:
    return Component(
        component_type=component_type,
        description=description,
        aws_service=aws_service,
        options_model=_Options,
        cost_line_items=(CostLineItem(description=line_description, amount=amount),),
        required_tags=REQUIRED_TAG_KEYS,
        deletion_behaviour=DeletionBehaviour.DELETE,
        permissions=(f"{aws_service.value}:TestAction",),
    )


def _priced_catalog() -> Catalog:
    catalog = Catalog()
    catalog.register(
        _component(
            "sqs-queue",
            "An SQS queue",
            AwsService.SQS,
            "requests",
            Decimal("2.25"),
        )
    )
    catalog.register(
        _component(
            "s3-bucket",
            "An S3 bucket",
            AwsService.S3,
            "storage",
            Decimal("1.50"),
        )
    )
    return catalog


def _unpriced_catalog() -> Catalog:
    catalog = Catalog()
    catalog.register(
        _component(
            "s3-bucket",
            "An S3 bucket",
            AwsService.S3,
            "storage",
            Decimal("1.50"),
        )
    )
    catalog.register(
        _component(
            "sns-topic",
            "An SNS topic",
            AwsService.SNS,
            "publishes",
            None,
        )
    )
    return catalog


_MANIFEST = dedent(
    """\
    - type: sqs-queue
      name: zebra
    - type: s3-bucket
      name: alpha
    """
)

_INVALID_MANIFEST = dedent(
    """\
    - type: no-such-type
      name: uploads
    - type: s3-bucket
      name: jobs
      options:
        foo: 1
    """
)

_UNPRICED_MANIFEST = dedent(
    """\
    - type: s3-bucket
      name: uploads
    - type: sns-topic
      name: events
    """
)

_ISSUE_LINES = (
    "position 1 uploads: unknown component type no-such-type\n"
    "position 2 jobs: unknown option foo\n"
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _assert_cannot_read(result: Result, path: Path) -> None:
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == f"cannot read manifest: {path}\n"
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_catalog_lists_components_sorted_with_version() -> None:
    catalog = _priced_catalog()
    app = create_app(catalog)

    result = runner.invoke(app, ["catalog"])

    assert result.exit_code == 0
    assert result.stdout == (
        "s3-bucket s3 An S3 bucket\n"
        "sqs-queue sqs An SQS queue\n"
        f"catalog_version: {catalog.catalog_version}\n"
    )
    assert result.stderr == ""


def test_validate_ok_prints_entry_count(tmp_path: Path) -> None:
    app = create_app(_priced_catalog())
    path = _write(tmp_path, _MANIFEST)

    result = runner.invoke(app, ["validate", str(path)])

    assert result.exit_code == 0
    assert result.stdout == "OK: 2 entries\n"
    assert result.stderr == ""


def test_validate_reports_every_issue_with_position_and_name(tmp_path: Path) -> None:
    app = create_app(_priced_catalog())
    path = _write(tmp_path, _INVALID_MANIFEST)

    result = runner.invoke(app, ["validate", str(path)])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == _ISSUE_LINES


def test_validate_bad_yaml_exits_1_without_traceback(tmp_path: Path) -> None:
    app = create_app(_priced_catalog())
    path = _write(tmp_path, "- type: [\n")

    result = runner.invoke(app, ["validate", str(path)])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "malformed YAML\n"
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_validate_missing_file_exits_2_without_traceback(tmp_path: Path) -> None:
    app = create_app(_priced_catalog())
    path = tmp_path / "missing.yaml"

    result = runner.invoke(app, ["validate", str(path)])

    _assert_cannot_read(result, path)


def test_validate_directory_exits_2_without_traceback(tmp_path: Path) -> None:
    app = create_app(_priced_catalog())
    path = tmp_path / "not-a-file"
    path.mkdir()

    result = runner.invoke(app, ["validate", str(path)])

    _assert_cannot_read(result, path)


def test_estimate_directory_exits_2_without_traceback(tmp_path: Path) -> None:
    app = create_app(_priced_catalog())
    path = tmp_path / "not-a-file"
    path.mkdir()

    result = runner.invoke(app, ["estimate", str(path)])

    _assert_cannot_read(result, path)


def test_validate_non_utf8_exits_2_without_traceback(tmp_path: Path) -> None:
    app = create_app(_priced_catalog())
    path = tmp_path / "manifest.yaml"
    path.write_bytes(b"\xff\xfe\xfa")

    result = runner.invoke(app, ["validate", str(path)])

    _assert_cannot_read(result, path)


def test_estimate_non_utf8_exits_2_without_traceback(tmp_path: Path) -> None:
    app = create_app(_priced_catalog())
    path = tmp_path / "manifest.yaml"
    path.write_bytes(b"\xff\xfe\xfa")

    result = runner.invoke(app, ["estimate", str(path)])

    _assert_cannot_read(result, path)


def test_validate_reads_utf8_with_a_byte_order_mark(tmp_path: Path) -> None:
    app = create_app(_priced_catalog())
    path = tmp_path / "manifest.yaml"
    path.write_bytes(b"\xef\xbb\xbf" + _MANIFEST.encode("utf-8"))

    result = runner.invoke(app, ["validate", str(path)])

    assert result.exit_code == 0
    assert result.stdout == "OK: 2 entries\n"
    assert result.stderr == ""


def test_estimate_prints_breakdown_in_manifest_order(tmp_path: Path) -> None:
    app = create_app(_priced_catalog())
    path = _write(tmp_path, _MANIFEST)

    result = runner.invoke(app, ["estimate", str(path)])

    assert result.exit_code == 0
    assert result.stdout == (
        "zebra sqs-queue\n"
        "  requests 2.25\n"
        "  total: 2.25\n"
        "alpha s3-bucket\n"
        "  storage 1.50\n"
        "  total: 1.50\n"
        "manifest total: 3.75\n"
        "POC heuristic\n"
    )
    assert result.stderr == ""


def test_estimate_unpriced_component_prints_not_priced(tmp_path: Path) -> None:
    app = create_app(_unpriced_catalog())
    path = _write(tmp_path, _UNPRICED_MANIFEST)

    result = runner.invoke(app, ["estimate", str(path)])

    assert result.exit_code == 0
    assert result.stdout == (
        "uploads s3-bucket\n"
        "  storage 1.50\n"
        "  total: 1.50\n"
        "events sns-topic\n"
        "  publishes not priced\n"
        "  total: not priced\n"
        "manifest total: not priced\n"
        "POC heuristic\n"
    )
    assert " 0" not in result.stdout
    assert result.stderr == ""


def test_estimate_invalid_manifest_prints_no_cost(tmp_path: Path) -> None:
    app = create_app(_priced_catalog())
    path = _write(tmp_path, _INVALID_MANIFEST)

    result = runner.invoke(app, ["estimate", str(path)])

    assert result.exit_code == 1
    assert "POC heuristic" not in result.stdout
    assert "total" not in result.stdout
    assert "not priced" not in result.stdout
    assert re.search(r"\d", result.stdout) is None
    assert result.stderr == _ISSUE_LINES


def test_estimate_zero_amount_is_priced_as_zero(tmp_path: Path) -> None:
    catalog = Catalog()
    catalog.register(
        _component(
            "s3-bucket",
            "An S3 bucket",
            AwsService.S3,
            "storage",
            Decimal(0),
        )
    )
    catalog.register(
        _component(
            "sqs-queue",
            "An SQS queue",
            AwsService.SQS,
            "requests",
            Decimal("1.50"),
        )
    )
    app = create_app(catalog)
    path = _write(
        tmp_path,
        dedent(
            """\
            - type: s3-bucket
              name: uploads
            - type: sqs-queue
              name: jobs
            """
        ),
    )

    result = runner.invoke(app, ["estimate", str(path)])

    assert result.exit_code == 0
    assert result.stdout == (
        "uploads s3-bucket\n"
        "  storage 0\n"
        "  total: 0\n"
        "jobs sqs-queue\n"
        "  requests 1.50\n"
        "  total: 1.50\n"
        "manifest total: 1.50\n"
        "POC heuristic\n"
    )
    assert "not priced" not in result.stdout
    assert result.stderr == ""


def test_estimate_component_with_no_line_items_is_not_priced(tmp_path: Path) -> None:
    catalog = Catalog()
    catalog.register(
        Component(
            component_type="s3-bucket",
            description="An S3 bucket",
            aws_service=AwsService.S3,
            options_model=_Options,
            cost_line_items=(),
            required_tags=REQUIRED_TAG_KEYS,
            deletion_behaviour=DeletionBehaviour.DELETE,
            permissions=("s3:TestAction",),
        )
    )
    app = create_app(catalog)
    path = _write(
        tmp_path,
        dedent(
            """\
            - type: s3-bucket
              name: uploads
            """
        ),
    )

    result = runner.invoke(app, ["estimate", str(path)])

    assert result.exit_code == 0
    assert result.stdout == (
        "uploads s3-bucket\n"
        "  total: not priced\n"
        "manifest total: not priced\n"
        "POC heuristic\n"
    )
    assert result.stderr == ""


def test_estimate_bad_yaml_exits_1_without_traceback(tmp_path: Path) -> None:
    app = create_app(_priced_catalog())
    path = _write(tmp_path, "- type: [\n")

    result = runner.invoke(app, ["estimate", str(path)])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "malformed YAML\n"
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_usage_errors_exit_2_and_are_pure_ascii() -> None:
    app = create_app(_priced_catalog())

    for args in (["estimate"], ["no-such-command"]):
        result = runner.invoke(app, args)

        assert result.exit_code == 2
        assert result.stdout.isascii()
        assert result.stderr.isascii()
        assert result.output.isascii()


def test_pyproject_declares_the_ssp_console_script() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)

    scripts = data["project"]["scripts"]
    assert isinstance(scripts, dict)
    assert scripts["ssp"] == "self_service_platform.cli.main:app"


def test_help_lists_commands() -> None:
    app = create_app(_priced_catalog())

    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "catalog" in result.stdout
    assert "validate" in result.stdout
    assert "estimate" in result.stdout
    assert result.stderr == ""


def test_command_output_is_pure_ascii(tmp_path: Path) -> None:
    app = create_app(_priced_catalog())
    path = _write(tmp_path, _MANIFEST)
    invocations = (
        ["--help"],
        ["catalog"],
        ["validate", str(path)],
        ["estimate", str(path)],
    )

    for args in invocations:
        result = runner.invoke(app, args)

        assert result.stdout.isascii()
        assert result.stderr.isascii()
        assert result.output.isascii()
