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

"""Task-9 rewrite/move/copy migration: RewriteObject streams each per-call
window ``src.media.chunks(offset, total, size)`` into a staging FileMedia
instead of slicing ``src.media[offset:total]`` into one buffer, and
MoveObject/objects_copy build a staging FileMedia and stream the whole source
through ``chunks()`` instead of ``src.media.to_bytes()`` / ``b"" += src.media``.

Driven IN-PROCESS against the real gRPC RewriteObject/MoveObject servicer and
the REST ``objects_rewrite``/``objects_copy`` Flask views (via
``rest_server.server.test_client()`` with ``rest_server.db`` swapped to a
FileStore-backed Database), so ``resource.getrusage``/``tracemalloc`` observe
the process that actually streams the bytes. A single source larger than the
256 MiB ceiling is rewritten/moved/copied with a per-call window that spans the
whole object; the pre-migration idiom materialises that window (or the whole
source) into one buffer, so peak memory scales with the object size -- streaming
keeps it flat at one ~2 MiB read chunk plus the O_APPEND staging file. The
destination crc32c/md5/size are read back off disk and must equal the source;
the per-call ``totalBytesRewritten``/``objectSize`` windows must match the exact
arithmetic the pre-refactor code produced."""

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
import testbench.error
import testbench.grpc_server
import testbench.rest_server
from google.storage.v2 import storage_pb2
from testbench.filemedia import FileMedia
from testbench.filestore import FileStore

MiB = 1024 * 1024
_BUCKET = "projects/_/buckets/bucket-name"
_BUCKET_NAME = "bucket-name"
_SRC_MIB = 300  # one source > the 256 MiB ceiling, so a materialised window overflows
_CEILING = 256 * MiB


def rss_bytes():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r if sys.platform == "darwin" else r * 1024  # macOS bytes, Linux KiB


def _chunk_for(i):
    # A deterministic, cheaply-regenerable 1 MiB chunk, distinct per index so a
    # dropped/duplicated/mis-ordered chunk changes the checksums.
    seed = b"%08d" % i
    return (seed * (MiB // len(seed) + 1))[:MiB]


class TestFileMediaRewrite(unittest.TestCase):
    @classmethod
    def _mock_context(cls):
        context = unittest.mock.Mock()
        context.invocation_metadata = unittest.mock.Mock(return_value=dict())
        return context

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="testbench-rewrite-")
        cls.fs = FileStore(cls.root)
        cls.db = testbench.database.Database.init(store=cls.fs)
        request = testbench.common.FakeRequest(
            args={}, data=json.dumps({"name": _BUCKET_NAME})
        )
        bucket, _ = gcs.bucket.Bucket.init(request, None)
        cls.db.insert_bucket(bucket, None)
        cls.grpc = testbench.grpc_server.StorageServicer(cls.db)

        # The shared read-only source used by rewrite/copy, and a dedicated
        # source for the destructive move test (identical content -> identical
        # expected checksums). Both exceed the 256 MiB ceiling.
        cls.source_name = "src-object"
        cls.move_source_name = "src-move"
        cls._upload_source(cls.source_name)
        crc, md5, size = cls._upload_source(cls.move_source_name)
        cls.expected_crc = crc
        cls.expected_md5 = md5
        cls.expected_size = size

    @classmethod
    def _upload_source(cls, name):
        def requests():
            offset = 0
            for i in range(_SRC_MIB):
                content = _chunk_for(i)
                finish = i == _SRC_MIB - 1
                spec = None
                if i == 0:
                    spec = storage_pb2.WriteObjectSpec(
                        resource={"name": name, "bucket": _BUCKET}
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

        exp_crc, exp_md5, exp_size = 0, hashlib.md5(), 0
        for i in range(_SRC_MIB):
            content = _chunk_for(i)
            exp_crc = crc32c.crc32c(content, exp_crc)
            exp_md5.update(content)
            exp_size += len(content)
        resp = cls.grpc.WriteObject(requests(), context=cls._mock_context())
        assert resp is not None and resp.resource.name == name
        return exp_crc, exp_md5.digest(), exp_size

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def _assert_dest_on_disk(self, dest_name):
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

    def _grpc_rewrite(self, dest_name, max_bytes):
        token, responses = "", []
        while True:
            req = storage_pb2.RewriteObjectRequest(
                destination_bucket=_BUCKET,
                destination_name=dest_name,
                source_bucket=_BUCKET,
                source_object=self.source_name,
                max_bytes_rewritten_per_call=max_bytes,
                rewrite_token=token,
            )
            resp = self.grpc.RewriteObject(req, context=self._mock_context())
            responses.append(resp)
            if resp.done:
                break
            token = resp.rewrite_token
        return responses

    def test_grpc_rewrite_single_call_streams_bounded(self):
        # A window that spans the whole source: the old src.media[0:size] slice
        # materialises the whole object; streaming keeps it flat.
        gc.collect()
        tracemalloc.start()
        base_rss = rss_bytes()
        responses = self._grpc_rewrite("dst-grpc-rewrite", self.expected_size + MiB)
        peak_rss = rss_bytes()
        _, tm_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertEqual(1, len(responses))
        self.assertTrue(responses[0].done)
        self.assertEqual(self.expected_size, responses[0].object_size)
        self.assertEqual(self.expected_size, responses[0].total_bytes_rewritten)
        self.assertEqual(self.expected_crc, responses[0].resource.checksums.crc32c)
        self.assertEqual(self.expected_md5, responses[0].resource.checksums.md5_hash)

        peak_delta = peak_rss - base_rss
        self.assertLess(
            peak_delta,
            _CEILING,
            "gRPC rewrite peak RSS delta %d MiB exceeds ceiling (materialised)"
            % (peak_delta // MiB),
        )
        self.assertLess(tm_peak, _CEILING)
        self._assert_dest_on_disk("dst-grpc-rewrite")

    def test_grpc_rewrite_windowed_totals_match_arithmetic(self):
        # Multi-call windows: totalBytesRewritten must step by exactly the
        # per-call maximum and objectSize must stay constant, matching the
        # pre-refactor slicing arithmetic; final checksums identical.
        per_call = 64 * MiB
        responses = self._grpc_rewrite("dst-grpc-windowed", per_call)
        expected = 0
        for i, resp in enumerate(responses):
            expected = min(expected + per_call, self.expected_size)
            self.assertEqual(self.expected_size, resp.object_size)
            self.assertEqual(expected, resp.total_bytes_rewritten)
            self.assertEqual(i == len(responses) - 1, resp.done)
        self.assertEqual(self.expected_size, expected)
        self._assert_dest_on_disk("dst-grpc-windowed")

    def test_grpc_move_streams_bounded(self):
        gc.collect()
        tracemalloc.start()
        base_rss = rss_bytes()
        req = storage_pb2.MoveObjectRequest(
            bucket=_BUCKET,
            source_object=self.move_source_name,
            destination_object="dst-grpc-move",
        )
        metadata = self.grpc.MoveObject(req, context=self._mock_context())
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
            "gRPC move peak RSS delta %d MiB exceeds ceiling (materialised)"
            % (peak_delta // MiB),
        )
        self.assertLess(tm_peak, _CEILING)
        self._assert_dest_on_disk("dst-grpc-move")
        # Move is copy + delete: the source no longer resolves as a live object.
        with self.assertRaises(testbench.error.RestException):
            self.db.get_object(_BUCKET, self.move_source_name, context=None)

    def _rest_client(self):
        prev_db = testbench.rest_server.db
        testbench.rest_server.db = self.db
        self.addCleanup(setattr, testbench.rest_server, "db", prev_db)
        return testbench.rest_server.server.test_client()

    def test_rest_rewrite_streams_bounded(self):
        client = self._rest_client()
        gc.collect()
        tracemalloc.start()
        base_rss = rss_bytes()
        token, done, last = None, False, None
        while not done:
            url = (
                "/storage/v1/b/%s/o/%s/rewriteTo/b/%s/o/%s?maxBytesRewrittenPerCall=%d"
                % (
                    _BUCKET_NAME,
                    self.source_name,
                    _BUCKET_NAME,
                    "dst-rest-rewrite",
                    self.expected_size + MiB,
                )
            )
            if token is not None:
                url += "&rewriteToken=%s" % token
            response = client.post(url)
            self.assertEqual(200, response.status_code)
            last = json.loads(response.data)
            done = last["done"]
            token = last.get("rewriteToken")
        peak_rss = rss_bytes()
        _, tm_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertEqual(str(self.expected_size), last["objectSize"])
        self.assertEqual(str(self.expected_size), last["totalBytesRewritten"])

        peak_delta = peak_rss - base_rss
        self.assertLess(
            peak_delta,
            _CEILING,
            "REST rewrite peak RSS delta %d MiB exceeds ceiling (materialised)"
            % (peak_delta // MiB),
        )
        self.assertLess(tm_peak, _CEILING)
        self._assert_dest_on_disk("dst-rest-rewrite")

    def test_rest_copy_streams_bounded(self):
        client = self._rest_client()
        gc.collect()
        tracemalloc.start()
        base_rss = rss_bytes()
        response = client.post(
            "/storage/v1/b/%s/o/%s/copyTo/b/%s/o/%s"
            % (_BUCKET_NAME, self.source_name, _BUCKET_NAME, "dst-rest-copy")
        )
        peak_rss = rss_bytes()
        _, tm_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertEqual(200, response.status_code)
        body = json.loads(response.data)
        self.assertEqual(str(self.expected_size), body["size"])

        peak_delta = peak_rss - base_rss
        self.assertLess(
            peak_delta,
            _CEILING,
            "REST copy peak RSS delta %d MiB exceeds ceiling (materialised)"
            % (peak_delta // MiB),
        )
        self.assertLess(tm_peak, _CEILING)
        self._assert_dest_on_disk("dst-rest-copy")


if __name__ == "__main__":
    unittest.main()
