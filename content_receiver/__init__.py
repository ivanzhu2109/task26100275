"""终端侧离线内容包接收库。

上层播放器直接使用：
    from content_receiver import ContentReceiver, Manifest, FileEntry
"""

from .errors import (
    ContentReceiverError,
    InvalidChunkError,
    InvalidManifestError,
    ManifestConflictError,
    UnknownFileError,
    UnknownVersionError,
)
from .receiver import ContentReceiver
from .types import CHUNK_SIZE, CurrentPackage, FileEntry, Manifest, Progress

__all__ = [
    "CHUNK_SIZE",
    "ContentReceiver",
    "ContentReceiverError",
    "CurrentPackage",
    "FileEntry",
    "InvalidChunkError",
    "InvalidManifestError",
    "Manifest",
    "ManifestConflictError",
    "Progress",
    "UnknownFileError",
    "UnknownVersionError",
]
