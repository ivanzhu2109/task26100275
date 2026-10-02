"""Filesystem layout and crash-safe file operations.

Layout under the caller-supplied root directory:

    <root>/state.db                       SQLite receive state
    <root>/staging/<version>/<file_id>/<index>.chunk
                                          verified chunks awaiting assembly
    <root>/packages/<version>/<relpath>   assembled, playable files

Every write goes to a temporary file, is fsynced, then atomically
renamed into place; parent directories are fsynced afterwards.  A
crash can therefore leave at most an orphaned temp or chunk file,
never a partially visible package file.
"""

from __future__ import annotations

import os

STAGING_DIR = "staging"
PACKAGES_DIR = "packages"
DB_NAME = "state.db"


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: str, data: bytes) -> None:
    """Write data to path atomically (tmp file + fsync + rename)."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(parent)


class FileStore:
    """Owns the on-disk layout for chunks and assembled packages."""

    def __init__(self, root_dir: str) -> None:
        self._root = root_dir
        os.makedirs(root_dir, exist_ok=True)

    @property
    def db_path(self) -> str:
        return os.path.join(self._root, DB_NAME)

    def _staging_dir(self, version: int, file_id: int) -> str:
        return os.path.join(
            self._root, STAGING_DIR, str(version), str(file_id)
        )

    def chunk_path(self, version: int, file_id: int, index: int) -> str:
        return os.path.join(
            self._staging_dir(version, file_id), f"{index}.chunk"
        )

    def package_path(self, version: int, relpath: str) -> str:
        return os.path.join(self._root, PACKAGES_DIR, str(version), relpath)

    def write_chunk(
        self, version: int, file_id: int, index: int, data: bytes
    ) -> None:
        """Persist one verified chunk (idempotent, atomic)."""
        _atomic_write(self.chunk_path(version, file_id, index), data)

    def assemble_file(
        self,
        version: int,
        file_id: int,
        relpath: str,
        chunk_count: int,
    ) -> None:
        """Concatenate staged chunks into the final package file."""
        dest = self.package_path(version, relpath)
        parent = os.path.dirname(dest)
        os.makedirs(parent, exist_ok=True)
        tmp = dest + ".tmp"
        with open(tmp, "wb") as out:
            for index in range(chunk_count):
                with open(
                    self.chunk_path(version, file_id, index), "rb"
                ) as part:
                    while True:
                        block = part.read(1 << 20)
                        if not block:
                            break
                        out.write(block)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dest)
        _fsync_dir(parent)

    def read_package_file(self, version: int, relpath: str) -> bytes:
        with open(self.package_path(version, relpath), "rb") as fh:
            return fh.read()
