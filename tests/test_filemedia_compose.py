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

"""Task-8 compose migration: ComposeObject streams each source into a staging
FileMedia via chunks()+append() instead of materialising every source
(``.to_bytes()`` on gRPC, ``b"" += media`` on REST) into one in-memory buffer.

Driven IN-PROCESS against the real gRPC ComposeObject servicer and the REST
``objects_compose`` Flask view (via ``rest_server.server.test_client()`` with
``rest_server.db`` swapped to a FileStore-backed Database), so
``resource.getrusage``/``tracemalloc`` observe the process that actually streams
the bytes. N sources of ~64 MiB each are composed; the pre-migration idiom
accumulates every source into one buffer (N*64 MiB), so peak memory scales with
the composed size -- streaming keeps it flat at one ~2 MiB read chunk plus the
O_APPEND staging file. Result crc32c/md5/size are read back off disk and must
equal the ordered concatenation of the sources."""

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
import testbench.rest_server
from google.storage.v2 import storage_pb2
from testbench.filemedia import FileMedia
from testbench.filestore import FileStore

MiB = 1024 * 1024
_BUCKET = "projects/_/buckets/bucket-name"
_BUCKET_NAME = "bucket-name"
_N_SOURCES = 8
_SRC_MIB = 64  # per source; N*_SRC_MIB = 512 MiB total, well over the 256 MiB ceiling
_CEILING = 256 * MiB


def rss_bytes():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r if sys.platform == "darwin" else r * 1024  # macOS bytes, Linux KiB


def _chunk_for(src, i):
    # A deterministic, cheaply-regenerable 1 MiB chunk, distinct per (source,
    # index) so a dropped/duplicated/mis-ordered chunk changes the checksums.
    seed = b"%03d-%08d" % (src, i)
    return (seed * (MiB // len(seed) + 1))[:MiB]


class TestFileMediaCompose(unittest.TestCase):
    @classmethod
    def _mock_context(cls):
        context = unittest.mock.Mock()
        context.invocation_metadata = unittest.mock.Mock(return_value=dict())
        return context

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="testbench-compose-")
        cls.fs = FileStore(cls.root)
        cls.db = testbench.database.Database.init(store=cls.fs)
        request = testbench.common.FakeRequest(
            args={}, data=json.dumps({"name": _BUCKET_NAME})
        )
        bucket, _ = gcs.bucket.Bucket.init(request, None)
        cls.db.insert_bucket(bucket, None)
        cls.grpc = testbench.grpc_server.StorageServicer(cls.db)

        # Upload N sources by streaming; accumulate the expected composed
        # checksums over the ordered concatenation of every source's bytes.
        cls.source_names = ["src-%d" % s for s in range(_N_SOURCES)]
        exp_crc, exp_md5, exp_size = 0, hashlib.md5(), 0
        for s in range(_N_SOURCES):

            def requests(s=s):
                offset = 0
                for i in range(_SRC_MIB):
                    content = _chunk_for(s, i)
                    finish = i == _SRC_MIB - 1
                    spec = None
                    if i == 0:
                        spec = storage_pb2.WriteObjectSpec(
                            resource={"name": cls.source_names[s], "bucket": _BUCKET}
                        )
                    req = storage_pb2.WriteObjectRequest(
                        write_offset=offset,
                        checksummed_data=storage_pb2.ChecksummedData(
                            content=content, crc32c=crc32c.crc32c(content)
                        ),
                        finish_write=finish,
                    )
                    if spec is not None:
                        req.write_object_spec.CopyFrom(spec)
                    offset += len(content)
                    yield req

            for i in range(_SRC_MIB):
                content = _chunk_for(s, i)
                exp_crc = crc32c.crc32c(content, exp_crc)
                exp_md5.update(content)
                exp_size += len(content)
            resp = cls.grpc.WriteObject(requests(), context=cls._mock_context())
            assert resp is not None and resp.resource.name == cls.source_names[s]
        cls.expected_crc = exp_crc
        cls.expected_md5 = exp_md5.digest()
        cls.expected_size = exp_size

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def _assert_composed_on_disk(self, dest_name):
        bucket_dir = os.open(os.path.join(self.root, _BUCKET_NAME), os.O_RDONLY)
        try:
            read_back = FileMedia.from_path(bucket_dir, dest_name)
        finally:
            os.close(bucket_dir)
        try:
            self.assertEqual(self.expected_size, len(read_back))
            self.assertEqual(self.expected_crc, read_back.crc32c())
            self.assertEqual(self.expected_md5, read_back.md5())
        finally:
            read_back.close()

    def test_grpc_compose_streams_bounded(self):
        request = storage_pb2.ComposeObjectRequest(
            destination=storage_pb2.Object(name="dst-grpc", bucket=_BUCKET),
            source_objects=[
                storage_pb2.ComposeObjectRequest.SourceObject(name=n)
                for n in self.source_names
            ],
        )
        gc.collect()
        tracemalloc.start()
        base_rss = rss_bytes()
        metadata = self.grpc.ComposeObject(request, context=self._mock_context())
        peak_rss = rss_bytes()
        _, tm_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertIsNotNone(metadata)
        self.assertEqual(self.expected_size, metadata.size)
        self.assertEqual(self.expected_crc, metadata.checksums.crc32c)
        self.assertEqual(self.expected_md5, metadata.checksums.md5_hash)

        peak_delta = peak_rss - base_rss
        self.assertLess(
            peak_delta,
            _CEILING,
            "gRPC compose peak RSS delta %d MiB exceeds ceiling (materialised)"
            % (peak_delta // MiB),
        )
        self.assertLess(tm_peak, _CEILING)
        self._assert_composed_on_disk("dst-grpc")

    def test_rest_compose_streams_bounded(self):
        prev_db = testbench.rest_server.db
        testbench.rest_server.db = self.db
        self.addCleanup(setattr, testbench.rest_server, "db", prev_db)
        client = testbench.rest_server.server.test_client()

        payload = {
            "sourceObjects": [{"name": n} for n in self.source_names],
            "destination": {"contentType": "application/octet-stream"},
        }
        gc.collect()
        tracemalloc.start()
        base_rss = rss_bytes()
        response = client.post(
            "/storage/v1/b/%s/o/%s/compose" % (_BUCKET_NAME, "dst-rest"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        peak_rss = rss_bytes()
        _, tm_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertEqual(200, response.status_code)
        body = json.loads(response.data)
        self.assertEqual(self.expected_size, int(body["size"]))

        peak_delta = peak_rss - base_rss
        self.assertLess(
            peak_delta,
            _CEILING,
            "REST compose peak RSS delta %d MiB exceeds ceiling (materialised)"
            % (peak_delta // MiB),
        )
        self.assertLess(tm_peak, _CEILING)
        self._assert_composed_on_disk("dst-rest")


if __name__ == "__main__":
    unittest.main()
