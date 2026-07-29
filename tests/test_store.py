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

import datetime
import json
import unittest
import unittest.mock

import crc32c
from google.protobuf import field_mask_pb2

import gcs
import testbench
from google.storage.v2 import storage_pb2
from testbench import rest_server
from testbench.store import NullStore, Store

_RETENTION_SECONDS = 7 * 24 * 60 * 60  # minimum allowed by validate_soft_delete_policy


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
            # the name again. (A lookup miss would raise `AttributeError` from
            # `context.abort` rather than the usual not-found error -- see
            # `store.py`'s docstring -- but a hit, which is what we expect
            # here, never reaches that path.)
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

    def object_soft_deleted(self, bucket_name, blob, hard_delete_time):
        self.calls.append(
            ("object_soft_deleted", bucket_name, blob.metadata.name, hard_delete_time)
        )

    def object_purged(self, bucket_name, object_name, generation):
        self.calls.append(("object_purged", bucket_name, object_name, generation))

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


def _make_soft_delete_bucket(name, retention_seconds=_RETENTION_SECONDS):
    return _make_bucket(
        name, softDeletePolicy={"retentionDurationSeconds": retention_seconds}
    )


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
        self.assertIsNone(
            store.object_soft_deleted(
                "projects/_/buckets/bkt", blob, blob.metadata.hard_delete_time
            )
        )
        self.assertIsNone(store.object_purged("projects/_/buckets/bkt", "o", 1))
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
        store.object_soft_deleted(
            "projects/_/buckets/bkt", blob, blob.metadata.hard_delete_time
        )
        store.object_purged("projects/_/buckets/bkt", "o", 1)
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
        # No soft delete policy on this bucket, so this is a hard delete: it
        # must notify `object_deleted`, not `object_soft_deleted`.
        bucket = _make_bucket("bucket-name")
        self.db.insert_bucket(bucket, None)
        blob = _make_object(bucket, "o.txt")
        self.db.insert_object("bucket-name", blob, None)
        self.store.calls.clear()
        self.db.delete_object("bucket-name", "o.txt", None)
        self.assertEqual(
            [
                (
                    "object_deleted",
                    "projects/_/buckets/bucket-name",
                    "o.txt",
                    blob.metadata.generation,
                )
            ],
            self.store.calls,
        )

    def test_soft_delete_notifies_object_soft_deleted_not_object_deleted(self):
        bucket = _make_soft_delete_bucket("soft-bkt")
        self.db.insert_bucket(bucket, None)
        blob = _make_object(bucket, "o.txt")
        self.db.insert_object("soft-bkt", blob, None)
        self.store.calls.clear()
        self.db.delete_object("soft-bkt", "o.txt", None)
        self.assertEqual(
            [
                call
                for call in self.store.calls
                if call[0] in ("object_deleted", "object_soft_deleted")
            ],
            [
                (
                    "object_soft_deleted",
                    "projects/_/buckets/soft-bkt",
                    "o.txt",
                    blob.metadata.hard_delete_time,
                )
            ],
        )

    def test_restore_object_notifies_via_object_inserted_only(self):
        # Finding 1: a restore is an insert of the same blob at a new
        # generation from a persistence standpoint, so it must notify
        # `object_inserted` and nothing else -- specifically not a second,
        # duplicate event for the same generation.
        bucket = _make_soft_delete_bucket("soft-bkt")
        self.db.insert_bucket(bucket, None)
        blob = _make_object(bucket, "o.txt")
        self.db.insert_object("soft-bkt", blob, None)
        original_generation = blob.metadata.generation
        self.db.delete_object("soft-bkt", "o.txt", None)
        self.store.calls.clear()

        restored = self.db.restore_object("soft-bkt", "o.txt", original_generation)

        self.assertNotEqual(original_generation, restored.metadata.generation)
        self.assertEqual(
            [
                (
                    "object_inserted",
                    "projects/_/buckets/soft-bkt",
                    "o.txt",
                    restored.metadata.generation,
                )
            ],
            self.store.calls,
        )

    def test_object_purged_notifies_when_hard_delete_time_has_passed(self):
        bucket = _make_soft_delete_bucket("soft-bkt")
        self.db.insert_bucket(bucket, None)
        blob = _make_object(bucket, "o.txt")
        self.db.insert_object("soft-bkt", blob, None)
        generation = blob.metadata.generation
        self.db.delete_object("soft-bkt", "o.txt", None)

        bucket_key = "projects/_/buckets/soft-bkt"
        # There is no public way to fast-forward the retention window, so
        # reach into the soft-deleted record directly (a plain attribute,
        # not name-mangled) and back-date its hard_delete_time so the next
        # expiry sweep treats it as expired. Two days of margin avoids any
        # ambiguity from the sweep comparing a naive local `now()` against a
        # naive UTC `ToDatetime()`.
        soft_deleted = self.db._soft_deleted_objects[bucket_key]["o.txt"][0]
        soft_deleted.metadata.hard_delete_time.FromDatetime(
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)
        )
        self.store.calls.clear()

        # restore_object() runs the expiry sweep before looking the requested
        # generation up; since the sweep just purged it, the lookup itself
        # then raises not-found. That exception is expected and orthogonal to
        # what this test checks: the sweep must have notified regardless.
        with self.assertRaises(Exception):
            self.db.restore_object("soft-bkt", "o.txt", generation)

        self.assertIn(
            ("object_purged", bucket_key, "o.txt", generation), self.store.calls
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


class _CapturingObjectUpdatedStore(Store):
    """Records `object_updated` calls with a fully-materialized snapshot.

    `blob.rest_metadata()` is called eagerly, inside the notification, so the
    recorded dict reflects exactly what `update_fn` left behind -- not
    whatever the same, mutable blob happens to look like by the time the
    test later inspects it.
    """

    def __init__(self):
        self.calls = []

    def object_updated(self, bucket_name, blob):
        self.calls.append((bucket_name, blob.rest_metadata()))


def _grpc_context():
    context = unittest.mock.Mock()
    context.invocation_metadata = unittest.mock.Mock(return_value=dict())
    context.abort.side_effect = testbench.error.RestException("aborted", 400)
    return context


class TestDoUpdateObjectNotifiesObjectUpdated(unittest.TestCase):
    """C2: every `do_update_object` call site mutates, so every one must
    notify `object_updated`. Driven through real REST and gRPC entry points
    (never by calling `do_update_object` directly), so each test also pins
    that call site's own path to the notification, not just the generic
    wiring inside `Database`.
    """

    def setUp(self):
        self.store = _CapturingObjectUpdatedStore()
        # `rest_server.db` is a plain module attribute the route handlers
        # look up dynamically, so it can be swapped for the duration of a
        # test and must be restored afterwards -- it is shared, global state
        # for the whole test process.
        self._original_rest_db = rest_server.db
        rest_server.db = testbench.database.Database.init(store=self.store)
        rest_server.db.insert_bucket(_make_bucket("bucket-name"), None)
        self.client = rest_server.server.test_client()

    def tearDown(self):
        rest_server.db = self._original_rest_db

    def test_object_patch_notifies_object_updated_with_post_mutation_blob(self):
        self.client.put("/bucket-name/o.txt", content_type="text/plain", data=b"hello")
        self.store.calls.clear()

        response = self.client.patch(
            "/storage/v1/b/bucket-name/o/o.txt",
            data=json.dumps({"metadata": {"colour": "blue"}}),
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(self.store.calls))
        bucket_name, snapshot = self.store.calls[0]
        self.assertEqual("projects/_/buckets/bucket-name", bucket_name)
        self.assertEqual("o.txt", snapshot["name"])
        self.assertEqual("blue", snapshot["metadata"]["colour"])

    def test_object_acl_insert_notifies_object_updated_with_post_mutation_blob(self):
        self.client.put("/bucket-name/o.txt", content_type="text/plain", data=b"hello")
        self.store.calls.clear()

        response = self.client.post(
            "/storage/v1/b/bucket-name/o/o.txt/acl",
            data=json.dumps({"entity": "allUsers", "role": "READER"}),
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(self.store.calls))
        bucket_name, snapshot = self.store.calls[0]
        self.assertEqual("projects/_/buckets/bucket-name", bucket_name)
        self.assertIn("allUsers", [entry["entity"] for entry in snapshot["acl"]])

    def test_grpc_update_object_notifies_object_updated_with_post_mutation_blob(self):
        store = _CapturingObjectUpdatedStore()
        db = testbench.database.Database.init(store=store)
        db.insert_bucket(_make_bucket("bucket-name"), None)
        blob = _make_object(_make_bucket("bucket-name"), "o.txt")
        db.insert_object("bucket-name", blob, None)
        grpc = testbench.grpc_server.StorageServicer(db)

        request = storage_pb2.UpdateObjectRequest(
            object=storage_pb2.Object(
                bucket="projects/_/buckets/bucket-name",
                name="o.txt",
                content_type="text/plain",
            ),
            update_mask=field_mask_pb2.FieldMask(paths=["content_type"]),
        )
        response = grpc.UpdateObject(request, _grpc_context())

        self.assertEqual("text/plain", response.content_type)
        self.assertEqual(1, len(store.calls))
        bucket_name, snapshot = store.calls[0]
        self.assertEqual("projects/_/buckets/bucket-name", bucket_name)
        self.assertEqual("text/plain", snapshot["contentType"])

    def test_appendable_finalize_notifies_object_updated_with_post_mutation_media(self):
        # Driving this through gcs/upload.py's real BidiWriteObject appendable
        # path (rather than the two REST cases above) is what proves
        # `do_update_object`'s notification also covers a mutation of
        # `blob.media` itself -- an appendable object's bytes and size change
        # without ever calling `insert_object`, so this is the one call site
        # `object_inserted` cannot stand in for. A single message carrying
        # `write_object_spec`, `checksummed_data`, and `finish_write=True`
        # together drives the emulator through both `do_update_object`
        # mutators in one call: `update_appendable_blob` (writes the bytes)
        # and, once the loop sees `upload.complete`, `finalize_blob` (sets
        # `finalize_time`). This was verified against a live Database before
        # being committed here, so it is not a fabricated substitute for the
        # brief's literal REST-only fallback.
        store = _CapturingObjectUpdatedStore()
        db = testbench.database.Database.init(store=store)
        db.insert_bucket(_make_bucket("bucket-name"), None)
        grpc = testbench.grpc_server.StorageServicer(db)

        media = b"hello world, this is appendable content"
        request = storage_pb2.BidiWriteObjectRequest(
            write_object_spec=storage_pb2.WriteObjectSpec(
                resource=storage_pb2.Object(
                    name="appendable.txt", bucket="projects/_/buckets/bucket-name"
                ),
                appendable=True,
            ),
            write_offset=0,
            checksummed_data=storage_pb2.ChecksummedData(
                content=media, crc32c=crc32c.crc32c(media)
            ),
            finish_write=True,
        )
        responses = list(grpc.BidiWriteObject(iter([request]), context=_grpc_context()))

        # Not asserted against `responses[0].resource`: a single-message
        # appendable write that both creates and finalizes the object in one
        # call hits a pre-existing, unrelated quirk in `gcs/upload.py`'s
        # `response()` closure (it treats the terminal yield as the "first
        # response" and overwrites `resource` from the pre-finalize
        # `upload.metadata`), which is production behavior out of scope
        # here. The store's snapshot, captured from the real `blob` object
        # `finalize_blob` mutated, is unaffected by that response-encoding
        # detail and is what this test is actually about.
        self.assertEqual(1, len(responses))
        # `update_appendable_blob` (media write) then `finalize_blob`
        # (finalize_time) -- both are `object_updated`, not `object_inserted`.
        self.assertEqual(2, len(store.calls))
        for bucket_name, snapshot in store.calls:
            self.assertEqual("projects/_/buckets/bucket-name", bucket_name)
            self.assertEqual("appendable.txt", snapshot["name"])
        _, last_snapshot = store.calls[-1]
        self.assertEqual(str(len(media)), last_snapshot["size"])
        self.assertIn("timeFinalized", last_snapshot)


class TestObjectAndBucketNamesAreUnvalidatedCallerInput(unittest.TestCase):
    """I8: `Database` and the notifications it fires trust `bucket_name`
    exactly as given. `gcs.bucket.py`'s bucket-name validation only checks
    length in its dotted-name branch and never applies the character-class
    regex, so a dotted, traversal-shaped name is accepted today. This pins
    that as current, tested behavior -- not a defect this branch introduces
    or fixes -- so a future `Store` author does not have to rediscover it.
    """

    def test_dotted_traversal_shaped_bucket_name_is_accepted_and_notified(self):
        store = RecordingStore()
        db = testbench.database.Database.init(store=store)
        traversal_name = "../../etc/passwd"

        bucket = _make_bucket(traversal_name)
        db.insert_bucket(bucket, None)

        self.assertIn(
            ("bucket_inserted", "projects/_/buckets/../../etc/passwd"),
            store.calls,
        )


if __name__ == "__main__":
    unittest.main()
