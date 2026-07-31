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

"""Task-11 restart hydration: a fresh FileStore.rebuild_index hydrates each live
object as a read-only FileMedia.from_existing pointed at the on-disk media,
trusting the sidecar's persisted size/crc32c/md5 -- so restarting over a
multi-GB tree never materialises a whole object.

Driven IN-PROCESS (the rebuild runs in this process), so RUSAGE_SELF/tracemalloc
observe the process that actually hydrates. The large object is streamed onto
disk through the real gRPC WriteObject servicer (a lazy request generator, so
the WRITE peak is already bounded); the interesting measurement is the RSS delta
across the subsequent cold rebuild. A whole-file `_read_media` read would make
the object resident (~n_chunks MiB) and blow the ceiling; from_existing opens an
fd and reads nothing."""

import gc
import hashlib
import json
import os
import resource
import shutil
import sys
import tempfile
import tracemalloc
import unittest
import unittest.mock

import crc32c

import gcs.bucket
import testbench.common
import testbench.database
import testbench.grpc_server
from google.storage.v2 import storage_pb2
from testbench.filestore import FileStore

MiB = 1024 * 1024
_BUCKET = "projects/_/buckets/bucket-name"


def rss_bytes():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r if sys.platform == "darwin" else r * 1024  # macOS bytes, Linux KiB


def _chunk_for(i, size):
    # A deterministic, cheaply-regenerable 1 MiB chunk (distinct per index so a
    # dropped/duplicated chunk changes the checksums).
    seed = b"%08d" % i
    return (seed * (size // len(seed) + 1))[:size]


class TestFileMediaRestart(unittest.TestCase):
    def _mock_context(self):
        context = unittest.mock.Mock()
        context.invocation_metadata = unittest.mock.Mock(return_value=dict())
        return context

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.fs = FileStore(self.root)
        self.db = testbench.database.Database.init(store=self.fs)
        request = testbench.common.FakeRequest(
            args={}, data=json.dumps({"name": "bucket-name"})
        )
        bucket, _ = gcs.bucket.Bucket.init(request, None)
        self.db.insert_bucket(bucket, None)
        self.grpc = testbench.grpc_server.StorageServicer(self.db)

    def _stream_upload(self, name, n_chunks, chunk):
        acc = {"crc": 0, "md5": hashlib.md5(), "size": 0}

        def requests():
            offset = 0
            for i in range(n_chunks):
                content = _chunk_for(i, chunk)
                acc["crc"] = crc32c.crc32c(content, acc["crc"])
                acc["md5"].update(content)
                acc["size"] += len(content)
                finish = i == n_chunks - 1
                if i == 0:
                    req = storage_pb2.WriteObjectRequest(
                        write_object_spec=storage_pb2.WriteObjectSpec(
                            resource={"name": name, "bucket": _BUCKET},
                        ),
                        write_offset=0,
                        checksummed_data=storage_pb2.ChecksummedData(
                            content=content, crc32c=crc32c.crc32c(content)
                        ),
                        finish_write=finish,
                    )
                else:
                    req = storage_pb2.WriteObjectRequest(
                        write_offset=offset,
                        checksummed_data=storage_pb2.ChecksummedData(
                            content=content, crc32c=crc32c.crc32c(content)
                        ),
                        finish_write=finish,
                    )
                offset += len(content)
                yield req

        response = self.grpc.WriteObject(requests(), context=self._mock_context())
        return acc, response

    def test_restart_hydrates_large_object_bounded_memory(self):
        n_chunks = 512
        chunk = MiB
        acc, response = self._stream_upload("big.bin", n_chunks, chunk)
        self.assertEqual(n_chunks * chunk, response.resource.size)
        expected_crc = acc["crc"]
        expected_md5 = acc["md5"].digest()
        size = acc["size"]

        # Drop the live Database (and its FileStore + finalized FileMedia fds) so
        # the rebuild runs against a cold tree, exactly as a process restart does.
        del self.db
        del self.grpc
        del self.fs
        gc.collect()

        tracemalloc.start()
        base_rss = rss_bytes()
        db2 = testbench.database.Database.init(store=FileStore(self.root))  # rebuild
        peak_rss = rss_bytes()
        _, tm_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # (a) bounded memory: a whole-file _read_media would make the object
        # resident (~512 MiB); from_existing opens an fd and trusts the persisted
        # checksums, so the rebuild RSS delta stays flat.
        peak_delta = peak_rss - base_rss
        self.assertLess(
            peak_delta,
            256 * MiB,
            "hydration RSS delta %d MiB exceeds ceiling (rebuild materialised)"
            % (peak_delta // MiB),
        )
        self.assertLess(tm_peak, 256 * MiB)

        # (b) reads back byte-identically: stream the hydrated media off disk and
        # recompute crc32c/md5 over the REAL bytes (bounded per-chunk), proving
        # on-disk integrity rather than echoing the trusted proto.
        blob2 = db2.get_object("bucket-name", "big.bin")
        self.assertEqual(size, len(blob2.media))
        rolling_crc = 0
        rolling_md5 = hashlib.md5()
        read = 0
        for piece in blob2.media.chunks(0, len(blob2.media), MiB):
            rolling_crc = crc32c.crc32c(piece, rolling_crc)
            rolling_md5.update(piece)
            read += len(piece)
        self.assertEqual(size, read)
        self.assertEqual(expected_crc, rolling_crc)
        self.assertEqual(expected_md5, rolling_md5.digest())

        # ... and the hydrated media reports the persisted checksums too.
        self.assertEqual(expected_crc, blob2.media.crc32c())
        self.assertEqual(expected_md5, blob2.media.md5())


if __name__ == "__main__":
    unittest.main()
