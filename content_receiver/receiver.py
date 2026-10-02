"""Public entry point: the offline content package receiver."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .errors import UnknownFileError, UnknownVersionError
from .files import FileStore
from .manifest import CHUNK_SIZE, Manifest, parse_manifest
from .store import StateStore


@dataclass(frozen=True)
class FileProgress:
    """Receive progress for a single file of a version."""

    path: str
    total_chunks: int
    missing_chunks: tuple[int, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_chunks


@dataclass(frozen=True)
class VersionProgress:
    """Receive progress for one registered version."""

    version: int
    files: tuple[FileProgress, ...]

    @property
    def complete(self) -> bool:
        return all(f.complete for f in self.files)


@dataclass(frozen=True)
class ContentPackage:
    """A fully received, playable content package."""

    version: int
    manifest: bytes  # manifest bytes exactly as registered
    files: dict[str, bytes]  # relative path -> content


class ContentReceiver:
    """Receives content packages for a single terminal.

    All state lives under ``root_dir``: package files on disk, receive
    state in a SQLite database.  Calls are serial; the process may be
    killed at any point and recovers by reopening the receiver.
    """

    def __init__(self, root_dir: str) -> None:
        self._files = FileStore(root_dir)
        self._store = StateStore(self._files.db_path)
        self._manifests: dict[int, Manifest] = {}
        self._recover()

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "ContentReceiver":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internal helpers -------------------------------------------

    def _get_manifest(self, version: int) -> Manifest:
        cached = self._manifests.get(version)
        if cached is not None:
            return cached
        raw = self._store.get_manifest(version)
        if raw is None:
            raise UnknownVersionError(f"version {version} is not registered")
        manifest = parse_manifest(raw)
        self._manifests[version] = manifest
        return manifest

    def _refresh_version(self, version: int) -> None:
        """Assemble any files whose chunks have all arrived.

        When every file of the version is assembled, mark the version
        complete and activate it if it is the highest complete one.
        """
        if self._store.is_complete(version):
            return
        manifest = self._get_manifest(version)
        received = self._store.received_chunks(version)
        for file_id, entry in enumerate(manifest.files):
            if entry.path not in self._store.pending_files(version):
                continue
            if all(
                (entry.path, idx) in received
                for idx in range(len(entry.chunk_hashes))
            ):
                self._files.assemble_file(
                    version, file_id, entry.path, len(entry.chunk_hashes)
                )
                self._store.mark_file_done(version, entry.path)
        if not self._store.pending_files(version):
            self._store.complete_version_and_activate(version)

    def _recover(self) -> None:
        """Restore a consistent state after a possible crash.

        Re-assembles files whose chunks were fully acknowledged but
        whose assembly/activation may have been interrupted, then
        recomputes the current version as the highest complete one.
        """
        for version in self._registered_versions():
            self._refresh_version(version)
        self._store.recover_current()

    def _registered_versions(self) -> list[int]:
        return self._store.registered_versions()

    # -- public API -------------------------------------------------

    def register_version(self, manifest: bytes) -> int:
        """Register a version manifest; returns the version number.

        Re-registering the identical manifest is a no-op; a different
        manifest for the same version raises ManifestConflictError.
        """
        parsed = parse_manifest(manifest)
        self._store.register_version(
            parsed.version,
            parsed.raw,
            [entry.path for entry in parsed.files],
        )
        self._manifests[parsed.version] = parsed
        # Zero-chunk files (and any already-received chunks) may make
        # this version complete immediately.
        self._refresh_version(parsed.version)
        return parsed.version

    def deliver_chunk(
        self, version: int, path: str, chunk_index: int, data: bytes
    ) -> bool:
        """Deliver one chunk; returns True iff it was accepted.

        A chunk is accepted only when its length and SHA-256 match the
        manifest.  Invalid or duplicate chunks leave state unchanged.
        """
        manifest = self._get_manifest(version)
        try:
            entry = manifest.file_entry(path)
        except KeyError:
            raise UnknownFileError(
                f"{path!r} is not in the manifest of version {version}"
            ) from None
        total = len(entry.chunk_hashes)
        if not 0 <= chunk_index < total:
            return False
        remaining = entry.length - chunk_index * CHUNK_SIZE
        expected_len = min(CHUNK_SIZE, remaining)
        if len(data) != expected_len:
            return False
        if hashlib.sha256(data).hexdigest() != entry.chunk_hashes[chunk_index]:
            return False
        if self._store.is_complete(version):
            return True  # valid but nothing left to do
        if (path, chunk_index) in self._store.received_chunks(version):
            return True  # duplicate delivery of an acknowledged chunk
        file_id = manifest.files.index(entry)
        self._files.write_chunk(version, file_id, chunk_index, data)
        self._store.record_chunk(version, path, chunk_index)
        self._refresh_version(version)
        return True

    def progress(self, version: int) -> VersionProgress:
        """Return the exact set of missing chunks for a version."""
        manifest = self._get_manifest(version)
        received = self._store.received_chunks(version)
        files = tuple(
            FileProgress(
                path=entry.path,
                total_chunks=len(entry.chunk_hashes),
                missing_chunks=tuple(
                    idx
                    for idx in range(len(entry.chunk_hashes))
                    if (entry.path, idx) not in received
                ),
            )
            for entry in manifest.files
        )
        return VersionProgress(version=version, files=files)

    def current_version(self) -> int | None:
        """Highest fully received version, or None if nothing is playable."""
        return self._store.current_version()

    def read_current(self) -> ContentPackage | None:
        """Read the current playable package, or None if none exists.

        The manifest and every file belong to the same version and are
        byte-identical to what was registered and delivered.
        """
        version = self._store.current_version()
        if version is None:
            return None
        manifest = self._get_manifest(version)
        files = {
            entry.path: self._files.read_package_file(version, entry.path)
            for entry in manifest.files
        }
        return ContentPackage(
            version=version, manifest=manifest.raw, files=files
        )
