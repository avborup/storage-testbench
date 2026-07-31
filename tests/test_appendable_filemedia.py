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

"""Trace-UNCOVERED F2 appendable round-trip on the FILE backend.

No conformance trace exercises appendable uploads, so B == C cannot guard this
path -- this dedicated test is the safety net. It drives a gRPC BidiWriteObject
appendable upload across >=2 checkpoints + finalize against a FileStore-backed
Database, then asserts the destination streamed to a single on-disk inode
(bytes + crc32c + md5), the staging file was sealed away, and a restart
(fresh FileStore + rebuild_index) hydrates the finalized object identically."""

import hashlib
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

import crc32c as crc32c_mod
import grpc

import gcs.bucket
import testbench.common
import testbench.database
import testbench.grpc_server
from google.storage.v2 import storage_pb2
from testbench.filemedia import FileMedia
from testbench.filestore import FileStore

_BUCKET = "projects/_/buckets/bucket-name"
_QUANTUM = 256 * 1024


def _block(desired_bytes):
    line = "A" * 127 + "\n"
    return (int(desired_bytes / len(line)) * line).encode("utf-8")


class TestAppendableFileMedia(unittest.TestCase):
    def _mock_context(self):
        context = unittest.mock.Mock()
        context.invocation_metadata = unittest.mock.Mock(return_value=dict())
        context.abort.side_effect = grpc.RpcError()
        return context

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.db = testbench.database.Database.init(store=FileStore(self.root))
        request = testbench.common.FakeRequest(
            args={}, data=json.dumps({"name": "bucket-name"})
        )
        bucket, _ = gcs.bucket.Bucket.init(request, None)
        self.db.insert_bucket(bucket, None)
        self.grpc = testbench.grpc_server.StorageServicer(self.db)

    def _uploads_dir(self):
        return os.path.join(self.root, "bucket-name", ".gcs", "uploads")

    def _dest(self):
        return os.path.join(self.root, "bucket-name", "object-name")

    def test_appendable_round_trip_and_restart(self):
        context = self._mock_context()
        media = _block(2 * _QUANTUM + _QUANTUM // 2)

        # --- first stream: TWO intermediate checkpoints (no finalize) --------
        c1 = media[0:_QUANTUM]
        r1 = storage_pb2.BidiWriteObjectRequest(
            write_object_spec=storage_pb2.WriteObjectSpec(
                resource=storage_pb2.Object(name="object-name", bucket=_BUCKET),
                appendable=True,
            ),
            write_offset=0,
            checksummed_data=storage_pb2.ChecksummedData(
                content=c1, crc32c=crc32c_mod.crc32c(c1)
            ),
            flush=True,
        )
        c2 = media[_QUANTUM : 2 * _QUANTUM]
        r2 = storage_pb2.BidiWriteObjectRequest(
            write_offset=_QUANTUM,
            checksummed_data=storage_pb2.ChecksummedData(
                content=c2, crc32c=crc32c_mod.crc32c(c2)
            ),
            flush=True,
            state_lookup=True,
        )
        responses = list(self.grpc.BidiWriteObject([r1, r2], context=context))
        self.assertEqual(2, len(responses))
        self.assertEqual(_QUANTUM, responses[0].resource.size)
        self.assertEqual(2 * _QUANTUM, responses[1].persisted_size)
        generation = responses[0].resource.generation
        write_handle = responses[0].write_handle

        # Bytes are already live at the destination via the shared inode after
        # each checkpoint (no os.replace per checkpoint would have closed the fd).
        with open(self._dest(), "rb") as handle:
            self.assertEqual(media[0 : 2 * _QUANTUM], handle.read())

        # --- second stream: append the tail, then FINALIZE ------------------
        c3 = media[2 * _QUANTUM :]
        r3 = storage_pb2.BidiWriteObjectRequest(
            append_object_spec=storage_pb2.AppendObjectSpec(
                bucket=_BUCKET,
                object="object-name",
                generation=generation,
                write_handle=write_handle,
            ),
            write_offset=2 * _QUANTUM,
            checksummed_data=storage_pb2.ChecksummedData(
                content=c3, crc32c=crc32c_mod.crc32c(c3)
            ),
            finish_write=True,
        )
        responses = list(self.grpc.BidiWriteObject([r3], context=context))
        self.assertTrue(responses[-1].resource.HasField("finalize_time"))

        # --- post-finalize on-disk + media assertions ----------------------
        blob = self.db.get_object("bucket-name", "object-name")
        self.assertIsInstance(blob.media, FileMedia)
        self.assertTrue(blob.media.is_finalized)  # seal ran exactly once
        self.assertEqual(len(media), len(blob.media))
        self.assertEqual(media, blob.media.to_bytes())
        self.assertEqual(crc32c_mod.crc32c(media), blob.media.crc32c())
        self.assertEqual(hashlib.md5(media).digest(), blob.media.md5())
        # seal unlinked the staging NAME; the destination hardlink survives.
        self.assertEqual([], os.listdir(self._uploads_dir()))
        with open(self._dest(), "rb") as handle:
            self.assertEqual(media, handle.read())

        # --- restart: fresh FileStore + rebuild_index hydrates identically --
        db2 = testbench.database.Database.init(store=FileStore(self.root))
        blob2 = db2.get_object("bucket-name", "object-name")
        self.assertEqual(media, blob2.media.to_bytes())
        self.assertEqual(crc32c_mod.crc32c(media), blob2.media.crc32c())
        self.assertEqual(hashlib.md5(media).digest(), blob2.media.md5())


if __name__ == "__main__":
    unittest.main()
