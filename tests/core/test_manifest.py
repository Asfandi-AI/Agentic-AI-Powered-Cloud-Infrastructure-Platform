from pathlib import Path
from textwrap import dedent

import pytest

from self_service_platform.core.manifest import (
    ManifestEntry,
    ManifestLoadError,
    load_manifest_from_file,
    load_manifest_from_string,
)

VALID_MANIFEST = dedent(
    """\
    - type: s3-bucket
      name: uploads
      options:
        versioning: true
    - type: sqs-queue
      name: jobs
    """
)

EXPECTED_ENTRIES = [
    ManifestEntry(type="s3-bucket", name="uploads", options={"versioning": True}),
    ManifestEntry(type="sqs-queue", name="jobs", options={}),
]


def _entry_list_yaml(count: int) -> str:
    return "".join(
        f"- type: s3-bucket\n  name: component-{index}\n" for index in range(count)
    )


def test_load_manifest_from_string_reads_a_valid_yaml_list() -> None:
    manifest = load_manifest_from_string(VALID_MANIFEST)

    assert manifest.root == EXPECTED_ENTRIES


def test_load_manifest_from_string_rejects_malformed_yaml() -> None:
    with pytest.raises(ManifestLoadError):
        load_manifest_from_string("- type: [\n")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty-string"),
        pytest.param("# comment only\n", id="comment-only"),
    ],
)
def test_empty_or_comment_only_document_raises_manifest_load_error(text: str) -> None:
    with pytest.raises(ManifestLoadError):
        load_manifest_from_string(text)


def test_load_manifest_from_file_reads_a_valid_yaml_list(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text(VALID_MANIFEST, encoding="utf-8")

    manifest = load_manifest_from_file(path)

    assert manifest.root == EXPECTED_ENTRIES


def test_load_manifest_from_file_reads_utf8_with_a_byte_order_mark(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_bytes(b"\xef\xbb\xbf" + VALID_MANIFEST.encode("utf-8"))

    manifest = load_manifest_from_file(path)

    assert manifest.root == EXPECTED_ENTRIES


def test_load_manifest_from_file_rejects_malformed_yaml(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text("- type: [\n", encoding="utf-8")

    with pytest.raises(ManifestLoadError):
        load_manifest_from_file(path)


def test_load_manifest_from_string_accepts_exactly_40_entries() -> None:
    manifest = load_manifest_from_string(_entry_list_yaml(40))

    assert len(manifest.root) == 40
    assert manifest.root[0].name == "component-0"
    assert manifest.root[39].name == "component-39"


def test_load_manifest_from_string_rejects_41_entries() -> None:
    with pytest.raises(ManifestLoadError):
        load_manifest_from_string(_entry_list_yaml(41))


def test_duplicate_name_error_mentions_the_duplicated_name() -> None:
    text = dedent(
        """\
        - type: s3-bucket
          name: billing-queue
        - type: sqs-queue
          name: billing-queue
        """
    )

    with pytest.raises(ManifestLoadError) as caught:
        load_manifest_from_string(text)

    message = str(caught.value).lower()
    assert "billing-queue" in message
    assert "duplicate" in message


def test_empty_name_error_names_the_one_based_position() -> None:
    text = dedent(
        """\
        - type: s3-bucket
          name: uploads
        - type: sqs-queue
          name: ""
        - type: sns-topic
          name: events
        """
    )

    with pytest.raises(ManifestLoadError, match=r"position 2"):
        load_manifest_from_string(text)


def test_name_made_only_of_spaces_counts_as_empty() -> None:
    text = dedent(
        """\
        - type: s3-bucket
          name: uploads
        - type: sqs-queue
          name: "   "
        """
    )

    with pytest.raises(ManifestLoadError, match=r"position 2"):
        load_manifest_from_string(text)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("type: s3-bucket\nname: uploads\n", id="mapping"),
        pytest.param("not-a-list\n", id="string"),
    ],
)
def test_root_that_is_not_a_list_raises_manifest_load_error(text: str) -> None:
    with pytest.raises(ManifestLoadError):
        load_manifest_from_string(text)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("- type: s3-bucket\n  name: 12\n", id="name-not-a-string"),
        pytest.param(
            "- type: s3-bucket\n  name: uploads\n  options: yes\n",
            id="options-not-a-mapping",
        ),
        pytest.param("- type: s3-bucket\n", id="missing-name"),
        pytest.param("- not-an-entry\n", id="entry-not-a-mapping"),
    ],
)
def test_wrong_field_types_raise_manifest_load_error(text: str) -> None:
    with pytest.raises(ManifestLoadError):
        load_manifest_from_string(text)


def test_python_object_apply_tag_raises_manifest_load_error_and_does_not_execute(
    capfd: pytest.CaptureFixture[str],
) -> None:
    text = '- !!python/object/apply:os.system ["echo hacked"]\n'

    with pytest.raises(ManifestLoadError):
        load_manifest_from_string(text)

    captured = capfd.readouterr()
    assert "hacked" not in captured.out
    assert "hacked" not in captured.err
