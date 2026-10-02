"""公开数据类型。

清单（Manifest）由调用方构造，描述一个完整内容包：
每个文件给出相对路径、总长度和按 1 MiB 切片后各分片的 SHA-256（十六进制小写）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

CHUNK_SIZE = 1024 * 1024  # 1 MiB


@dataclass(frozen=True)
class FileEntry:
    """清单中的单个文件条目。

    path: 相对路径（POSIX 风格，不允许绝对路径或 ".."）。
    length: 文件总字节数，>= 0。
    chunk_sha256: 各分片的 SHA-256 十六进制摘要；空文件为空列表，
        否则长度为 ceil(length / CHUNK_SIZE)，末片可以较短。
    """

    path: str
    length: int
    chunk_sha256: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Manifest:
    """一个版本的完整清单。同一版本号的清单必须逐字节一致。"""

    version: int
    files: Tuple[FileEntry, ...] = ()


@dataclass(frozen=True)
class Progress:
    """某个版本的接收进度。

    missing: 尚缺的分片集合，元素为 (文件相对路径, 分片序号)。
    """

    version: int
    total_chunks: int
    received_chunks: int
    complete: bool
    missing: Tuple[Tuple[str, int], ...] = ()


@dataclass(frozen=True)
class CurrentPackage:
    """当前可播放的完整包。version 为 None 表示暂无可播放内容。"""

    version: Optional[int]
    manifest: Optional[Manifest]
