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

"""Task-12 fault-path + staging-cleanup coverage for the FileMedia backend.

The conformance harness (B == C) is BLIND to every path here: fault-injection
responses are stripped by the masked overlay, and staging lifetime is an on-disk
implementation detail with no wire shape. These are the dedicated backstops for:

  (a) return-broken-stream on a download -- the generic retry wrapper
      materialises the FileMedia response body (fault-only get_data()) and
      aborts mid-stream;
  (b) inject-upload-data-error -> corrupt_media over a FileMedia in Object.init
      -- the documented fault-only small-buffer materialiser (object.py:144),
      which then re-wraps the corrupted bytes as a BytesMedia;
  (c) return-503-after-256K retry-success on a ranged download -- the success
      branch hands flask.Response GENUINE bytes materialised via the widened
      `isinstance(_, Media)` guard (object.py:576/:684), so a FileMedia serves a
      200 rather than crashing flask;
  (d) resumable finalize on the `bytes */N` branch (rest_server.py:1197);
  (e) non-Content-Range simple-completion of a resumable upload
      (rest_server.py:1315);

plus staging-leak cleanup on BOTH abort paths -- a cancelled upload
(delete_upload) and an abandoned rewrite token (delete_rewrite) each leave no
file under .gcs/uploads.

The REST-client tests run under BOTH conftest legs: on `TESTBENCH_TEST_STORE=file`
the autouse fixture swaps the live singleton store to a FileStore, so resumable
uploads/downloads exercise a real FileMedia; on memory they exercise BytesMedia.
Both must hold. The cleanup tests construct their own FileStore, so they assert
the file-backend behaviour on either leg.
"""

import json
import os
import re
import shutil
import tempfile
import types
import unittest

import gcs.bucket
import gcs.object
import testbench.common
import testbench.database
import testbench.error
import testbench.filemedia
from google.storage.v2 import storage_pb2
from testbench import rest_server
from testbench.filestore import FileStore
from testbench.media import BytesMedia

_BUCKET = "projects/_/buckets/bucket-name"


class TestFileMediaFaults(unittest.TestCase):
    def setUp(self):
        rest_server.db.clear()
        self.client = rest_server.server.test_client()
        os.environ.pop("GOOGLE_CLOUD_CPP_STORAGE_TEST_BUCKET_NAME", None)
        response = self.client.post(
            "/storage/v1/b", data=json.dumps({"name": "bucket-name"})
        )
        self.assertEqual(response.status_code, 200, msg=response.data)
        # A dedicated bucket object for the direct Object.init fault test.
        request = testbench.common.FakeRequest(
            args={}, data=json.dumps({"name": "bucket-name"})
        )
        self.bucket, _ = gcs.bucket.Bucket.init(request, None)

    # --- helpers ----------------------------------------------------------
    def _resumable_put(self, name, data, finalize_content_range=True):
        """Stream `data` into a resumable upload so that, on the file leg,
        upload.media is a real O_APPEND FileMedia promoted at finalize. Returns
        the finalize response."""
        response = self.client.post(
            "/upload/storage/v1/b/bucket-name/o",
            query_string={"uploadType": "resumable", "name": name},
            content_type="application/json",
            data=json.dumps({"name": name}),
        )
        self.assertEqual(response.status_code, 200, msg=response.data)
        match = re.search("[&?]upload_id=([^&]+)", response.headers.get("location"))
        self.assertIsNotNone(match)
        upload_id = match.group(1)
        headers = {}
        if finalize_content_range:
            headers["content-range"] = "bytes 0-%d/%d" % (len(data) - 1, len(data))
        response = self.client.put(
            "/upload/storage/v1/b/bucket-name/o",
            query_string={"upload_id": upload_id},
            headers=headers,
            data=data,
        )
        return upload_id, response

    def _staging_filemedia(self, data):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        dfd = os.open(root, os.O_RDONLY)
        try:
            fm = testbench.filemedia.FileMedia.new_staging(dfd, "s")
            fm.append(data)
            self.addCleanup(fm.close)
            return fm
        finally:
            os.close(dfd)

    # --- (a) broken-stream on a download ----------------------------------
    def test_return_broken_stream_download(self):
        _, response = self._resumable_put("broken.bin", b"A" * 4096)
        self.assertEqual(response.status_code, 200, msg=response.data)
        response = self.client.post(
            "/retry_test",
            data=json.dumps(
                {"instructions": {"storage.objects.get": ["return-broken-stream"]}}
            ),
        )
        self.assertEqual(response.status_code, 200)
        test_id = json.loads(response.data)["id"]
        response = self.client.get(
            "/storage/v1/b/bucket-name/o/broken.bin",
            query_string={"alt": "media"},
            headers={"x-retry-test-id": test_id},
        )
        # Reading the streamed FileMedia body materialises it (fault-only
        # get_data()) then aborts after the first few bytes.
        with self.assertRaises(testbench.error.RestException) as ex:
            _ = len(response.data)
        self.assertIn("broken stream", ex.exception.msg)

    # --- (b) inject-upload-data-error -> corrupt_media over a FileMedia ----
    def test_corrupt_media_over_filemedia(self):
        # Direct: corrupt_media reads a FileMedia by slice and returns a small
        # bytes buffer (first byte A<->B flipped).
        fm = self._staging_filemedia(b"ABCDEFGH")
        corrupted = testbench.common.corrupt_media(fm)
        self.assertEqual(bytes(corrupted), b"BBCDEFGH")

        # In Object.init: the inject-upload-data-error fault path materialises
        # and RE-WRAPS the corrupted bytes as a BytesMedia (never leaves a
        # FileMedia carrying phantom checksums).
        request = testbench.common.FakeRequest(
            args={},
            headers={"x-goog-testbench-instructions": "inject-upload-data-error"},
            environ={},
        )
        metadata = storage_pb2.Object()
        metadata.name = "corrupt-obj"
        fm2 = self._staging_filemedia(b"ABCDEFGH")
        blob, _ = gcs.object.Object.init(
            request, metadata, fm2, self.bucket.metadata, False, None, csek=False
        )
        self.assertNotIsInstance(blob.media, testbench.filemedia.FileMedia)
        self.assertIsInstance(blob.media, BytesMedia)
        self.assertEqual(blob.media.to_bytes(), b"BBCDEFGH")
        self.assertEqual(blob.metadata.size, 8)

    # --- (c) return-503-after-256K retry-success on a ranged download ------
    def test_return_503_after_256k_retry_success_serves_bytes(self):
        data = b"Z" * (300 * 1024)
        _, response = self._resumable_put("obj503.bin", data)
        self.assertEqual(response.status_code, 200, msg=response.data)
        # begin != 0 (a Range request) + no /retry-N suffix -> the success
        # branch: flask.Response(response_payload, 200) where response_payload
        # was materialised from the (File)Media via the widened Media guard.
        response = self.client.get(
            "/storage/v1/b/bucket-name/o/obj503.bin",
            query_string={"alt": "media"},
            headers={
                "x-goog-emulator-instructions": "return-503-after-256K",
                "range": "bytes=1-",
            },
        )
        self.assertEqual(response.status_code, 200, msg=response.data)
        # Genuine bytes reached flask (a raw FileMedia would have raised).
        self.assertEqual(response.data, data)

    # --- (d) resumable finalize on the `bytes */N` branch -----------------
    def test_resumable_finalize_star_slash_n(self):
        data = b"finalize-via-star-slash-N" * 100
        response = self.client.post(
            "/upload/storage/v1/b/bucket-name/o",
            query_string={"uploadType": "resumable", "name": "starN.bin"},
            content_type="application/json",
            data=json.dumps({"name": "starN.bin"}),
        )
        self.assertEqual(response.status_code, 200)
        upload_id = re.search(
            "[&?]upload_id=([^&]+)", response.headers.get("location")
        ).group(1)
        # Upload the bytes with an OPEN total ("/*") so the upload is not yet
        # complete...
        response = self.client.put(
            "/upload/storage/v1/b/bucket-name/o",
            query_string={"upload_id": upload_id},
            headers={"content-range": "bytes 0-%d/*" % (len(data) - 1)},
            data=data,
        )
        self.assertEqual(response.status_code, 308, msg=response.data)
        # ...then finalize via the `bytes */N` branch (rest_server.py:1197).
        response = self.client.put(
            "/upload/storage/v1/b/bucket-name/o",
            query_string={"upload_id": upload_id},
            headers={"content-range": "bytes */%d" % len(data)},
        )
        self.assertEqual(response.status_code, 200, msg=response.data)
        self.assertEqual(int(json.loads(response.data)["size"]), len(data))
        response = self.client.get(
            "/storage/v1/b/bucket-name/o/starN.bin", query_string={"alt": "media"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, data)

    # --- (e) non-Content-Range simple-completion of a resumable upload ----
    def test_resumable_finalize_no_content_range(self):
        data = b"no-content-range-completion" * 50
        _, response = self._resumable_put(
            "nocr.bin", data, finalize_content_range=False
        )
        self.assertEqual(response.status_code, 200, msg=response.data)
        self.assertEqual(int(json.loads(response.data)["size"]), len(data))
        response = self.client.get(
            "/storage/v1/b/bucket-name/o/nocr.bin", query_string={"alt": "media"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, data)


class TestStagingCleanup(unittest.TestCase):
    """Staging must not leak on abort. Constructs its own FileStore so it asserts
    the file-backend behaviour regardless of the conftest leg."""

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

    def _staging_path(self, name):
        return os.path.join(self.root, "bucket-name", ".gcs", "uploads", name)

    def test_cancelled_upload_leaves_no_staging_file(self):
        media = self.fs.new_upload_media(_BUCKET, "u-cancel")
        self.assertTrue(os.path.exists(self._staging_path("u-cancel")))
        upload = types.SimpleNamespace(
            upload_id="u-cancel",
            bucket=types.SimpleNamespace(name=_BUCKET),
            media=media,
        )
        self.db.insert_upload(upload)
        # CancelResumableWrite / DELETE resumable / abort all route here.
        self.db.delete_upload("u-cancel", None)
        self.assertFalse(
            os.path.exists(self._staging_path("u-cancel")),
            "cancelled upload leaked its staging file",
        )

    def test_abandoned_rewrite_leaves_no_staging_file(self):
        media = self.fs.new_staging_media("bucket-name", "tok-abandon")
        self.assertTrue(os.path.exists(self._staging_path("tok-abandon")))
        rewrite = types.SimpleNamespace(
            token="tok-abandon",
            dst_bucket_name="bucket-name",
            media=media,
        )
        self.db.insert_rewrite(rewrite)
        # A dropped/expired multi-call rewrite that never reached the terminal
        # `done` (which would have consumed the staging name via os.replace).
        self.db.delete_rewrite("tok-abandon", None)
        self.assertFalse(
            os.path.exists(self._staging_path("tok-abandon")),
            "abandoned rewrite leaked its staging file",
        )

    def _appendable_blob(self, name, media):
        metadata = storage_pb2.Object()
        metadata.name = name
        metadata.bucket = _BUCKET
        metadata.generation = 1
        metadata.size = len(media)
        metadata.checksums.crc32c = media.crc32c()
        metadata.checksums.md5_hash = media.md5()
        # A truthy `upload` marks this an in-progress appendable insert, so
        # object_inserted takes the link_into branch (not finalize).
        return gcs.object.Object(metadata, media, None, upload=object())

    def test_appendable_reinsert_same_name_replaces(self):
        # A fresh appendable insert of an object-name that is ALREADY live (the
        # gRPC BidiWrite redirect flow does exactly this) must replace the prior
        # generation, matching the memory backend -- NOT raise FileExistsError
        # from os.link onto an existing destination.
        m1 = self.fs.new_upload_media(_BUCKET, "reins-1")
        m1.append(b"first-generation")
        self.fs.object_inserted(_BUCKET, self._appendable_blob("object-name", m1))
        dest = os.path.join(self.root, "bucket-name", "object-name")
        self.assertTrue(os.path.exists(dest))

        m2 = self.fs.new_upload_media(_BUCKET, "reins-2")
        m2.append(b"second-generation-content")
        # Must not raise.
        self.fs.object_inserted(_BUCKET, self._appendable_blob("object-name", m2))
        self.assertTrue(os.path.exists(dest))
        # The destination now aliases the SECOND staging inode (further appends
        # to m2 are visible at the destination).
        m2.append(b"-more")
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), b"second-generation-content-more")


if __name__ == "__main__":
    unittest.main()
