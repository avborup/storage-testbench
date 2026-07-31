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

"""Task-5 upload migration: uploads stream through an O_APPEND staging FileMedia
and finalize via os.replace, never materialising the whole object.

Driven IN-PROCESS against gcs.upload + FileStore + gcs.object via the real
gRPC WriteObject servicer, so RUSAGE_SELF/tracemalloc observe the process that
actually streams the bytes -- the gunicorn worker in the subprocess conformance
Emulator is invisible to RUSAGE_SELF (see tests/conformance/emulator.py). The
request_iterator is a lazy generator, so only one ~1 MiB chunk is live at a time
on the driver side; a materialising backend (BytesMedia fallback, or a to_bytes()
at the finalize site) is what makes peak RSS blow past the ceiling."""

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
from testbench.filemedia import FileMedia
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


class TestFileMediaUpload(unittest.TestCase):
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

    def test_resumable_roundtrip_and_bounded_memory(self):
        n_chunks = 512
        chunk = MiB
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
                            resource={"name": "big.bin", "bucket": _BUCKET},
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

        gc.collect()
        tracemalloc.start()
        base_rss = rss_bytes()
        response = self.grpc.WriteObject(requests(), context=self._mock_context())
        peak_rss = rss_bytes()
        _, tm_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # (a) the upload completed and produced object metadata
        self.assertIsNotNone(response)
        self.assertIsNotNone(response.resource)
        self.assertEqual("big.bin", response.resource.name)
        self.assertEqual(n_chunks * chunk, response.resource.size)

        # (b) bounded memory: the whole object was never materialised. A
        # BytesMedia accumulate (or a to_bytes() at finalize) would spike RSS by
        # ~512 MiB; streaming keeps the peak delta well under the ceiling.
        peak_delta = peak_rss - base_rss
        self.assertLess(
            peak_delta,
            256 * MiB,
            "peak RSS delta %d MiB exceeds ceiling (upload materialised)"
            % (peak_delta // MiB),
        )
        self.assertLess(tm_peak, 256 * MiB)

        # (c) the on-disk object is byte-exact: reopen the finalized destination
        # and recompute crc32c/md5 over the real file, comparing to the stream.
        expected_crc = acc["crc"]
        expected_md5 = acc["md5"].digest()
        self.assertEqual(expected_crc, response.resource.checksums.crc32c)
        self.assertEqual(expected_md5, response.resource.checksums.md5_hash)

        bucket_dir = os.open(os.path.join(self.root, "bucket-name"), os.O_RDONLY)
        try:
            read_back = FileMedia.from_path(bucket_dir, "big.bin")
        finally:
            os.close(bucket_dir)
        self.assertEqual(acc["size"], len(read_back))
        self.assertEqual(expected_crc, read_back.crc32c())
        self.assertEqual(expected_md5, read_back.md5())
        read_back.close()


if __name__ == "__main__":
    unittest.main()
