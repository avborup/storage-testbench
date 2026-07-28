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

"""Tests for the Database -> Store notification contract.

These tests pin the contract that the file backend depends on. A mutation
that reaches the in-memory index but not the store would produce an emulator
whose disk silently diverges from its behavior, so each mutating path is
asserted explicitly.
"""

import json
import unittest

import gcs
import testbench
from testbench.store import NullStore, Store


class RecordingStore(Store):
    def __init__(self, db=None):
        # `db`, when supplied, lets a notification look back at the database
        # to assert that the mutation is already visible from inside the
        # call -- i.e. that notifications fire strictly after the mutation,
        # not before it.
        self.db = db
        self.calls = []

    def bucket_inserted(self, bucket):
        self.calls.append(("bucket_inserted", bucket.metadata.name))

    def bucket_deleted(self, bucket_name):
        self.calls.append(("bucket_deleted", bucket_name))

    def object_inserted(self, bucket_name, blob):
        if self.db is not None:
            # The mutation must already be visible to a lookup performed from
            # inside the notification itself. `bucket_name` here is already
            # in proto form (see `Database.__bucket_key`), so pass a non-None
            # sentinel `context` to `get_object` to tell it not to convert
            # the name again.
            current = self.db.get_object(
                bucket_name, blob.metadata.name, context=object()
            )
            if (
                current is None
                or current.metadata.generation != blob.metadata.generation
            ):
                raise AssertionError(
                    "object_inserted fired before the mutation was visible"
                )
        self.calls.append(
            (
                "object_inserted",
                bucket_name,
                blob.metadata.name,
                blob.metadata.generation,
            )
        )

    def object_deleted(self, bucket_name, object_name, generation):
        self.calls.append(("object_deleted", bucket_name, object_name, generation))

    def object_updated(self, bucket_name, blob):
        self.calls.append(("object_updated", bucket_name, blob.metadata.name))

    def object_restored(self, bucket_name, blob):
        self.calls.append(("object_restored", bucket_name, blob.metadata.name))

    def folder_inserted(self, folder_name, folder):
        self.calls.append(("folder_inserted", folder_name))

    def folder_deleted(self, folder_name):
        self.calls.append(("folder_deleted", folder_name))

    def folder_renamed(self, src_folder_name, dst_folder_name, folder):
        self.calls.append(("folder_renamed", src_folder_name, dst_folder_name))

    def cleared(self):
        self.calls.append(("cleared",))


def _make_bucket(name, **fields):
    payload = dict(fields)
    payload["name"] = name
    request = testbench.common.FakeRequest(args={}, data=json.dumps(payload))
    bucket, _ = gcs.bucket.Bucket.init(request, None)
    return bucket


def _make_object(bucket, name, media=b"hello"):
    # `headers` and `environ` are required: FakeRequest is a SimpleNamespace,
    # so absent attributes raise, and Object.init reaches request.headers via
    # extract_instruction() and csek.extract(). This mirrors the construction
    # used in tests/test_object.py.
    request = testbench.common.FakeRequest(args={}, headers={}, environ={})
    blob, _ = gcs.object.Object.init_dict(
        request, {"name": name}, media, bucket.metadata, False
    )
    return blob


class TestStoreContract(unittest.TestCase):
    def setUp(self):
        self.store = RecordingStore()
        self.db = testbench.database.Database.init(store=self.store)
        self.store.db = self.db

    def test_default_store_is_a_null_store(self):
        self.assertIsInstance(testbench.database.Database.init().store, NullStore)

    def test_database_with_no_store_argument_defaults_to_null_store(self):
        # A `Database` built exactly the way every pre-existing caller in the
        # codebase builds one -- with no `store` keyword at all -- must land
        # on the same `NullStore` default as `Database.init()`. This guards
        # against the seam being wired for `.init()` but not for direct
        # construction (or vice-versa), which would be a latent behavior
        # difference the next task would build on top of unknowingly.
        db = testbench.database.Database({}, {}, {}, {}, {}, {}, [], {}, {})
        self.assertIsInstance(db.store, NullStore)
        # Every notification the contract defines must exist on the default
        # and be a genuine no-op (return None), or a default-configured
        # emulator would crash, or silently do something, on any mutation.
        store = db.store
        bucket = _make_bucket("bkt")
        blob = _make_object(bucket, "o")
        self.assertIsNone(store.bucket_inserted(bucket))
        self.assertIsNone(store.object_inserted("projects/_/buckets/bkt", blob))
        self.assertIsNone(store.object_updated("projects/_/buckets/bkt", blob))
        self.assertIsNone(store.object_restored("projects/_/buckets/bkt", blob))
        self.assertIsNone(store.object_deleted("projects/_/buckets/bkt", "o", 1))
        self.assertIsNone(store.folder_inserted("f", object()))
        self.assertIsNone(store.folder_renamed("f", "g", object()))
        self.assertIsNone(store.folder_deleted("g"))
        self.assertIsNone(store.bucket_deleted("projects/_/buckets/bkt"))
        self.assertIsNone(store.cleared())

    def test_null_store_accepts_every_notification(self):
        # NullStore must implement the whole protocol, or a default-configured
        # emulator would crash on any mutation.
        store = NullStore()
        bucket = _make_bucket("bkt")
        blob = _make_object(bucket, "o")
        store.bucket_inserted(bucket)
        store.object_inserted("projects/_/buckets/bkt", blob)
        store.object_updated("projects/_/buckets/bkt", blob)
        store.object_restored("projects/_/buckets/bkt", blob)
        store.object_deleted("projects/_/buckets/bkt", "o", 1)
        store.folder_inserted("f", object())
        store.folder_renamed("f", "g", object())
        store.folder_deleted("g")
        store.bucket_deleted("projects/_/buckets/bkt")
        store.cleared()

    def test_insert_bucket_notifies(self):
        self.db.insert_bucket(_make_bucket("bucket-name"), None)
        self.assertIn(
            ("bucket_inserted", "projects/_/buckets/bucket-name"), self.store.calls
        )

    def test_duplicate_insert_bucket_does_not_notify(self):
        self.db.insert_bucket(_make_bucket("bucket-name"), None)
        self.store.calls.clear()
        with self.assertRaises(Exception):
            self.db.insert_bucket(_make_bucket("bucket-name"), None)
        self.assertEqual([], self.store.calls)

    def test_delete_bucket_notifies(self):
        self.db.insert_bucket(_make_bucket("bucket-name"), None)
        self.db.delete_bucket("bucket-name", None)
        self.assertIn(
            ("bucket_deleted", "projects/_/buckets/bucket-name"), self.store.calls
        )

    def test_insert_object_notifies_with_generation(self):
        bucket = _make_bucket("bucket-name")
        self.db.insert_bucket(bucket, None)
        blob = _make_object(bucket, "o.txt")
        self.db.insert_object("bucket-name", blob, None)
        self.assertIn(
            (
                "object_inserted",
                "projects/_/buckets/bucket-name",
                "o.txt",
                blob.metadata.generation,
            ),
            self.store.calls,
        )

    def test_insert_object_notification_sees_the_mutation_already_applied(self):
        # Pins hazard 1 as behavior, not just as a comment: the store must be
        # able to observe the new state *from inside* the notification call,
        # which is only possible if the notification fires after the mutation
        # (and while still holding the lock that serializes it).
        bucket = _make_bucket("bucket-name")
        self.db.insert_bucket(bucket, None)
        blob = _make_object(bucket, "o.txt")
        # RecordingStore.object_inserted raises AssertionError itself if the
        # database does not yet reflect the insert; letting that propagate
        # here (instead of silently swallowing it) is what makes this a real
        # assertion rather than a vacuous one.
        self.db.insert_object("bucket-name", blob, None)
        self.assertIn(
            (
                "object_inserted",
                "projects/_/buckets/bucket-name",
                "o.txt",
                blob.metadata.generation,
            ),
            self.store.calls,
        )

    def test_failed_precondition_does_not_notify(self):
        bucket = _make_bucket("bucket-name")
        self.db.insert_bucket(bucket, None)
        self.store.calls.clear()
        never = lambda current, live_generation, context: False
        self.db.insert_object(
            "bucket-name", _make_object(bucket, "o.txt"), None, preconditions=[never]
        )
        self.assertEqual([], self.store.calls)

    def test_delete_object_notifies(self):
        bucket = _make_bucket("bucket-name")
        self.db.insert_bucket(bucket, None)
        blob = _make_object(bucket, "o.txt")
        self.db.insert_object("bucket-name", blob, None)
        self.store.calls.clear()
        self.db.delete_object("bucket-name", "o.txt", None)
        self.assertIn(
            (
                "object_deleted",
                "projects/_/buckets/bucket-name",
                "o.txt",
                blob.metadata.generation,
            ),
            self.store.calls,
        )

    def test_insert_folder_notifies(self):
        self.db.insert_folder("f/", object(), None)
        self.assertIn(("folder_inserted", "f/"), self.store.calls)

    def test_delete_folder_notifies(self):
        self.db.insert_folder("f/", object(), None)
        self.store.calls.clear()
        self.db.delete_folder("f/", None)
        self.assertIn(("folder_deleted", "f/"), self.store.calls)

    def test_rename_folder_notifies(self):
        self.db.insert_folder("f/", object(), None)
        self.store.calls.clear()
        self.db.rename_folder("f/", "g/", None)
        self.assertIn(("folder_renamed", "f/", "g/"), self.store.calls)

    def test_clear_notifies(self):
        self.db.clear()
        self.assertIn(("cleared",), self.store.calls)


if __name__ == "__main__":
    unittest.main()
