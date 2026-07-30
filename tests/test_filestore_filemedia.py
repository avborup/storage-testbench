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

"""Direct-drive tests for FileStore's FileMedia staging factory and the
object_inserted finalize/link_into branches. These are the only exercise of the
FileMedia server path in Task 4 -- no producer builds a FileMedia yet, so the
conformance trace stays on the BytesMedia fallback (B == C)."""

import os
import shutil
import tempfile
import unittest

import gcs.object
import testbench.common
from testbench.filemedia import FileMedia
from testbench.filestore import FileStore
from tests.test_store import _make_bucket, _make_object

_BUCKET = "projects/_/buckets/bucket-name"


def _make_object_with_media(bucket, name, fm, appendable=False):
    # Build an Object around a pre-constructed (FileMedia) media. init_dict's
    # widened guard (Task 1) passes a Media through by identity, so `fm` reaches
    # blob.media unwrapped. `appendable=True` marks an in-progress appendable
    # insert: blob.upload is the sentinel object_inserted branches on (mirrors
    # upload.py's _insert_empty_appendable_object, which passes upload=upload).
    request = testbench.common.FakeRequest(args={}, headers={}, environ={})
    blob, _ = gcs.object.Object.init_dict(
        request, {"name": name}, fm, bucket.metadata, False
    )
    if appendable:
        blob.upload = object()
    return blob


class TestFileStoreFinalize(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.fs = FileStore(self.root)
        self.bucket = _make_bucket("bucket-name")
        self.fs.bucket_inserted(self.bucket)

    def _uploads(self):
        return os.path.join(self.root, "bucket-name", ".gcs", "uploads")

    def test_new_upload_media_stages_under_gcs_uploads(self):
        m = self.fs.new_upload_media(_BUCKET, "up-1")
        self.assertIsInstance(m, FileMedia)
        self.assertTrue(os.path.exists(os.path.join(self._uploads(), "up-1")))

    def test_object_inserted_promotes_finalized_filemedia_without_double_write(self):
        m = self.fs.new_upload_media(_BUCKET, "up-2")
        m.append(b"hello world")
        blob = _make_object_with_media(self.bucket, "audio/clip.wav", m)
        self.fs.object_inserted(_BUCKET, blob)
        media = os.path.join(self.root, "bucket-name", "audio", "clip.wav")
        with open(media, "rb") as handle:
            self.assertEqual(b"hello world", handle.read())
        self.assertTrue(os.path.exists(media + ".gcsmeta"))
        # staging consumed by os.replace (no double-write, no leftover staging)
        self.assertEqual([], os.listdir(self._uploads()))

    def test_object_inserted_links_unfinalized_appendable(self):
        m = self.fs.new_upload_media(_BUCKET, "up-3")
        blob = _make_object_with_media(self.bucket, "live.dat", m, appendable=True)
        self.fs.object_inserted(_BUCKET, blob)
        dest = os.path.join(self.root, "bucket-name", "live.dat")
        self.assertTrue(os.path.exists(dest))  # destination linked (0 bytes)
        m.append(b"grow")  # append AFTER link flows to the shared inode
        with open(dest, "rb") as handle:
            self.assertEqual(b"grow", handle.read())
        self.assertFalse(m.is_finalized)

    def test_object_inserted_bytesmedia_still_uses_fallback(self):
        blob = _make_object(self.bucket, "b.txt", media=b"x")  # BytesMedia
        self.fs.object_inserted(_BUCKET, blob)
        with open(os.path.join(self.root, "bucket-name", "b.txt"), "rb") as handle:
            self.assertEqual(b"x", handle.read())

    def test_delete_upload_removes_staging(self):
        self.fs.new_upload_media(_BUCKET, "up-x")
        self.fs.delete_upload(_BUCKET, "up-x")
        self.assertEqual([], os.listdir(self._uploads()))


if __name__ == "__main__":
    unittest.main()
