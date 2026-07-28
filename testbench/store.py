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

"""Persistence seam for the testbench database.

`Database` holds the authoritative in-memory index. A `Store` is notified
after each successful mutation so that an implementation can mirror the index
somewhere durable. `NullStore` is the default and does nothing, which makes
the seam free for every existing caller.

Notifications fire while the database still holds its lock, and only after
the in-memory mutation has succeeded, so a store never observes a change that
was rejected by a precondition, and two mutations of the same locked state
can never be delivered out of order relative to each other.

Implemented as a base class with no-op methods rather than a
`typing.Protocol`: the supported Python range is 3.8 to 3.12, where
`Protocol` runtime semantics differ, and subclasses should be free to
override only the notifications they care about.

Consequences a `Store` implementation must plan around:

- A notification that raises propagates to `Database`'s caller *after* the
  in-memory mutation has already committed. The index and the store are now
  divergent, and the client sees a 500 for an operation that, from the
  index's point of view, already succeeded. `Database` does not catch or
  retry on a store's behalf.
- The call happens while `Database` still holds the lock that guards the
  state being reported. A slow or blocking store therefore stalls every
  other request contending for that same lock, not just the caller.
- A notification handler is allowed to call back into `Database` (for
  example to look up the object it was just handed) only because the locks
  involved are all `threading.RLock`, which the same thread may re-acquire.
  If a lock is ever changed to a plain `threading.Lock`, a re-entrant call
  from inside a notification deadlocks silently. `tests/test_store.py`
  exercises this re-entrancy directly.
- `bucket_name` arguments are always the proto-form name (as produced by
  `Database.__bucket_key`), regardless of whether the request that triggered
  the mutation was REST or gRPC. A notification that calls back into
  `Database` to look something up must pass a non-`None` sentinel `context`
  so `__bucket_key` treats the name as already proto-form instead of
  converting it again (`bucket_name_to_proto` is not idempotent: feeding it
  an already-proto-form name double-prefixes it). Note that `context` is
  also forwarded to `testbench.error.notfound`, which on a real gRPC context
  calls `context.abort(...)`; a bare sentinel object has no such method, so a
  lookup that legitimately misses raises `AttributeError` instead of the
  usual REST/gRPC error. That still surfaces as a failure, but it is a
  different failure than a normal lookup miss would produce.
"""


class Store:
    """No-op base class defining the persistence notifications."""

    def bucket_inserted(self, bucket):
        """A new bucket was added. `bucket` is a `gcs.bucket.Bucket`."""

    def bucket_deleted(self, bucket_name):
        """A bucket was removed. `bucket_name` is the proto-form name."""

    def object_inserted(self, bucket_name, blob):
        """A new object generation became live. `blob` is a `gcs.object.Object`.

        This also covers a soft-deleted object becoming live again: from a
        persistence standpoint a restore is just an insert of the same blob
        at a new generation, so `restore_object` relies on this notification
        rather than emitting a separate one.
        """

    def object_deleted(self, bucket_name, object_name, generation):
        """A specific live object generation was permanently removed.

        Only fired for a hard delete. A delete on a bucket with a soft
        delete policy fires `object_soft_deleted` instead -- the object is
        not gone, it is retained until `hard_delete_time`. A `Store` that
        treated the two as equivalent would discard the copy a client can
        still restore.
        """

    def object_updated(self, bucket_name, blob):
        """An existing object's metadata changed."""

    def object_soft_deleted(self, bucket_name, blob, hard_delete_time):
        """A live object generation moved to the soft-deleted set instead of
        being removed outright. `hard_delete_time` is when the bucket's soft
        delete policy will make the removal permanent; a persistent store
        needs it to expire its own copy on the same schedule.
        """

    def object_purged(self, bucket_name, object_name, generation):
        """A soft-deleted object generation passed its `hard_delete_time` and
        was dropped for good."""

    def folder_inserted(self, folder_name, folder):
        """A managed folder was created."""

    def folder_deleted(self, folder_name):
        """A managed folder was removed."""

    def folder_renamed(self, src_folder_name, dst_folder_name, folder):
        """A managed folder was renamed."""

    def cleared(self):
        """All resources were dropped, as in `Database.clear()`."""


class NullStore(Store):
    """The default store: keeps nothing, so behavior is unchanged."""
