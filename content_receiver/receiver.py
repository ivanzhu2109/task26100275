"""ContentReceiver：终端侧离线内容包接收库的主入口。"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Dict, Optional

from .errors import (
    ContentReceiverError,
    InvalidChunkError,
    InvalidManifestError,
    ManifestConflictError,
    UnknownFileError,
    UnknownVersionError,
)
from .store import StateStore
from .store import manifest_to_json
from .types import CHUNK_SIZE, CurrentPackage, FileEntry, Manifest, Progress

MAX_PACKAGE_BYTES = 512 * 1024 * 1024  # 512 MiB
MAX_FILES = 2000
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def _validate_path(path: str) -> None:
    if not path or path.startswith("/") or "\\" in path or "\0" in path:
        raise InvalidManifestError(f"非法相对路径: {path!r}")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise InvalidManifestError(f"非法相对路径: {path!r}")


def _validate_manifest(manifest: Manifest) -> None:
    if not isinstance(manifest.version, int) or manifest.version <= 0:
        raise InvalidManifestError("版本号必须是正整数")
    if len(manifest.files) > MAX_FILES:
        raise InvalidManifestError(f"文件数超过上限 {MAX_FILES}")
    seen = set()
    total = 0
    for f in manifest.files:
        _validate_path(f.path)
        if f.path in seen:
            raise InvalidManifestError(f"路径重复: {f.path!r}")
        seen.add(f.path)
        if f.length < 0:
            raise InvalidManifestError(f"长度不能为负: {f.path!r}")
        expected_chunks = (f.length + CHUNK_SIZE - 1) // CHUNK_SIZE
        if len(f.chunk_sha256) != expected_chunks:
            raise InvalidManifestError(
                f"分片数与长度不符: {f.path!r} 期望 {expected_chunks} 片"
            )
        for digest in f.chunk_sha256:
            if not _SHA256_HEX.fullmatch(digest):
                raise InvalidManifestError(f"摘要必须是 64 位小写十六进制: {f.path!r}")
        total += f.length
    if total > MAX_PACKAGE_BYTES:
        raise InvalidManifestError("包总大小超过 512 MiB")


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ContentReceiver:
    """由上层播放器直接实例化并串行调用。

    root_dir 由调用方指定，库在其中创建：
      content.db            SQLite 接收状态
      versions/<版本号>/    各版本的文件内容（按清单相对路径存放）
    """

    def __init__(self, root_dir: str) -> None:
        self._root = os.path.abspath(root_dir)
        self._versions_dir = os.path.join(self._root, "versions")
        os.makedirs(self._versions_dir, exist_ok=True)
        _fsync_dir(self._root)
        self._store = StateStore(os.path.join(self._root, "content.db"))
        # 崩溃恢复：当前版本恒等于已完整版本的最高者，重算幂等。
        self._store.refresh_current_version()
        # 确保所有已登记版本的空文件真实存在（注册与崩溃恢复两处都会走到）。
        for version in self._store.registered_versions():
            manifest = self._store.get_manifest(version)
            assert manifest is not None
            self._create_empty_files(manifest)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "ContentReceiver":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def register_version(self, manifest: Manifest) -> None:
        """登记一个版本的清单。重复登记相同清单为幂等空操作。"""
        _validate_manifest(manifest)
        existing = self._store.get_manifest(manifest.version)
        if existing is not None:
            if manifest_to_json(existing) != manifest_to_json(manifest):
                raise ManifestConflictError(
                    f"版本 {manifest.version} 已登记过不同的清单"
                )
            return  # 相同清单重复登记：保留进度，直接返回
        self._create_empty_files(manifest)  # 先落盘空文件，再登记元数据
        self._store.register_manifest(manifest)

    def _create_empty_files(self, manifest: Manifest) -> None:
        for f in manifest.files:
            if f.length > 0:
                continue
            file_path = self._file_path(manifest.version, f.path)
            parent = os.path.dirname(file_path)
            os.makedirs(parent, exist_ok=True)
            if not os.path.exists(file_path):
                with open(file_path, "wb"):
                    pass
                _fsync_dir(parent)

    def deliver_chunk(self, version: int, path: str, chunk_index: int, data: bytes) -> bool:
        """投递一个分片。校验长度与摘要，合法则持久化并返回 True，
        与清单不符则抛 InvalidChunkError，重复投递返回 False（幂等）。"""
        manifest = self._store.get_manifest(version)
        if manifest is None:
            raise UnknownVersionError(f"版本 {version} 尚未登记")
        entry = self._file_index(manifest).get(path)
        if entry is None:
            raise UnknownFileError(f"版本 {version} 的清单中没有 {path!r}")
        if not 0 <= chunk_index < len(entry.chunk_sha256):
            raise InvalidChunkError(f"分片序号越界: {path!r}#{chunk_index}")
        remaining = entry.length - chunk_index * CHUNK_SIZE
        expected_len = min(remaining, CHUNK_SIZE)
        if len(data) != expected_len:
            raise InvalidChunkError(
                f"分片长度不符: {path!r}#{chunk_index} 期望 {expected_len} 字节"
            )
        if hashlib.sha256(data).hexdigest() != entry.chunk_sha256[chunk_index]:
            raise InvalidChunkError(f"分片摘要不符: {path!r}#{chunk_index}")
        if self._store.is_chunk_received(version, path, chunk_index):
            return False  # 重复投递：不改变进度
        self._write_chunk(version, path, chunk_index, data)
        self._store.mark_chunk_received(version, path, chunk_index)
        return True

    def progress(self, version: int) -> Progress:
        """查看某版本的接收进度与缺片集合。"""
        if self._store.get_manifest(version) is None:
            raise UnknownVersionError(f"版本 {version} 尚未登记")
        total, received = self._store.chunk_counts(version)
        return Progress(
            version=version,
            total_chunks=total,
            received_chunks=received,
            complete=self._store.is_complete(version),
            missing=tuple(self._store.missing_chunks(version)),
        )

    def current(self) -> CurrentPackage:
        """当前可播放版本及其清单；暂无可播放内容时 version 为 None。"""
        version = self._store.current_version()
        if version is None:
            return CurrentPackage(version=None, manifest=None)
        return CurrentPackage(version=version, manifest=self._store.get_manifest(version))

    def read_file(self, path: str) -> bytes:
        """读取当前版本中的某个文件内容。"""
        current = self.current()
        if current.version is None:
            raise ContentReceiverError("暂无可播放内容")
        assert current.manifest is not None
        if path not in self._file_index(current.manifest):
            raise UnknownFileError(f"当前版本中没有 {path!r}")
        with open(self._file_path(current.version, path), "rb") as fh:
            return fh.read()

    def file_path(self, path: str) -> str:
        """返回当前版本中某文件在磁盘上的绝对路径（供播放器零拷贝读取）。"""
        current = self.current()
        if current.version is None:
            raise ContentReceiverError("暂无可播放内容")
        assert current.manifest is not None
        if path not in self._file_index(current.manifest):
            raise UnknownFileError(f"当前版本中没有 {path!r}")
        return self._file_path(current.version, path)

    @staticmethod
    def _file_index(manifest: Manifest) -> Dict[str, FileEntry]:
        return {f.path: f for f in manifest.files}

    def _file_path(self, version: int, path: str) -> str:
        return os.path.join(self._versions_dir, str(version), *path.split("/"))

    def _write_chunk(self, version: int, path: str, chunk_index: int, data: bytes) -> None:
        """把分片写入最终位置并 fsync，然后才允许 SQLite 记录（先数据后元数据）。"""
        file_path = self._file_path(version, path)
        parent = os.path.dirname(file_path)
        created_dirs = not os.path.isdir(parent)
        os.makedirs(parent, exist_ok=True)
        created_file = not os.path.exists(file_path)
        # 'r+b' 要求文件已存在；不存在则用 'w+b' 创建（不会截断已有内容之外的区域）。
        mode = "r+b" if not created_file else "w+b"
        with open(file_path, mode) as fh:
            fh.seek(chunk_index * CHUNK_SIZE)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if created_file:
            _fsync_dir(parent)
        if created_dirs:
            _fsync_dir(os.path.join(self._versions_dir, str(version)))
