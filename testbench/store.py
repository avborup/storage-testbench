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
was rejected by a precondition.

Implemented as a base class with no-op methods rather than a
`typing.Protocol`: the supported Python range is 3.8 to 3.12, where
`Protocol` runtime semantics differ, and subclasses should be free to
override only the notifications they care about.
"""


class Store:
    """No-op base class defining the persistence notifications."""

    def bucket_inserted(self, bucket):
        """A new bucket was added. `bucket` is a `gcs.bucket.Bucket`."""

    def bucket_deleted(self, bucket_name):
        """A bucket was removed. `bucket_name` is the proto-form name."""

    def object_inserted(self, bucket_name, blob):
        """A new object generation became live. `blob` is a `gcs.object.Object`."""

    def object_deleted(self, bucket_name, object_name, generation):
        """A specific object generation was removed."""

    def object_updated(self, bucket_name, blob):
        """An existing object's metadata changed."""

    def object_restored(self, bucket_name, blob):
        """A soft-deleted object was restored as a new generation."""

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
