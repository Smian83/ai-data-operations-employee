"""
ArtifactStorage: a small, local-filesystem-only I/O abstraction for the
artifact bytes CleaningHandler/StandardizationHandler/ExportHandler
produce (Modules 6/7/9) and that Module 10 reads and Module 13 will
delete. See docs/module-13-output-artifact-retention-design.md.

This module knows nothing about tenants, organizations, or path safety --
it operates exclusively on an already-resolved, already-containment-
checked absolute Path. app.artifacts.download.resolve_artifact_path
remains the single place in this codebase that turns a tenant/run
reference into a safe filesystem path; every caller of ArtifactStorage is
responsible for calling that first. This module's only job is "given a
safe path, perform this one filesystem operation" -- nothing more. It is
a pure I/O primitive layer sitting strictly beneath the existing
tenant-isolation/integrity-verification boundary, never a replacement
for it.

Only one implementation exists, LocalFilesystemArtifactStorage. The
Protocol exists so a future non-local backend is a one-class addition
wherever it becomes justified -- no backend selection, registry, or
plugin mechanism is built now, because nothing today needs one (S3,
Azure Blob, GCS, and similar are explicitly out of scope for this
module).
"""
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO, Protocol


class ArtifactStorage(Protocol):
    """Every operation takes an already-resolved, already-safe absolute
    path -- never a tenant reference, never a relative or unvalidated
    path. Callers are responsible for path resolution and containment
    (app.artifacts.download.resolve_artifact_path) before calling any
    method here. No method here ever raises a tenant- or containment-
    related error; that boundary is enforced strictly before this
    interface is ever reached."""

    def exists(self, path: Path) -> bool:
        """True if a regular file exists at path. False for a missing
        path or a path that exists but is not a regular file (e.g. a
        directory) -- both mean "no artifact file here." Consistent with
        open()/delete() below, a directory at path is never treated as a
        valid artifact."""
        ...

    def open(self, path: Path) -> BinaryIO:
        """Open path for binary reading. Raises FileNotFoundError if it
        does not exist, IsADirectoryError if it is a directory, or
        another OSError subclass (e.g. PermissionError) for a genuine
        I/O failure. Callers (e.g.
        app.artifacts.download.open_verified_artifact) are responsible
        for translating these into this project's own exception types;
        this method itself never wraps, reclassifies, or swallows them."""
        ...

    def delete(self, path: Path) -> bool:
        """Idempotently remove path. Returns True if a regular file
        existed at path and was removed; returns False if nothing was
        there to begin with -- deleting an already-missing artifact
        reaches the same end state as a successful delete, so it is
        success, not an error. Raises for a genuine filesystem failure
        (permission denied, path is a directory, or another I/O error)
        -- these are never silently swallowed; only "was already
        missing" is treated as a non-error outcome."""
        ...


class LocalFilesystemArtifactStorage:
    """The only ArtifactStorage implementation in this system. A thin,
    deliberately unclever wrapper over pathlib -- no caching, no
    retries, no special-casing beyond what exists()/open()/delete()'s
    own contracts above require."""

    def exists(self, path: Path) -> bool:
        # Path.is_file() returns False for a missing path or a
        # directory (its S_ISREG check fails), and -- confirmed
        # empirically, not assumed -- still raises for a genuine access
        # failure such as a permission-denied parent directory, since
        # pathlib only swallows the "doesn't exist / not traversable"
        # class of OSError, not PermissionError.
        return path.is_file()

    def open(self, path: Path) -> BinaryIO:
        # Deliberately a direct, unwrapped delegation: FileNotFoundError/
        # IsADirectoryError/PermissionError/other OSError all propagate
        # exactly as raw pathlib would raise them. This is what makes
        # the Module 10 refactor behavior-preserving -- the calling code
        # (app.artifacts.download.open_verified_artifact) already
        # catches these exact exception types today.
        return path.open("rb")

    def delete(self, path: Path) -> bool:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            # Idempotent: the artifact is already gone, which is exactly
            # the end state a delete() call is trying to reach. Not an
            # error -- confirmed empirically that this is the only
            # "missing" signal unlink() raises; a directory at path
            # raises IsADirectoryError (not FileNotFoundError) and is
            # allowed to propagate below, unmodified, since a directory
            # is never a valid artifact target.
            return False
        # IsADirectoryError, PermissionError, and any other OSError
        # propagate unmodified here -- genuine filesystem failures,
        # including a directory unexpectedly reaching this call, are
        # never silently swallowed.


@lru_cache
def get_artifact_storage() -> ArtifactStorage:
    """Returns the single configured ArtifactStorage implementation for
    this process. Only one implementation exists today
    (LocalFilesystemArtifactStorage); this indirection exists solely so
    callers (app.artifacts.download, and app.worker.retention in a later
    phase) depend on the ArtifactStorage interface rather than importing
    LocalFilesystemArtifactStorage directly -- adding a future non-local
    implementation would be a change here, not at every call site. No
    configuration-driven backend selection exists, deliberately: nothing
    in this system needs one yet. Mirrors
    app.core.config.get_settings()'s own lru_cache singleton
    convention."""
    return LocalFilesystemArtifactStorage()
