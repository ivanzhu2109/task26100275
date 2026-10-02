"""SQLite 状态层：版本、文件、分片接收标记与当前版本号的持久化。"""

from __future__ import annotations

import json
import sqlite3
from typing import List, Optional, Tuple

from .types import FileEntry, Manifest

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    version  INTEGER PRIMARY KEY,
    manifest TEXT NOT NULL,
    complete INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS files (
    version INTEGER NOT NULL,
    path    TEXT NOT NULL,
    length  INTEGER NOT NULL,
    PRIMARY KEY (version, path)
);
CREATE TABLE IF NOT EXISTS chunks (
    version     INTEGER NOT NULL,
    path        TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    length      INTEGER NOT NULL,
    received    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (version, path, chunk_index)
);
"""


def manifest_to_json(manifest: Manifest) -> str:
    """清单的规范化序列化：文件按路径排序，用于同版本一致性比较。"""
    files = sorted(manifest.files, key=lambda f: f.path)
    return json.dumps(
        {
            "version": manifest.version,
            "files": [
                {"path": f.path, "length": f.length, "chunks": list(f.chunk_sha256)}
                for f in files
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def manifest_from_json(text: str) -> Manifest:
    raw = json.loads(text)
    return Manifest(
        version=raw["version"],
        files=tuple(
            FileEntry(path=f["path"], length=f["length"], chunk_sha256=tuple(f["chunks"]))
            for f in raw["files"]
        ),
    )


class StateStore:
    """封装 content.db 的全部读写。所有多步写操作都在单个事务中完成。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get_manifest(self, version: int) -> Optional[Manifest]:
        row = self._conn.execute(
            "SELECT manifest FROM versions WHERE version = ?", (version,)
        ).fetchone()
        return manifest_from_json(row[0]) if row else None

    def registered_versions(self) -> List[int]:
        rows = self._conn.execute("SELECT version FROM versions ORDER BY version")
        return [r[0] for r in rows]

    def register_manifest(self, manifest: Manifest) -> None:
        """在单个事务中写入版本、文件与分片期望记录（幂等）。"""
        text = manifest_to_json(manifest)
        with self._conn:
            self._conn.execute(
                "INSERT INTO versions (version, manifest, complete) VALUES (?, ?, 0)",
                (manifest.version, text),
            )
            for f in manifest.files:
                self._conn.execute(
                    "INSERT INTO files (version, path, length) VALUES (?, ?, ?)",
                    (manifest.version, f.path, f.length),
                )
                remaining = f.length
                for index, digest in enumerate(f.chunk_sha256):
                    chunk_len = min(remaining, 1024 * 1024)
                    self._conn.execute(
                        "INSERT INTO chunks (version, path, chunk_index, sha256, length)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (manifest.version, f.path, index, digest, chunk_len),
                    )
                    remaining -= chunk_len
            # 没有任何分片的包（全部为空文件）登记即完整。
            total = self._conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE version = ?", (manifest.version,)
            ).fetchone()[0]
            if total == 0:
                self._conn.execute(
                    "UPDATE versions SET complete = 1 WHERE version = ?",
                    (manifest.version,),
                )
            self._refresh_current_locked()

    def mark_chunk_received(self, version: int, path: str, chunk_index: int) -> bool:
        """记录一个分片已落盘；返回该版本是否因此变为完整。"""
        with self._conn:
            self._conn.execute(
                "UPDATE chunks SET received = 1"
                " WHERE version = ? AND path = ? AND chunk_index = ?",
                (version, path, chunk_index),
            )
            remaining = self._conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE version = ? AND received = 0",
                (version,),
            ).fetchone()[0]
            if remaining == 0:
                self._conn.execute(
                    "UPDATE versions SET complete = 1 WHERE version = ?", (version,)
                )
                self._refresh_current_locked()
                return True
            return False

    def missing_chunks(self, version: int) -> List[Tuple[str, int]]:
        rows = self._conn.execute(
            "SELECT path, chunk_index FROM chunks"
            " WHERE version = ? AND received = 0 ORDER BY path, chunk_index",
            (version,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def is_chunk_received(self, version: int, path: str, chunk_index: int) -> bool:
        row = self._conn.execute(
            "SELECT received FROM chunks"
            " WHERE version = ? AND path = ? AND chunk_index = ?",
            (version, path, chunk_index),
        ).fetchone()
        return bool(row and row[0])

    def chunk_counts(self, version: int) -> Tuple[int, int]:
        """返回 (总分片数, 已收分片数)。"""
        total, received = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(received), 0) FROM chunks WHERE version = ?",
            (version,),
        ).fetchone()
        return total, received

    def is_complete(self, version: int) -> bool:
        row = self._conn.execute(
            "SELECT complete FROM versions WHERE version = ?", (version,)
        ).fetchone()
        return bool(row and row[0])

    def current_version(self) -> Optional[int]:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'current_version'"
        ).fetchone()
        return int(row[0]) if row else None

    def refresh_current_version(self) -> Optional[int]:
        """把当前版本设为已完整版本中的最高者（永不回退）。"""
        with self._conn:
            return self._refresh_current_locked()

    def _refresh_current_locked(self) -> Optional[int]:
        row = self._conn.execute(
            "SELECT MAX(version) FROM versions WHERE complete = 1"
        ).fetchone()
        best = row[0]
        if best is None:
            return None
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('current_version', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(best),),
        )
        return int(best)
