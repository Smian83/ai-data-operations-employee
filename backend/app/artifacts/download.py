"""
Module 10: pure, framework-independent artifact-resolution and
verify-then-stream logic shared by the three download endpoints
(cleaning/standardization/export). See
docs/module-10-artifact-retrieval-design.md Sections 6, 13, 14.

NO ARTIFACT BYTES ARE SENT BEFORE INTEGRITY VERIFICATION SUCCEEDS. Every
artifact this module serves is opened exactly once: open_verified_
artifact() reads the complete file in bounded chunks to compute its
SHA-256 before returning anything, then -- on a match -- rewinds that
SAME file descriptor (never a second open() call, never a second path
resolution) so the caller can stream from it. This closes the
time-of-check/time-of-use gap between verification and transmission by
construction: there is no window in which a different file could be
substituted at the same path between the two passes, because both
passes read through the one descriptor opened at the start (Section 13's
TOCTOU analysis). On a hash mismatch, the file is closed and
ArtifactIntegrityError is raised -- zero bytes are ever returned to a
caller in that case.

Mirrors app.profiling.csv_loader.resolve_source_path's containment
technique (same defense-in-depth reasoning), applied here to an
already-absolute, server-written output path and an output root instead
of a client-configured relative input path and the input root.
"""
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

# Bounded chunk size used for BOTH the verification read pass and the
# streaming pass -- memory usage never scales with file size in either
# phase, independent of the ~25MB per-artifact ceiling this system
# already enforces at ingestion time (CSV_MAX_FILE_SIZE_BYTES).
ARTIFACT_DOWNLOAD_CHUNK_SIZE_BYTES = 65536


class ArtifactPathError(ValueError):
    """Raised when a run's recorded output path cannot be safely
    resolved: it escapes its tenant-scoped root. Never constructed from
    client input -- the path always comes from a server-written run row;
    this is defense-in-depth, not a response to attacker-controlled
    input."""


class ArtifactMissingError(OSError):
    """Raised when the artifact cannot be opened as a regular file --
    covers both 'never existed' and the narrow race where it existed at
    resolution time but disappeared or became unreadable before the
    verification pass completed. failure_reason_code distinguishes the
    two ('file_not_found' vs 'not_a_regular_file' vs 'io_error')."""

    def __init__(self, failure_reason_code: str, *args: object) -> None:
        super().__init__(*args)
        self.failure_reason_code = failure_reason_code


class ArtifactIntegrityError(ValueError):
    """Raised when the artifact's computed SHA-256 does not match the
    run's recorded output_sha256. The file has already been closed by
    the time this is raised -- see open_verified_artifact."""


def resolve_artifact_path(tenant_root: Path, output_file_path: str) -> Path:
    """Confirms output_file_path resolves inside tenant_root. Raises
    ArtifactPathError if it does not -- defense-in-depth against a
    hypothetical stored-data bug, since output_file_path is always
    computed and written server-side by CleaningHandler/
    StandardizationHandler/ExportHandler, never client-supplied."""
    root = tenant_root.resolve()
    resolved = Path(output_file_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ArtifactPathError("path_containment_violation") from exc
    return resolved


def open_verified_artifact(path: Path, expected_sha256: str) -> BinaryIO:
    """Opens path exactly once, reads it in full via bounded chunks to
    compute its SHA-256, and either:
      - raises ArtifactMissingError if the file cannot be opened/read at
        the OS level (does not exist, is not a regular file, or another
        I/O error occurs) -- no bytes were ever read from a stream sense;
      - raises ArtifactIntegrityError if the computed hash does not match
        expected_sha256 -- the file is closed before this is raised, so
        zero bytes are ever returned to the caller; or
      - returns the SAME open file descriptor, rewound to the start,
        ready to stream -- no second open() call, no second path
        resolution, closing the TOCTOU gap by construction (see module
        docstring).
    """
    try:
        fileobj = path.open("rb")
    except FileNotFoundError as exc:
        raise ArtifactMissingError("file_not_found") from exc
    except IsADirectoryError as exc:
        raise ArtifactMissingError("not_a_regular_file") from exc
    except OSError as exc:
        raise ArtifactMissingError("io_error") from exc

    try:
        digest = hashlib.sha256()
        while True:
            chunk = fileobj.read(ARTIFACT_DOWNLOAD_CHUNK_SIZE_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    except OSError as exc:
        fileobj.close()
        raise ArtifactMissingError("io_error") from exc

    if digest.hexdigest() != expected_sha256:
        fileobj.close()
        raise ArtifactIntegrityError("hash_mismatch")

    fileobj.seek(0)
    return fileobj


def iter_artifact_chunks(fileobj: BinaryIO) -> Iterator[bytes]:
    """Bounded-chunk generator streaming from an already-open,
    already-verified file descriptor (the one returned by
    open_verified_artifact -- never a freshly reopened one). Always
    closes the descriptor, whether exhausted normally or interrupted by
    an error."""
    try:
        while True:
            chunk = fileobj.read(ARTIFACT_DOWNLOAD_CHUNK_SIZE_BYTES)
            if not chunk:
                break
            yield chunk
    finally:
        fileobj.close()


def safe_download_filename(artifact_type: str, run_id: object) -> str:
    """A synthetic, server-generated filename for Content-Disposition --
    built only from the artifact type and the run's UUID, never from the
    real on-disk filename or any part of output_file_path. Guarantees no
    filesystem detail is ever exposed via a response header."""
    return f"{artifact_type}-{run_id}.csv"
