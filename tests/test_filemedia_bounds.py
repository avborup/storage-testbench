# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Task-12 Mechanism-5 bounds: prove the whole up+down path streams in BOUNDED
memory and LINEAR time over the FileMedia backend.

Driven IN-PROCESS against FileStore / FileMedia (never against RUSAGE_SELF while
an out-of-process gunicorn grandchild moves the bytes) so the RSS/tracemalloc
samples observe the code that actually streams. `ru_maxrss` is unit-normalised
(bytes on macOS, KiB on Linux). The default leg uses N = 256 MiB so a laptop can
run it; the full 4 GiB up+down leg is gated behind TESTBENCH_BOUNDS_4GB=1 and is
exercised in CI/nightly. A whole-buffer materialiser anywhere on the path (a
surviving to_bytes()/slice) makes the object resident and blows the 256-MiB
ceiling; a quadratic append/read makes t(2N)/t(N) >= 3.
"""

import json
import os
import resource
import shutil
import sys
import tempfile
import time
import tracemalloc
import unittest

import gcs.bucket
import gcs.object
import testbench.common
import testbench.database
from google.storage.v2 import storage_pb2
from testbench.filestore import FileStore

MiB = 1024 * 1024
N_DEFAULT = 256 * MiB
BIG = os.environ.get("TESTBENCH_BOUNDS_4GB") == "1"
_BUCKET = "projects/_/buckets/bucket-bounds"


def rss_bytes():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r if sys.platform == "darwin" else r * 1024  # macOS bytes, Linux KiB


def _prng_chunks(total, size, seed=1234567):
    """A deterministic, cheaply-regenerable stream that NEVER materialises more
    than `size` bytes at once -- so the client side of the driver is itself
    bounded and only the code under test could blow the ceiling."""
    x = seed
    produced = 0
    while produced < total:
        n = min(size, total - produced)
        x = (1103515245 * x + 12345) & 0xFFFFFFFF
        yield (
            bytes((x >> (i % 24) & 0xFF) for i in range(min(n, 64))) * (n // 64 + 1)
        )[:n]
        produced += n


def _make_bucket(short):
    request = testbench.common.FakeRequest(args={}, data=json.dumps({"name": short}))
    bucket, _ = gcs.bucket.Bucket.init(request, None)
    return bucket


def _make_object_with_media(name, media):
    """A finalized (blob.upload is None) Object wrapping the given staging media,
    with size/crc32c/md5 recorded on the metadata so object_inserted persists a
    sidecar the restart hydration can trust WITHOUT re-reading the file. len /
    crc32c / md5 are O(1) on FileMedia -- no whole-buffer pass here."""
    metadata = storage_pb2.Object()
    metadata.name = name
    metadata.bucket = _BUCKET
    metadata.generation = 1
    metadata.size = len(media)
    metadata.checksums.crc32c = media.crc32c()
    metadata.checksums.md5_hash = media.md5()
    return gcs.object.Object(metadata, media, None)


def _reopen(root, short, name):
    """A cold restart: a fresh FileStore + Database hydrate the on-disk tree and
    hand back the object as a read-only FileMedia pointed at the finalized
    inode (from_existing opens an fd and reads nothing)."""
    db = testbench.database.Database.init(store=FileStore(root))
    return db.get_object(short, name)


class TestBoundedMemory(unittest.TestCase):
    def test_upload_then_download_is_bounded(self):
        total = 4 * 1024 * MiB if BIG else N_DEFAULT
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fs = FileStore(root)
        fs.bucket_inserted(_make_bucket("bucket-bounds"))

        base = rss_bytes()
        tracemalloc.start()
        try:
            # up: stream the object into O_APPEND staging one bounded chunk at a
            # time, then promote it with an O(1) os.replace (finalize).
            media = fs.new_upload_media(_BUCKET, "u")
            for chunk in _prng_chunks(total, 8 * MiB):
                media.append(chunk)
            blob = _make_object_with_media("big.bin", media)
            fs.object_inserted(_BUCKET, blob)

            # down: reopen (hydrate) and stream via chunks(), discarding -- a
            # whole-file read on either side would exceed the ceiling.
            hydrated = _reopen(root, "bucket-bounds", "big.bin").media
            seen = 0
            for c in hydrated.chunks(0, len(hydrated), 8 * MiB):
                seen += len(c)
            peak_py = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        self.assertEqual(total, seen)
        self.assertLess(
            rss_bytes() - base,
            256 * MiB,
            "up+down RSS delta exceeded 256 MiB (a whole-buffer materialiser survives)",
        )
        self.assertLess(peak_py, 256 * MiB)


class TestLinearTime(unittest.TestCase):
    def _elapsed(self, n):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        from testbench.filemedia import FileMedia

        dfd = os.open(root, os.O_RDONLY)
        try:
            m = FileMedia.new_staging(dfd, "t")
            chunk = b"a" * (4 * MiB)
            t0 = time.perf_counter()
            for _ in range(n):
                m.append(chunk)
            for _ in m.chunks(0, len(m), 4 * MiB):
                pass
            elapsed = time.perf_counter() - t0
            m.close()
            return elapsed
        finally:
            os.close(dfd)

    def test_append_read_is_linear(self):
        n = 32
        # O(n) append+read: doubling the work at most triples the wall time (a
        # generous ceiling absorbing timer noise). A quadratic re-copy per append
        # (the classic bytes-concat regression) would blow well past 3x.
        self.assertLess(self._elapsed(2 * n) / max(self._elapsed(n), 1e-6), 3.0)


if __name__ == "__main__":
    unittest.main()
