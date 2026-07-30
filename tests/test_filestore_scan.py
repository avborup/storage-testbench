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

"""Startup tree-scan / index-hydration tests for FileStore.rebuild_index.

The scan reseeds the in-memory index directly from the on-disk sidecars
(without re-notifying), fails LOUDLY on a corrupt sidecar, and detects a
collision by the filesystem-truthful inode-identity rule (so it fires on a
case-insensitive FS and correctly stays silent on a case-sensitive one). The
restore-reconciliation test is the ONLY gate that can observe the object_purged
disk cleanup -- B == C cannot, because the external restore response is
identical whether or not the stale on-disk copy is removed.
"""

import os
import shutil
import tempfile
import unittest

import testbench.database
from testbench.filestore import FileStore
from tests.test_store import _make_bucket, _make_object, _make_soft_delete_bucket


def _case_insensitive(root):
    probe = os.path.join(root, "CaseProbe")
    open(probe, "w").close()
    try:
        return os.path.exists(os.path.join(root, "caseprobe"))
    finally:
        os.remove(probe)


class TestScan(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.fs = FileStore(self.root)

    def test_round_trip_rebuild_matches(self):
        bucket = _make_bucket("bucket-name")
        self.fs.bucket_inserted(bucket)
        blob = _make_object(bucket, "audio/clip.wav", media=b"hi")
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        db = testbench.database.Database.init(store=self.fs)  # hydrates
        self.assertIn("projects/_/buckets/bucket-name", db._buckets)
        # Object present in the rebuilt index, via the public get_object path.
        obj = db.get_object("bucket-name", "audio/clip.wav")
        self.assertIsNotNone(obj)
        self.assertEqual(blob.metadata.generation, obj.metadata.generation)
        self.assertEqual(b"hi", obj.media.to_bytes())

    def test_case_collision_is_loud_on_case_insensitive_fs(self):
        bucket = _make_bucket("bucket-name")
        self.fs.bucket_inserted(bucket)
        b1 = _make_object(bucket, "Clip.wav", media=b"a")
        self.fs.object_inserted("projects/_/buckets/bucket-name", b1)
        b2 = _make_object(bucket, "clip.wav", media=b"b")
        if _case_insensitive(self.root):
            # write-time guard fires first (different true-name at same target)
            with self.assertRaises(RuntimeError):
                self.fs.object_inserted("projects/_/buckets/bucket-name", b2)
        else:
            # case-sensitive FS: both coexist, rebuild sees two distinct inodes
            self.fs.object_inserted("projects/_/buckets/bucket-name", b2)
            db = testbench.database.Database.init(store=self.fs)
            self.assertIn("projects/_/buckets/bucket-name", db._buckets)

    def test_corrupt_sidecar_raises_loudly(self):
        bucket = _make_bucket("bucket-name")
        self.fs.bucket_inserted(bucket)
        blob = _make_object(bucket, "clip.wav", media=b"x")
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        with open(
            os.path.join(self.root, "bucket-name", "clip.wav.gcsmeta"), "w"
        ) as fh:
            fh.write('{"schema_version":1,"proto"')  # truncated
        with self.assertRaises(ValueError):
            testbench.database.Database.init(store=self.fs)

    def test_restore_removes_soft_deleted_on_disk(self):
        # soft-delete then restore -> the .gcs/soft_deleted/<orig_gen> dir is
        # gone (object_purged reconciliation), live media/sidecar correct.
        db = testbench.database.Database.init(store=self.fs)
        bucket = _make_soft_delete_bucket("sd-bucket")
        db.insert_bucket(bucket, None)  # soft_delete_policy set
        blob = _make_object(bucket, "clip.wav", media=b"x")
        db.insert_object("sd-bucket", blob, None)
        orig_gen = blob.metadata.generation
        db.delete_object("sd-bucket", "clip.wav")  # soft-deletes
        sd_dir = os.path.join(
            self.root, "sd-bucket", ".gcs", "soft_deleted", str(orig_gen)
        )
        self.assertTrue(os.path.isdir(sd_dir))
        db.restore_object("sd-bucket", "clip.wav", orig_gen)
        self.assertFalse(os.path.exists(sd_dir))  # purge reconciled the disk


if __name__ == "__main__":
    unittest.main()
