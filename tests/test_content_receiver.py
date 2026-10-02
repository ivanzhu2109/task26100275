"""content_receiver 自动化测试。

运行方式：python3 -m unittest discover -s tests -v
"""

import hashlib
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from content_receiver import (  # noqa: E402
    CHUNK_SIZE,
    ContentReceiver,
    FileEntry,
    InvalidChunkError,
    InvalidManifestError,
    Manifest,
    ManifestConflictError,
    UnknownFileError,
    UnknownVersionError,
)


def make_package(version, seed, file_specs):
    """确定性生成一个版本的内容：{path: bytes} 与对应 Manifest。"""
    rng = random.Random(seed)
    files = {}
    entries = []
    for path, length in file_specs:
        data = bytes(rng.randrange(256) for _ in range(length))
        files[path] = data
        chunks = tuple(
            hashlib.sha256(data[i : i + CHUNK_SIZE]).hexdigest()
            for i in range(0, length, CHUNK_SIZE)
        )
        entries.append(FileEntry(path, length, chunks))
    return files, Manifest(version, tuple(entries))


def all_chunks(manifest):
    return [(f.path, i) for f in manifest.files for i in range(len(f.chunk_sha256))]


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="content_receiver_test_")
        self.addCleanup(shutil.rmtree, self.root, True)


class ChunkArrivalTest(TempDirTestCase):
    def test_out_of_order_duplicate_and_corrupt_chunks(self):
        specs = [
            ("layout.json", 500),
            ("img/a.png", CHUNK_SIZE + 123),  # 两片，末片短
            ("img/b.png", 2 * CHUNK_SIZE),
            ("empty.txt", 0),
        ]
        files, manifest = make_package(1, seed=42, file_specs=specs)
        r = ContentReceiver(self.root)
        r.register_version(manifest)

        # 独立记录缺片集合
        missing = set(all_chunks(manifest))
        self.assertEqual(set(r.progress(1).missing), missing)

        order = list(all_chunks(manifest))
        random.Random(7).shuffle(order)  # 乱序
        for path, idx in order:
            data = files[path][idx * CHUNK_SIZE : (idx + 1) * CHUNK_SIZE]
            # 摘要错误的分片：被拒绝且不改变进度
            with self.assertRaises(InvalidChunkError):
                r.deliver_chunk(1, path, idx, bytes(len(data)))
            # 长度错误的分片：同样被拒绝
            with self.assertRaises(InvalidChunkError):
                r.deliver_chunk(1, path, idx, data + b"x")
            self.assertEqual(set(r.progress(1).missing), missing)
            # 正确投递
            self.assertTrue(r.deliver_chunk(1, path, idx, data))
            missing.discard((path, idx))
            self.assertEqual(set(r.progress(1).missing), missing)
            # 重复投递：幂等，不改变进度
            self.assertFalse(r.deliver_chunk(1, path, idx, data))
            self.assertEqual(set(r.progress(1).missing), missing)

        self.assertTrue(r.progress(1).complete)
        self.assertEqual(r.current().version, 1)
        for path, data in files.items():
            self.assertEqual(r.read_file(path), data)
        r.close()

    def test_invalid_chunk_and_unknown_references(self):
        files, manifest = make_package(1, seed=1, file_specs=[("a.bin", 10)])
        r = ContentReceiver(self.root)
        with self.assertRaises(UnknownVersionError):
            r.deliver_chunk(9, "a.bin", 0, files["a.bin"])
        r.register_version(manifest)
        with self.assertRaises(UnknownFileError):
            r.deliver_chunk(1, "nope.bin", 0, b"x")
        with self.assertRaises(InvalidChunkError):
            r.deliver_chunk(1, "a.bin", 1, files["a.bin"])  # 序号越界
        with self.assertRaises(UnknownVersionError):
            r.progress(9)
        r.close()


class VersionInterleavingTest(TempDirTestCase):
    def test_highest_complete_version_wins_and_never_rolls_back(self):
        files1, m1 = make_package(1, seed=11, file_specs=[("v1.txt", 100)])
        files2, m2 = make_package(2, seed=22, file_specs=[("v2.txt", 100)])
        files3, m3 = make_package(
            3, seed=33, file_specs=[("v3a.txt", 100), ("v3b.txt", 100)]
        )
        r = ContentReceiver(self.root)
        for m in (m1, m2, m3):
            r.register_version(m)

        # 版本 1 收齐，正在播放
        r.deliver_chunk(1, "v1.txt", 0, files1["v1.txt"])
        self.assertEqual(r.current().version, 1)

        # 版本 3 只收到一部分
        r.deliver_chunk(3, "v3a.txt", 0, files3["v3a.txt"])
        self.assertEqual(r.current().version, 1)

        # 版本 2 先收齐：播放版本 2
        r.deliver_chunk(2, "v2.txt", 0, files2["v2.txt"])
        self.assertEqual(r.current().version, 2)
        self.assertEqual(r.read_file("v2.txt"), files2["v2.txt"])

        # 版本 3 收齐：播放版本 3
        r.deliver_chunk(3, "v3b.txt", 0, files3["v3b.txt"])
        self.assertEqual(r.current().version, 3)
        self.assertEqual(r.read_file("v3a.txt"), files3["v3a.txt"])
        self.assertEqual(r.read_file("v3b.txt"), files3["v3b.txt"])

        # 迟到的旧版本分片（重复投递）不能导致回退
        r.deliver_chunk(1, "v1.txt", 0, files1["v1.txt"])
        r.deliver_chunk(2, "v2.txt", 0, files2["v2.txt"])
        self.assertEqual(r.current().version, 3)
        r.close()

    def test_same_path_different_versions_are_independent(self):
        files1, m1 = make_package(1, seed=1, file_specs=[("price.json", 64)])
        files2, m2 = make_package(2, seed=2, file_specs=[("price.json", 64)])
        self.assertNotEqual(files1["price.json"], files2["price.json"])
        r = ContentReceiver(self.root)
        r.register_version(m1)
        r.register_version(m2)
        r.deliver_chunk(1, "price.json", 0, files1["price.json"])
        self.assertEqual(r.read_file("price.json"), files1["price.json"])
        # 版本 2 的同路径文件必须独立接收，不覆盖版本 1
        self.assertEqual(set(r.progress(2).missing), {("price.json", 0)})
        r.deliver_chunk(2, "price.json", 0, files2["price.json"])
        self.assertEqual(r.current().version, 2)
        self.assertEqual(r.read_file("price.json"), files2["price.json"])
        r.close()


class ManifestRulesTest(TempDirTestCase):
    def test_conflict_and_idempotent_register(self):
        files, m1 = make_package(1, seed=5, file_specs=[("a.txt", 10)])
        r = ContentReceiver(self.root)
        r.register_version(m1)
        r.deliver_chunk(1, "a.txt", 0, files["a.txt"])
        # 相同清单重复登记：不清空进度
        r.register_version(m1)
        self.assertTrue(r.progress(1).complete)
        # 同版本不同清单：拒绝
        _, m1_other = make_package(1, seed=6, file_specs=[("a.txt", 10)])
        with self.assertRaises(ManifestConflictError):
            r.register_version(m1_other)
        r.close()

    def test_invalid_manifest_rejected(self):
        r = ContentReceiver(self.root)
        digest = "ab" * 32
        with self.assertRaises(InvalidManifestError):
            r.register_version(Manifest(0, ()))  # 版本号必须为正
        with self.assertRaises(InvalidManifestError):
            r.register_version(Manifest(1, (FileEntry("../x", 1, (digest,)),)))
        with self.assertRaises(InvalidManifestError):
            r.register_version(Manifest(1, (FileEntry("/abs", 1, (digest,)),)))
        with self.assertRaises(InvalidManifestError):
            r.register_version(
                Manifest(1, (FileEntry("a", 2 * CHUNK_SIZE, (digest,)),))
            )  # 片数与长度不符
        with self.assertRaises(InvalidManifestError):
            r.register_version(
                Manifest(1, (FileEntry("a", 1, ("zz" * 32,)),))
            )  # 摘要非法
        with self.assertRaises(InvalidManifestError):
            r.register_version(
                Manifest(1, (FileEntry("a", 0, ()), FileEntry("a", 0, ())))
            )  # 路径重复
        r.close()

    def test_empty_package_activates_on_register(self):
        r = ContentReceiver(self.root)
        self.assertIsNone(r.current().version)
        r.register_version(Manifest(1, (FileEntry("empty.txt", 0, ()),)))
        self.assertEqual(r.current().version, 1)
        self.assertEqual(r.read_file("empty.txt"), b"")
        r.close()


CRASH_WORKER = r"""
import os, sys, time
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from test_content_receiver import make_package
from content_receiver import ContentReceiver

root, ack_log = sys.argv[1], sys.argv[2]
r = ContentReceiver(root)
manifests = []
packages = {}
for v in (1, 2, 3):
    specs = [("f%d_%d.dat" % (v, i), 1000 + i) for i in range(30)]
    files, m = make_package(v, seed=100 + v, file_specs=specs)
    r.register_version(m)
    manifests.append(m)
    packages[v] = files

with open(ack_log, "w") as log:
    # 三个版本交错投递，每确认一片写一条 ack 并刷盘
    for v in (1, 2, 3):
        for path, data in sorted(packages[v].items()):
            r.deliver_chunk(v, path, 0, data)
            log.write("%d %s\n" % (v, path))
            log.flush()
            os.fsync(log.fileno())
            time.sleep(0.02)
with open(ack_log + ".done", "w") as f:
    f.write("done")
"""


class CrashRecoveryTest(TempDirTestCase):
    def test_kill_mid_receive_then_resume(self):
        ack_log = os.path.join(self.root, "acks.log")
        worker = os.path.join(os.path.dirname(__file__), "_crash_worker.py")
        with open(worker, "w") as fh:
            fh.write(CRASH_WORKER)
        self.addCleanup(os.remove, worker)

        proc = subprocess.Popen([sys.executable, worker, self.root, ack_log])
        time.sleep(0.6)  # 让它收到一半左右
        proc.send_signal(signal.SIGKILL)  # 强制终止
        proc.wait()
        self.assertFalse(os.path.exists(ack_log + ".done"))

        with open(ack_log) as fh:
            acks = [tuple(line.split()) for line in fh if line.strip()]
        self.assertGreater(len(acks), 0, "应至少确认过一些分片")

        # 重启：仅凭本地状态恢复，已确认分片不得丢失
        r = ContentReceiver(self.root)
        for v in (1, 2, 3):
            missing = set(r.progress(v).missing)
            for av, path in acks:
                if int(av) == v:
                    self.assertNotIn((path, 0), missing)

        # 恢复后当前版本若存在，其内容必须完整且逐字节正确
        packages = {}
        manifests = {}
        for v in (1, 2, 3):
            specs = [("f%d_%d.dat" % (v, i), 1000 + i) for i in range(30)]
            files, m = make_package(v, seed=100 + v, file_specs=specs)
            packages[v] = files
            manifests[v] = m
        cur = r.current()
        if cur.version is not None:
            for path, data in packages[cur.version].items():
                self.assertEqual(r.read_file(path), data)

        # 继续投递缺片，最终结果与不中断执行一致
        for v in (1, 2, 3):
            for path, idx in list(r.progress(v).missing):
                data = packages[v][path][idx * CHUNK_SIZE : (idx + 1) * CHUNK_SIZE]
                r.deliver_chunk(v, path, idx, data)
        self.assertEqual(r.current().version, 3)
        for path, data in packages[3].items():
            self.assertEqual(r.read_file(path), data)
        r.close()

        # 再重开一次：最高完整版本仍是 3
        r2 = ContentReceiver(self.root)
        self.assertEqual(r2.current().version, 3)
        r2.close()


if __name__ == "__main__":
    unittest.main()
