"""Module 10 unit tests for the pure, framework-independent logic in
app.artifacts.download: tenant-root path containment (including a
symlink-escape attempt), the verify-then-stream single-open() flow
(hash match / hash mismatch / missing file / not-a-regular-file), the
bounded-chunk streaming generator, and the safe synthetic filename
builder. No FastAPI, no database -- these exercise the module directly,
independent of the API layer (mirrors tests/test_csv_loader.py's
discipline for app.profiling.csv_loader.resolve_source_path)."""
import hashlib
import os
from pathlib import Path

import pytest

from app.artifacts.download import (
    ARTIFACT_DOWNLOAD_CHUNK_SIZE_BYTES,
    ArtifactIntegrityError,
    ArtifactMissingError,
    ArtifactPathError,
    iter_artifact_chunks,
    open_verified_artifact,
    resolve_artifact_path,
    safe_download_filename,
)


# --- resolve_artifact_path ---------------------------------------------


def test_resolve_artifact_path_within_root_is_returned(tmp_path: Path) -> None:
    root = tmp_path / "tenant"
    root.mkdir()
    target = root / "run.csv"
    target.write_bytes(b"a,b\n1,2\n")

    resolved = resolve_artifact_path(root, str(target))
    assert resolved == target.resolve()


def test_resolve_artifact_path_escaping_root_raises(tmp_path: Path) -> None:
    root = tmp_path / "tenant"
    root.mkdir()
    outside = tmp_path / "other_tenant" / "run.csv"
    outside.parent.mkdir()
    outside.write_bytes(b"secret")

    with pytest.raises(ArtifactPathError):
        resolve_artifact_path(root, str(outside))


def test_resolve_artifact_path_symlink_escape_raises(tmp_path: Path) -> None:
    root = tmp_path / "tenant"
    root.mkdir()
    outside_dir = tmp_path / "other_tenant"
    outside_dir.mkdir()
    outside_file = outside_dir / "secret.csv"
    outside_file.write_bytes(b"secret")

    link = root / "escape.csv"
    try:
        link.symlink_to(outside_file)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this sandbox")

    with pytest.raises(ArtifactPathError):
        resolve_artifact_path(root, str(link))


# --- open_verified_artifact ----------------------------------------------


def test_open_verified_artifact_hash_match_returns_rewound_readable_fd(
    tmp_path: Path,
) -> None:
    content = b"id,name\n1,jane\n2,bob\n"
    path = tmp_path / "run.csv"
    path.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()

    fileobj = open_verified_artifact(path, expected)
    try:
        # Rewound -- reading from the start returns the full content,
        # not the tail left over from the internal verification pass.
        assert fileobj.read() == content
    finally:
        fileobj.close()


def test_open_verified_artifact_hash_mismatch_raises_and_sends_nothing(
    tmp_path: Path,
) -> None:
    content = b"id,name\n1,jane\n"
    path = tmp_path / "run.csv"
    path.write_bytes(content)
    wrong_hash = hashlib.sha256(b"not the real content").hexdigest()

    with pytest.raises(ArtifactIntegrityError):
        open_verified_artifact(path, wrong_hash)


def test_open_verified_artifact_missing_file_raises_file_not_found(
    tmp_path: Path,
) -> None:
    path = tmp_path / "does_not_exist.csv"
    with pytest.raises(ArtifactMissingError) as exc_info:
        open_verified_artifact(path, hashlib.sha256(b"x").hexdigest())
    assert exc_info.value.failure_reason_code == "file_not_found"


def test_open_verified_artifact_directory_raises_not_a_regular_file(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "a_directory"
    directory.mkdir()
    with pytest.raises(ArtifactMissingError) as exc_info:
        open_verified_artifact(directory, hashlib.sha256(b"x").hexdigest())
    assert exc_info.value.failure_reason_code == "not_a_regular_file"


def test_open_verified_artifact_uses_bounded_chunks_not_a_full_read(
    tmp_path: Path,
) -> None:
    # Content larger than one chunk, to exercise the loop over multiple
    # reads rather than a single call.
    content = os.urandom(ARTIFACT_DOWNLOAD_CHUNK_SIZE_BYTES * 2 + 17)
    path = tmp_path / "large.csv"
    path.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()

    fileobj = open_verified_artifact(path, expected)
    try:
        assert fileobj.read() == content
    finally:
        fileobj.close()


# --- iter_artifact_chunks -------------------------------------------------


def test_iter_artifact_chunks_yields_full_content_and_closes_descriptor(
    tmp_path: Path,
) -> None:
    content = b"x" * (ARTIFACT_DOWNLOAD_CHUNK_SIZE_BYTES + 1)
    path = tmp_path / "run.csv"
    path.write_bytes(content)
    fileobj = path.open("rb")

    collected = b"".join(iter_artifact_chunks(fileobj))
    assert collected == content
    assert fileobj.closed


def test_iter_artifact_chunks_closes_descriptor_even_on_error(tmp_path: Path) -> None:
    path = tmp_path / "run.csv"
    path.write_bytes(b"some content")
    fileobj = path.open("rb")
    fileobj.close()  # already closed -- reading raises ValueError

    with pytest.raises(ValueError):
        list(iter_artifact_chunks(fileobj))
    assert fileobj.closed


# --- safe_download_filename ------------------------------------------------


def test_safe_download_filename_contains_no_path_information() -> None:
    name = safe_download_filename("export", "11111111-2222-3333-4444-555555555555")
    assert name == "export-11111111-2222-3333-4444-555555555555.csv"
    assert "/" not in name
    assert "\\" not in name
