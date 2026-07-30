#!/usr/bin/env python3
#
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

"""Direct-drive + adversarial tests for FileStore's notification handlers.

Every write is asserted to land inside the bucket root via CONTAINED
filesystem ops; the planted-symlink test proves the O_NOFOLLOW walk is live
(a plain open() would escape the root). These tests never go through the
conformance harness -- they drive FileStore directly against a temp root.
"""

import os
import shutil
import tempfile
import unittest

# Reuse tests/test_store.py's proto-builders. `_make_object` dereferences
# `bucket.metadata`, so a real Bucket is passed (the plan's `None` placeholder
# only stands in for "any bucket whose metadata FileStore ignores" -- FileStore
# derives the short name from the explicit `bucket_name` argument, not the
# blob's origin bucket).
from testbench import sidecar
from testbench.filestore import FileStore
from tests.test_store import _make_bucket, _make_object


class TestFileStore(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.fs = FileStore(self.root)

    def _bucket_dir(self, name="bucket-name"):
        return os.path.join(self.root, name)

    def _insert_bucket(self, name="bucket-name"):
        bucket = _make_bucket(name)
        self.fs.bucket_inserted(bucket)
        return bucket

    def test_bucket_inserted_builds_tree_and_bucket_json(self):
        self._insert_bucket("bucket-name")
        d = self._bucket_dir()
        for sub in ("generations", "soft_deleted", "uploads", "folders", "overflow"):
            self.assertTrue(os.path.isdir(os.path.join(d, ".gcs", sub)))
        kind, name, _ = sidecar.read(os.path.join(d, ".gcs", "bucket.json"))
        self.assertEqual(("Bucket", "bucket-name"), (kind, name))

    def test_validate_bucket_name_rejects_traversal(self):
        with self.assertRaises(Exception):
            self.fs.validate_bucket_name("projects/_/buckets/../../etc/passwd", None)

    def test_object_inserted_writes_media_and_sidecar_at_natural_path(self):
        bucket = self._insert_bucket("bucket-name")
        blob = _make_object(bucket, "audio/clip.wav", media=b"hello")
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        media = os.path.join(self._bucket_dir(), "audio", "clip.wav")
        with open(media, "rb") as handle:
            self.assertEqual(b"hello", handle.read())
        self.assertTrue(os.path.exists(media + ".gcsmeta"))

    def test_object_inserted_refuses_symlinked_intermediate_component(self):
        # Plant <bucket>/audio -> /tmp/outside BEFORE the write; the O_NOFOLLOW
        # walk must refuse to follow it, and nothing may be written outside root.
        bucket = self._insert_bucket("bucket-name")
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        os.symlink(outside, os.path.join(self._bucket_dir(), "audio"))
        blob = _make_object(bucket, "audio/clip.wav", media=b"pwn")
        with self.assertRaises(OSError):
            self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        self.assertEqual([], os.listdir(outside))  # nothing escaped

    def test_soft_delete_moves_to_gcs_soft_deleted_not_removed(self):
        bucket = self._insert_bucket("bucket-name")
        blob = _make_object(bucket, "clip.wav", media=b"x")
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        self.fs.object_soft_deleted(
            "projects/_/buckets/bucket-name", blob, blob.metadata.hard_delete_time
        )
        self.assertFalse(os.path.exists(os.path.join(self._bucket_dir(), "clip.wav")))
        self.assertTrue(
            os.listdir(os.path.join(self._bucket_dir(), ".gcs", "soft_deleted"))
        )

    def test_object_updated_is_idempotent(self):
        bucket = self._insert_bucket("bucket-name")
        blob = _make_object(bucket, "clip.wav", media=b"x")
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        p = os.path.join(self._bucket_dir(), "clip.wav.gcsmeta")
        with open(p) as handle:
            before = handle.read()
        self.fs.object_updated("projects/_/buckets/bucket-name", blob)
        self.fs.object_updated("projects/_/buckets/bucket-name", blob)
        with open(p) as handle:
            self.assertEqual(before, handle.read())

    def test_overflow_name_lands_under_overflow_with_true_name(self):
        bucket = self._insert_bucket("bucket-name")
        blob = _make_object(bucket, "clip.wav.gcsmeta", media=b"x")
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        overflow = os.path.join(self._bucket_dir(), ".gcs", "overflow")
        entries = [f for f in os.listdir(overflow) if f.endswith(".gcsmeta")]
        _, true_name, _ = sidecar.read(os.path.join(overflow, entries[0]))
        self.assertEqual("clip.wav.gcsmeta", true_name)

    def test_object_deleted_removes_media_and_sidecar(self):
        bucket = self._insert_bucket("bucket-name")
        blob = _make_object(bucket, "clip.wav", media=b"x")
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        self.fs.object_deleted(
            "projects/_/buckets/bucket-name", "clip.wav", blob.metadata.generation
        )
        self.assertFalse(os.path.exists(os.path.join(self._bucket_dir(), "clip.wav")))
        self.assertFalse(
            os.path.exists(os.path.join(self._bucket_dir(), "clip.wav.gcsmeta"))
        )

    def test_object_purged_drops_soft_deleted_generation(self):
        bucket = self._insert_bucket("bucket-name")
        blob = _make_object(bucket, "clip.wav", media=b"x")
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        gen = blob.metadata.generation
        self.fs.object_soft_deleted(
            "projects/_/buckets/bucket-name", blob, blob.metadata.hard_delete_time
        )
        soft = os.path.join(self._bucket_dir(), ".gcs", "soft_deleted", str(gen))
        self.assertTrue(os.path.isdir(soft))
        self.fs.object_purged("projects/_/buckets/bucket-name", "clip.wav", gen)
        self.assertFalse(os.path.exists(soft))

    def test_cleared_and_bucket_deleted_tolerate_missing_dir(self):
        self._insert_bucket("bucket-name")
        self.fs.bucket_deleted("projects/_/buckets/bucket-name")
        self.fs.bucket_deleted("projects/_/buckets/bucket-name")  # already gone
        self.fs.cleared()

    def test_folder_handlers_do_not_touch_resource_state(self):
        self._insert_bucket("bucket-name")
        self.fs.folder_inserted("bucket-name/logs/", object())
        self.assertTrue(os.listdir(os.path.join(self._bucket_dir(), ".gcs", "folders")))
        self.fs.folder_deleted("bucket-name/logs/")
        self.assertEqual(
            [], os.listdir(os.path.join(self._bucket_dir(), ".gcs", "folders"))
        )


if __name__ == "__main__":
    unittest.main()
