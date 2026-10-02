"""Acceptance and unit tests for the content receiver library."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import random
import shutil
import signal
import tempfile
import time
import unittest

from content_receiver import (
    ContentReceiver,
    ManifestConflictError,
    ManifestError,
    UnknownFileError,
    UnknownVersionError,
)

CHUNK = 1 << 20


def chunk_data(data: bytes, index: int) -> bytes:
    return data[index * CHUNK : (index + 1) * CHUNK]


def make_manifest(version: int, files: dict[str, bytes]) -> bytes:
    entries = []
    for path, data in files.items():
        hashes = [
            hashlib.sha256(chunk_data(data, i)).hexdigest()
            for i in range((len(data) + CHUNK - 1) // CHUNK)
        ]
        entries.append({"path": path, "length": len(data), "chunks": hashes})
    return json.dumps({"version": version, "files": entries}).encode()


def all_chunks(
    version: int, files: dict[str, bytes]
) -> list[tuple[int, str, int, bytes]]:
    out = []
    for path, data in files.items():
        for i in range((len(data) + CHUNK - 1) // CHUNK):
            out.append((version, path, i, chunk_data(data, i)))
    return out


class ReceiverTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="rx-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def open(self) -> ContentReceiver:
        rx = ContentReceiver(self.root)
        self.addCleanup(rx.close)
        return rx


class ManifestTests(ReceiverTestCase):
    def test_register_and_reject_conflicts(self) -> None:
        rx = self.open()
        m1 = make_manifest(1, {"a.txt": b"aaa"})
        self.assertEqual(rx.register_version(m1), 1)
        rx.register_version(m1)  # identical re-registration is a no-op
        with self.assertRaises(ManifestConflictError):
            rx.register_version(make_manifest(1, {"a.txt": b"different"}))

    def test_reregister_keeps_progress(self) -> None:
        rx = self.open()
        files = {"a.txt": b"a" * (CHUNK + 5)}
        m1 = make_manifest(1, files)
        rx.register_version(m1)
        rx.deliver_chunk(1, "a.txt", 0, chunk_data(files["a.txt"], 0))
        rx.register_version(m1)
        prog = rx.progress(1)
        self.assertEqual(prog.files[0].missing_chunks, (1,))

    def test_invalid_manifests(self) -> None:
        rx = self.open()
        bad = [
            b"not json",
            json.dumps({"version": 0, "files": []}).encode(),
            json.dumps({"version": -2, "files": []}).encode(),
            json.dumps({"version": "1", "files": []}).encode(),
            json.dumps({"version": 1, "files": [{"path": "../x", "length": 0, "chunks": []}]}).encode(),
            json.dumps({"version": 1, "files": [{"path": "/abs", "length": 0, "chunks": []}]}).encode(),
            json.dumps({"version": 1, "files": [{"path": "a", "length": 5, "chunks": []}]}).encode(),
            json.dumps({"version": 1, "files": [{"path": "a", "length": 0, "chunks": ["zz"]}]}).encode(),
            json.dumps({"version": 1, "files": [
                {"path": "a", "length": 0, "chunks": []},
                {"path": "a", "length": 0, "chunks": []},
            ]}).encode(),
        ]
        for raw in bad:
            with self.assertRaises(ManifestError, msg=raw):
                rx.register_version(raw)

    def test_unknown_version_and_file(self) -> None:
        rx = self.open()
        with self.assertRaises(UnknownVersionError):
            rx.deliver_chunk(9, "a", 0, b"")
        with self.assertRaises(UnknownVersionError):
            rx.progress(9)
        rx.register_version(make_manifest(1, {"a": b"x"}))
        with self.assertRaises(UnknownFileError):
            rx.deliver_chunk(1, "nope", 0, b"x")


class ChunkDeliveryTests(ReceiverTestCase):
    def test_out_of_order_duplicate_and_corrupt_chunks(self) -> None:
        rx = self.open()
        rng = random.Random(42)
        files = {
            "layout.json": rng.randbytes(2000),
            "media/clip.bin": rng.randbytes(2 * CHUNK + 123),
            "img/a.png": rng.randbytes(CHUNK - 1),
            "empty.dat": b"",
        }
        rx.register_version(make_manifest(1, files))

        # Independent tracking of what is still missing.
        missing: dict[str, set[int]] = {
            p: set(range((len(d) + CHUNK - 1) // CHUNK))
            for p, d in files.items()
        }
        deliveries = all_chunks(1, files)
        rng.shuffle(deliveries)

        for version, path, idx, data in deliveries:
            # Mix in duplicates and corrupt chunks.
            bad = data + b"x" if rng.random() < 0.3 else None
            if bad is not None:
                self.assertFalse(rx.deliver_chunk(version, path, idx, bad))
            if rng.random() < 0.3:
                mutated = bytearray(data)
                if mutated:
                    mutated[0] ^= 0xFF
                self.assertFalse(
                    rx.deliver_chunk(version, path, idx, bytes(mutated))
                )
            self.assertTrue(rx.deliver_chunk(version, path, idx, data))
            missing[path].discard(idx)
            self.assertTrue(rx.deliver_chunk(version, path, idx, data))

            prog = rx.progress(1)
            for fp in prog.files:
                self.assertEqual(
                    set(fp.missing_chunks),
                    missing[fp.path],
                    f"mismatch for {fp.path}",
                )
            self.assertEqual(prog.complete, not any(missing.values()))

        self.assertEqual(rx.current_version(), 1)
        pkg = rx.read_current()
        self.assertIsNotNone(pkg)
        self.assertEqual(pkg.files, files)
        self.assertEqual(pkg.manifest, make_manifest(1, files))

    def test_wrong_index_and_length_rejected(self) -> None:
        rx = self.open()
        files = {"a": b"hello"}
        rx.register_version(make_manifest(1, files))
        self.assertFalse(rx.deliver_chunk(1, "a", 1, b"hello"))  # bad index
        self.assertFalse(rx.deliver_chunk(1, "a", -1, b"hello"))
        self.assertFalse(rx.deliver_chunk(1, "a", 0, b"hell"))  # bad length
        self.assertFalse(rx.deliver_chunk(1, "a", 0, b"hello!"))
        self.assertEqual(rx.current_version(), None)

    def test_empty_file_and_empty_package(self) -> None:
        rx = self.open()
        rx.register_version(make_manifest(1, {"empty": b""}))
        self.assertEqual(rx.current_version(), 1)
        self.assertEqual(rx.read_current().files, {"empty": b""})

        root2 = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root2, ignore_errors=True)
        rx2 = ContentReceiver(root2)
        self.addCleanup(rx2.close)
        rx2.register_version(make_manifest(2, {}))
        self.assertEqual(rx2.current_version(), 2)
        self.assertEqual(rx2.read_current().files, {})


class VersionActivationTests(ReceiverTestCase):
    def test_activation_order_and_no_regression(self) -> None:
        rx = self.open()
        rng = random.Random(7)
        v1 = {"price.json": b'{"p": 1}', "img.png": rng.randbytes(500)}
        v2 = {"price.json": b'{"p": 2}', "img.png": rng.randbytes(500)}
        v3 = {"price.json": b'{"p": 3}', "img.png": rng.randbytes(500)}
        for v, files in ((1, v1), (2, v2), (3, v3)):
            rx.register_version(make_manifest(v, files))

        # Version 1 fully arrives and becomes playable.
        for args in all_chunks(1, v1):
            rx.deliver_chunk(*args)
        self.assertEqual(rx.current_version(), 1)
        self.assertEqual(rx.read_current().files, v1)

        # Version 3 only partially arrives; version 2 completes first.
        v3_chunks = all_chunks(3, v3)
        rx.deliver_chunk(*v3_chunks[0])
        self.assertEqual(rx.current_version(), 1)
        for args in all_chunks(2, v2):
            rx.deliver_chunk(*args)
        self.assertEqual(rx.current_version(), 2)
        self.assertEqual(rx.read_current().files, v2)

        # Version 3 completes; it takes over.
        for args in v3_chunks[1:]:
            rx.deliver_chunk(*args)
        self.assertEqual(rx.current_version(), 3)
        self.assertEqual(rx.read_current().files, v3)

        # Late deliveries of older versions never cause regression.
        for args in all_chunks(1, v1) + all_chunks(2, v2):
            self.assertTrue(rx.deliver_chunk(*args))
        self.assertEqual(rx.current_version(), 3)
        self.assertEqual(rx.read_current().files, v3)

    def test_same_paths_different_versions_isolated(self) -> None:
        rx = self.open()
        v1 = {"same/path.bin": b"old-content"}
        v2 = {"same/path.bin": b"new-content!!"}
        rx.register_version(make_manifest(1, v1))
        rx.register_version(make_manifest(2, v2))
        # Interleave delivery; v2 completes first.
        rx.deliver_chunk(2, "same/path.bin", 0, v2["same/path.bin"])
        self.assertEqual(rx.current_version(), 2)
        self.assertEqual(rx.read_current().files, v2)
        # v1 still receivable and intact, but cannot regress playback.
        rx.deliver_chunk(1, "same/path.bin", 0, v1["same/path.bin"])
        self.assertEqual(rx.current_version(), 2)
        self.assertEqual(rx.read_current().files, v2)
        self.assertTrue(rx.progress(1).complete)


def _crash_worker(
    root: str,
    manifests: list[bytes],
    deliveries: list[tuple[int, str, int, bytes]],
    stop_after: int,
) -> None:
    """Child process: register, deliver some chunks, then die abruptly."""
    rx = ContentReceiver(root)
    for m in manifests:
        rx.register_version(m)
    for args in deliveries[:stop_after]:
        rx.deliver_chunk(*args)
    os.kill(os.getpid(), signal.SIGKILL)


class CrashRecoveryTests(ReceiverTestCase):
    def _run_crashed(self, manifests, deliveries, stop_after) -> None:
        proc = multiprocessing.Process(
            target=_crash_worker,
            args=(self.root, manifests, deliveries, stop_after),
        )
        proc.start()
        proc.join(timeout=60)
        self.assertEqual(proc.exitcode, -signal.SIGKILL)

    def test_recovery_after_kill(self) -> None:
        rng = random.Random(99)
        v1 = {"a.bin": rng.randbytes(CHUNK + 10), "b.bin": rng.randbytes(50)}
        v2 = {"a.bin": rng.randbytes(CHUNK * 2), "c.bin": rng.randbytes(9)}
        m1, m2 = make_manifest(1, v1), make_manifest(2, v2)
        deliveries = all_chunks(1, v1) + all_chunks(2, v2)
        rng.shuffle(deliveries)

        # Kill the receiver partway through delivery.
        self._run_crashed([m1, m2], deliveries, stop_after=2)

        # Reopen: acknowledged chunks must not need retransmission.
        rx = self.open()
        before = {
            v: {
                fp.path: set(fp.missing_chunks)
                for fp in rx.progress(v).files
            }
            for v in (1, 2)
        }
        self.assertEqual(rx.current_version(), None)

        # Deliver exactly the missing chunks, in shuffled order.
        remaining = [
            args
            for args in all_chunks(1, v1) + all_chunks(2, v2)
            if args[2] in before[args[0]][args[1]]
        ]
        rng.shuffle(remaining)
        for args in remaining:
            rx.deliver_chunk(*args)

        self.assertEqual(rx.current_version(), 2)
        self.assertEqual(rx.read_current().files, v2)
        rx.close()

        # Reopen again: state survives a clean restart too.
        rx = self.open()
        self.assertEqual(rx.current_version(), 2)
        self.assertEqual(rx.read_current().files, v2)

    def test_kill_at_every_point_matches_uninterrupted(self) -> None:
        """Killing after each prefix of deliveries must converge to the
        same final state as an uninterrupted run."""
        rng = random.Random(5)
        v1 = {"x.bin": rng.randbytes(100), "y.bin": rng.randbytes(CHUNK + 1)}
        v2 = {"x.bin": rng.randbytes(40)}
        m1, m2 = make_manifest(1, v1), make_manifest(2, v2)
        deliveries = all_chunks(1, v1) + all_chunks(2, v2)
        rng.shuffle(deliveries)

        # Uninterrupted reference run.
        ref_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, ref_root, ignore_errors=True)
        ref = ContentReceiver(ref_root)
        self.addCleanup(ref.close)
        ref.register_version(m1)
        ref.register_version(m2)
        for args in deliveries:
            ref.deliver_chunk(*args)
        expected_version = ref.current_version()
        expected_files = ref.read_current().files

        for stop_after in range(len(deliveries)):
            with self.subTest(stop_after=stop_after):
                shutil.rmtree(self.root, ignore_errors=True)
                os.makedirs(self.root)
                self._run_crashed([m1, m2], deliveries, stop_after)
                rx = ContentReceiver(self.root)
                # Continue with whatever is still missing.
                for args in deliveries:
                    rx.deliver_chunk(*args)
                self.assertEqual(rx.current_version(), expected_version)
                self.assertEqual(rx.read_current().files, expected_files)
                # And a further reopen keeps the same state.
                rx.close()
                rx = ContentReceiver(self.root)
                self.assertEqual(rx.current_version(), expected_version)
                self.assertEqual(rx.read_current().files, expected_files)
                rx.close()

    def test_no_mixed_versions_after_crash(self) -> None:
        """A crash while v2 is partially assembled must never expose a
        package mixing v1 and v2 files."""
        rng = random.Random(11)
        v1 = {"p": b"v1-price", "q": b"v1-img"}
        v2 = {"p": b"v2-price", "q": b"v2-img"}
        m1, m2 = make_manifest(1, v1), make_manifest(2, v2)
        deliveries = all_chunks(1, v1) + all_chunks(2, v2)[:1]
        self._run_crashed([m1, m2], deliveries, stop_after=len(deliveries))

        rx = self.open()
        self.assertEqual(rx.current_version(), 1)
        self.assertEqual(rx.read_current().files, v1)
        # Finish v2 and confirm a clean switch.
        rx.deliver_chunk(*all_chunks(2, v2)[1])
        self.assertEqual(rx.current_version(), 2)
        self.assertEqual(rx.read_current().files, v2)

    def test_crash_between_chunk_record_and_activation(self) -> None:
        """Simulate a kill right after the last chunk was acknowledged
        but before assembly/activation: recovery must finish the job."""
        from content_receiver.files import FileStore
        from content_receiver.manifest import parse_manifest
        from content_receiver.store import StateStore

        rng = random.Random(13)
        files = {"a.bin": rng.randbytes(CHUNK + 7), "b.bin": rng.randbytes(3)}
        m1 = make_manifest(1, files)
        manifest = parse_manifest(m1)

        fs = FileStore(self.root)
        store = StateStore(fs.db_path)
        store.register_version(1, m1, [e.path for e in manifest.files])
        for file_id, entry in enumerate(manifest.files):
            data = files[entry.path]
            for idx in range(len(entry.chunk_hashes)):
                fs.write_chunk(1, file_id, idx, chunk_data(data, idx))
                store.record_chunk(1, entry.path, idx)
        store.close()  # "crash": no file assembled, version not complete

        rx = self.open()
        self.assertEqual(rx.current_version(), 1)
        self.assertEqual(rx.read_current().files, files)

    def test_crash_between_assembly_and_activation(self) -> None:
        """Files assembled and marked done, but the version was never
        marked complete: recovery must activate it."""
        from content_receiver.files import FileStore
        from content_receiver.manifest import parse_manifest
        from content_receiver.store import StateStore

        files = {"only.bin": b"data"}
        m1 = make_manifest(1, files)
        manifest = parse_manifest(m1)

        fs = FileStore(self.root)
        store = StateStore(fs.db_path)
        store.register_version(1, m1, [e.path for e in manifest.files])
        for file_id, entry in enumerate(manifest.files):
            data = files[entry.path]
            for idx in range(len(entry.chunk_hashes)):
                fs.write_chunk(1, file_id, idx, chunk_data(data, idx))
                store.record_chunk(1, entry.path, idx)
            fs.assemble_file(1, file_id, entry.path, len(entry.chunk_hashes))
            store.mark_file_done(1, entry.path)
        store.close()  # "crash": complete flag never written

        rx = self.open()
        self.assertEqual(rx.current_version(), 1)
        self.assertEqual(rx.read_current().files, files)


if __name__ == "__main__":
    unittest.main()
