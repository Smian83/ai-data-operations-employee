"""Module 13 unit tests for app.artifacts.storage: LocalFilesystemArtifactStorage's
exists/open/delete contract in isolation. No FastAPI, no database, no
tenant/path-resolution logic involved -- this module operates purely on
already-resolved paths handed to it (mirrors tests/test_artifact_download_helpers.py's
discipline for app.artifacts.download). Every test constructs its own
LocalFilesystemArtifactStorage() directly rather than going through
get_artifact_storage(), so the lru_cache singleton is never a factor in
these tests."""
import os
from pathlib import Path

import pytest

from app.artifacts.storage import ArtifactStorage, LocalFilesystemArtifactStorage, get_artifact_storage


@pytest.fixture
def storage() -> ArtifactStorage:
    return LocalFilesystemArtifactStorage()


# --- exists --------------------------------------------------------------


def test_exists_returns_true_for_existing_file(storage: ArtifactStorage, tmp_path: Path) -> None:
    target = tmp_path / "artifact.csv"
    target.write_bytes(b"a,b\n1,2\n")

    assert storage.exists(target) is True


def test_exists_returns_false_for_missing_file(storage: ArtifactStorage, tmp_path: Path) -> None:
    missing = tmp_path / "never_written.csv"

    assert storage.exists(missing) is False


def test_exists_returns_false_for_a_directory(storage: ArtifactStorage, tmp_path: Path) -> None:
    a_dir = tmp_path / "not_a_file"
    a_dir.mkdir()

    assert storage.exists(a_dir) is False


# --- open ------------------------------------------------------------------


def test_open_reads_an_existing_file(storage: ArtifactStorage, tmp_path: Path) -> None:
    target = tmp_path / "artifact.csv"
    target.write_bytes(b"a,b\n1,2\n")

    with storage.open(target) as fileobj:
        assert fileobj.read() == b"a,b\n1,2\n"


def test_open_raises_file_not_found_for_a_missing_file(
    storage: ArtifactStorage, tmp_path: Path
) -> None:
    missing = tmp_path / "never_written.csv"

    with pytest.raises(FileNotFoundError):
        storage.open(missing)


def test_open_raises_is_a_directory_for_a_directory(
    storage: ArtifactStorage, tmp_path: Path
) -> None:
    """Directories are never treated as valid artifact files -- open()
    raises rather than returning a readable-but-nonsensical handle."""
    a_dir = tmp_path / "not_a_file"
    a_dir.mkdir()

    with pytest.raises(IsADirectoryError):
        storage.open(a_dir)


def test_open_raises_permission_error_and_does_not_swallow_it(
    storage: ArtifactStorage, tmp_path: Path
) -> None:
    target = tmp_path / "locked.csv"
    target.write_bytes(b"secret")
    os.chmod(target, 0o000)
    try:
        with pytest.raises(PermissionError):
            storage.open(target)
    finally:
        os.chmod(target, 0o644)  # restore so tmp_path cleanup can remove it


# --- delete ------------------------------------------------------------------


def test_delete_removes_an_existing_file_and_returns_true(
    storage: ArtifactStorage, tmp_path: Path
) -> None:
    target = tmp_path / "artifact.csv"
    target.write_bytes(b"a,b\n1,2\n")

    result = storage.delete(target)

    assert result is True
    assert not target.exists()


def test_delete_against_an_already_missing_file_returns_false(
    storage: ArtifactStorage, tmp_path: Path
) -> None:
    missing = tmp_path / "never_written.csv"

    result = storage.delete(missing)

    assert result is False


def test_delete_is_idempotent_across_repeated_calls(
    storage: ArtifactStorage, tmp_path: Path
) -> None:
    """Calling delete() twice in a row on the same path is safe: the
    first call removes the file and returns True, the second finds
    nothing left to remove and returns False -- never an exception on
    the second call."""
    target = tmp_path / "artifact.csv"
    target.write_bytes(b"a,b\n1,2\n")

    first = storage.delete(target)
    second = storage.delete(target)

    assert first is True
    assert second is False


def test_delete_raises_is_a_directory_and_does_not_treat_a_directory_as_missing(
    storage: ArtifactStorage, tmp_path: Path
) -> None:
    """A directory is never a valid artifact target -- delete() must not
    silently no-op or report it as 'already missing'."""
    a_dir = tmp_path / "not_a_file"
    a_dir.mkdir()

    with pytest.raises(IsADirectoryError):
        storage.delete(a_dir)

    assert a_dir.exists()  # never removed


def test_delete_does_not_silently_swallow_a_genuine_permission_error(
    storage: ArtifactStorage, tmp_path: Path
) -> None:
    """Deletion permission is governed by the containing directory's
    write bit on POSIX, not the target file's own mode -- lock the
    parent directory to force a genuine, non-'missing' filesystem
    failure and confirm it propagates rather than being reported as a
    successful or already-missing delete."""
    locked_dir = tmp_path / "locked_dir"
    locked_dir.mkdir()
    target = locked_dir / "artifact.csv"
    target.write_bytes(b"a,b\n1,2\n")
    os.chmod(locked_dir, 0o555)  # read + execute, no write
    try:
        with pytest.raises(PermissionError):
            storage.delete(target)
    finally:
        os.chmod(locked_dir, 0o755)  # restore so tmp_path cleanup can remove it

    assert target.exists()  # the file was never actually removed


# --- get_artifact_storage() -------------------------------------------------


def test_get_artifact_storage_returns_a_local_filesystem_implementation() -> None:
    result = get_artifact_storage()
    assert isinstance(result, LocalFilesystemArtifactStorage)


def test_get_artifact_storage_is_a_stable_singleton() -> None:
    """lru_cache-backed -- repeated calls return the identical instance,
    mirroring app.core.config.get_settings()'s own convention."""
    assert get_artifact_storage() is get_artifact_storage()
