# content-receiver

终端侧离线内容包接收库（纯 Python，无第三方依赖）。由上层播放器直接调用，
不负责传输协议与播放逻辑：调用方生成清单、提供分片字节，库负责校验、
断点续收、崩溃恢复和当前可播放包的原子切换。

## 能力

- `register_version(manifest)` — 登记版本清单，返回版本号
- `deliver_chunk(version, path, chunk_index, data)` — 投递一个分片，返回是否被接受
- `progress(version)` — 查询某版本的缺片集合
- `current_version()` / `read_current()` — 查看/读取当前完整可播放包

## 快速开始

```python
from content_receiver import ContentReceiver

rx = ContentReceiver("/var/screen/content")   # 所有状态都在该目录下

version = rx.register_version(manifest_bytes) # 清单为 JSON 字节
rx.deliver_chunk(version, "img/a.png", 0, chunk_bytes)  # 乱序/重复均可

prog = rx.progress(version)                   # 精确缺片列表
for f in prog.files:
    print(f.path, f.missing_chunks)

pkg = rx.current_version()                   # 已收齐的最高版本，或 None
pkg = rx.read_current()                       # None 表示暂无可播放内容
if pkg:
    play(pkg.manifest, pkg.files)             # files: {相对路径: 字节}
rx.close()
```

## 清单格式

```json
{
  "version": 3,
  "files": [
    {"path": "img/a.png", "length": 1234,
     "chunks": ["<第0片SHA-256 hex>", "..."]}
  ]
}
```

- 版本号为正整数，越大越新；文件按 1 MiB 切片，末片可短。
- `chunks` 按分片顺序列出各片的 SHA-256（小写 hex）；0 字节文件 `chunks` 为空数组。
- 同一版本重复登记相同清单不清除已有进度；登记不同清单抛 `ManifestConflictError`。

## 行为保证

- **校验**：只有长度和 SHA-256 均符合清单的分片才被接受；错误或重复分片不改变状态。
- **激活**：每次成功返回后，当前包 = 已收齐的最高版本；迟到的旧版本永不回退。
- **崩溃安全**：分片先落盘（tmp + fsync + 原子 rename）再记入 SQLite
  （WAL + synchronous=FULL）；版本完成与激活在同一事务提交。进程在任意时刻
  被 kill 后，重新 `ContentReceiver(root)` 即自动恢复：已确认分片无需重传，
  未完成的装配/激活自动补齐，当前版本重算为已完整的最高版本。
- **版本隔离**：各版本分片与成品文件分目录存放，互不覆盖；读取当前包时
  清单与全部文件必属同一版本且与输入逐字节一致。

## 存储布局

```
<root>/state.db                          接收状态（SQLite）
<root>/staging/<version>/<file_id>/N.chunk   已校验待装配分片
<root>/packages/<version>/<相对路径>      已装配的完整文件
```

## 运行测试

```
python3 -m unittest discover -s tests -v
```

覆盖：乱序/重复/坏分片混合投递与缺片集合核对、多版本交错激活与防回退、
同路径跨版本隔离、SIGKILL 崩溃后恢复（含每个投递前缀的kill点与
装配/激活中间态），以及恢复后结果与不中断执行的一致性。
