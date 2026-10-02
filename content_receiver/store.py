"""SQLite-backed receive-state storage.

Tracks registered versions, verified chunks, assembled files and the
currently active version.  All mutations are committed before the
calling operation returns, so acknowledged progress survives a crash.
"""

from __future__ import annotations

import sqlite3

from .errors import ManifestConflictError

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version INTEGER PRIMARY KEY,
    manifest BLOB NOT NULL,
    complete INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chunks (
    version INTEGER NOT NULL,
    path TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    PRIMARY KEY (version, path, chunk_index)
);
CREATE TABLE IF NOT EXISTS files (
    version INTEGER NOT NULL,
    path TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (version, path)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER
);
"""

_CURRENT_KEY = "current_version"


class StateStore:
    """Thin wrapper around the SQLite state database."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- versions ---------------------------------------------------

    def register_version(
        self, version: int, manifest: bytes, paths: list[str]
    ) -> None:
        """Insert a version row; conflict if a different manifest exists."""
        row = self._conn.execute(
            "SELECT manifest FROM versions WHERE version = ?", (version,)
        ).fetchone()
        if row is not None:
            if bytes(row[0]) != manifest:
                raise ManifestConflictError(
                    f"version {version} already registered with a "
                    "different manifest"
                )
            return  # identical re-registration keeps existing progress
        with self._conn:
            self._conn.execute(
                "INSERT INTO versions (version, manifest, complete) "
                "VALUES (?, ?, 0)",
                (version, manifest),
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO files (version, path, done) "
                "VALUES (?, ?, 0)",
                [(version, p) for p in paths],
            )

    def get_manifest(self, version: int) -> bytes | None:
        row = self._conn.execute(
            "SELECT manifest FROM versions WHERE version = ?", (version,)
        ).fetchone()
        return None if row is None else bytes(row[0])

    def is_complete(self, version: int) -> bool:
        row = self._conn.execute(
            "SELECT complete FROM versions WHERE version = ?", (version,)
        ).fetchone()
        return bool(row and row[0])

    def registered_versions(self) -> list[int]:
        rows = self._conn.execute(
            "SELECT version FROM versions ORDER BY version"
        ).fetchall()
        return [int(row[0]) for row in rows]

    # -- chunks -----------------------------------------------------

    def record_chunk(self, version: int, path: str, chunk_index: int) -> bool:
        """Record a verified chunk; True iff it was not already recorded."""
        with self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO chunks (version, path, chunk_index) "
                "VALUES (?, ?, ?)",
                (version, path, chunk_index),
            )
        return cur.rowcount > 0

    def received_chunks(self, version: int) -> set[tuple[str, int]]:
        rows = self._conn.execute(
            "SELECT path, chunk_index FROM chunks WHERE version = ?",
            (version,),
        ).fetchall()
        return {(path, idx) for path, idx in rows}

    # -- files ------------------------------------------------------

    def mark_file_done(self, version: int, path: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE files SET done = 1 WHERE version = ? AND path = ?",
                (version, path),
            )

    def pending_files(self, version: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT path FROM files WHERE version = ? AND done = 0",
            (version,),
        ).fetchall()
        return [row[0] for row in rows]

    # -- completion / activation ------------------------------------

    def complete_version_and_activate(self, version: int) -> None:
        """Mark a version complete and activate it if it is the highest.

        Both updates commit in a single transaction so a crash can never
        leave a complete-but-unactivatable or half-activated state.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE versions SET complete = 1 WHERE version = ?",
                (version,),
            )
            self._conn.execute(
                f"INSERT INTO meta (key, value) VALUES ('{_CURRENT_KEY}', ?) "
                f"ON CONFLICT(key) DO UPDATE SET "
                f"value = MAX(value, excluded.value)",
                (version,),
            )

    def current_version(self) -> int | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (_CURRENT_KEY,)
        ).fetchone()
        return None if row is None else int(row[0])

    def recover_current(self) -> int | None:
        """Recompute current as the highest complete version (crash recovery)."""
        row = self._conn.execute(
            "SELECT MAX(version) FROM versions WHERE complete = 1"
        ).fetchone()
        highest = row[0]
        with self._conn:
            if highest is None:
                self._conn.execute(
                    "DELETE FROM meta WHERE key = ?", (_CURRENT_KEY,)
                )
            else:
                self._conn.execute(
                    f"INSERT INTO meta (key, value) VALUES ('{_CURRENT_KEY}', ?) "
                    f"ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (highest,),
                )
        return None if highest is None else int(highest)
