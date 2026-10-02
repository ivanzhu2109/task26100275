# content_receiver — 终端侧离线内容包接收库

面向门店屏幕的离线内容包接收库。上层播放器直接 import 使用，无接口层、无传输协议、无播放逻辑。
内容存本地文件，接收状态存 SQLite，所有存储路径由调用方指定。仅依赖 Python 标准库（3.9+）。

## 能力

- `register_version(manifest)`：登记一个版本的清单。相同清单重复登记幂等；同版本不同清单抛 `ManifestConflictError`。
- `deliver_chunk(version, path, chunk_index, data)`：投递分片。长度与 SHA-256 均符合清单才接受（返回 `True`）；
  重复投递返回 `False`；不符抛 `InvalidChunkError`，已有有效数据不受影响。
- `progress(version)`：接收进度，含精确的缺片集合 `missing`（`(路径, 分片序号)` 元组）。
- `current()`：当前可播放版本（已收齐的最高版本）及其清单；暂无可播放内容时 `version` 为 `None`。
- `read_file(path)` / `file_path(path)`：读取当前版本的文件内容 / 获取磁盘路径。

## 快速开始

```python
import hashlib
from content_receiver import ContentReceiver, FileEntry, Manifest, CHUNK_SIZE

data = b"...file bytes..."
manifest = Manifest(
    version=3,
    files=(
        FileEntry(
            path="img/poster.png",
            length=len(data),
            chunk_sha256=tuple(
                hashlib.sha256(data[i:i + CHUNK_SIZE]).hexdigest()
                for i in range(0, len(data), CHUNK_SIZE)
            ),
        ),
    ),
)

receiver = ContentReceiver("/var/screens/player1/store")  # 路径由调用方指定
receiver.register_version(manifest)
receiver.deliver_chunk(3, "img/poster.png", 0, data[:CHUNK_SIZE])

cur = receiver.current()
if cur.version is not None:
    poster = receiver.read_file("img/poster.png")
receiver.close()
```

## 规则与保证

- 版本号为正整数，数值越大越新；文件按 1 MiB 切片，末片可短；空文件没有分片。
- 分片可乱序、重复到达；只有长度和摘要均符合清单的分片才被记录。
- 未收齐的版本不可播放；当前版本恒为已收齐的最高版本，迟到的旧版本永不回退。
- 每个版本的内容独立存放于 `versions/<版本号>/`，同路径文件跨版本互不影响。
- 崩溃安全：分片先写盘并 fsync，再在单个 SQLite 事务中记录；版本收齐与激活当前版本在同一事务完成。
  进程在任意时刻被杀死，重启后已确认分片不丢失，自动恢复出已完整接收的最高版本。
- 约束：单包 ≤ 512 MiB、≤ 2000 个文件；接收与恢复的额外内存 ≤ 64 MiB（分片流式写入，无整包缓冲）。

## 存储布局（root_dir 内）

```
content.db          SQLite 接收状态（WAL 模式，synchronous=FULL）
versions/<版本>/    各版本文件内容，按清单相对路径存放
```

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：乱序/重复/摘要错误分片混合到达与缺片集合核对、版本交错接收与不回退、
kill -9 强制终止后的进度恢复与最终内容一致性、清单冲突与结构校验、空包即时激活。
