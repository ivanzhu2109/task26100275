"""Manifest parsing and validation.

A manifest is a JSON document supplied by the caller:

    {
      "version": 3,
      "files": [
        {"path": "layout.json", "length": 1234,
         "chunks": ["<sha256 hex>", ...]},
        ...
      ]
    }

Files are split into 1 MiB chunks; the last chunk of a file may be
shorter.  The caller has already validated business-level references;
this module only checks structural consistency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .errors import ManifestError

CHUNK_SIZE = 1 << 20  # 1 MiB


@dataclass(frozen=True)
class FileEntry:
    """One file listed in a manifest."""

    path: str
    length: int
    chunk_hashes: tuple[str, ...]


@dataclass(frozen=True)
class Manifest:
    """Parsed, structurally validated manifest."""

    version: int
    files: tuple[FileEntry, ...]
    raw: bytes  # original manifest bytes, returned verbatim on read

    def file_entry(self, path: str) -> FileEntry:
        """Return the entry for path (raises KeyError if absent)."""
        for entry in self.files:
            if entry.path == path:
                return entry
        raise KeyError(path)


def _check_path(path: object) -> str:
    if not isinstance(path, str) or not path:
        raise ManifestError(f"invalid file path: {path!r}")
    if path.startswith(("/", "\\")) or "\x00" in path:
        raise ManifestError(f"file path must be relative: {path!r}")
    parts = path.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ManifestError(f"unsafe file path: {path!r}")
    return path


def _check_hash(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ManifestError(f"invalid sha256 hex digest: {value!r}")
    return value


def _parse_file(item: object) -> FileEntry:
    if not isinstance(item, dict):
        raise ManifestError("file entry must be an object")
    path = _check_path(item.get("path"))
    length = item.get("length")
    if not isinstance(length, int) or isinstance(length, bool) or length < 0:
        raise ManifestError(f"invalid length for {path!r}: {length!r}")
    chunks = item.get("chunks")
    if not isinstance(chunks, list):
        raise ManifestError(f"chunks for {path!r} must be a list")
    hashes = tuple(_check_hash(h) for h in chunks)
    expected = (length + CHUNK_SIZE - 1) // CHUNK_SIZE
    if len(hashes) != expected:
        raise ManifestError(
            f"{path!r}: length {length} implies {expected} chunks, "
            f"got {len(hashes)}"
        )
    return FileEntry(path=path, length=length, chunk_hashes=hashes)


def parse_manifest(raw: bytes) -> Manifest:
    """Parse and structurally validate manifest bytes."""
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ManifestError("manifest must be a JSON object")
    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise ManifestError(f"version must be a positive integer: {version!r}")
    files = doc.get("files")
    if not isinstance(files, list):
        raise ManifestError("manifest 'files' must be a list")
    entries = tuple(_parse_file(item) for item in files)
    paths = [entry.path for entry in entries]
    if len(set(paths)) != len(paths):
        raise ManifestError("duplicate file paths in manifest")
    return Manifest(version=version, files=entries, raw=bytes(raw))
