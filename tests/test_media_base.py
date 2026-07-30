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

"""Tests for the Media base type + the Store media factory (Plan 5, Task 1)."""

import hashlib
import json
import unittest

import crc32c

import gcs.bucket
import gcs.object
import testbench.common
from google.storage.v2 import storage_pb2
from testbench.media import BytesMedia, Media
from testbench.store import NullStore


def _bucket():
    request = testbench.common.FakeRequest(args={}, data=json.dumps({"name": "bucket"}))
    bucket, _ = gcs.bucket.Bucket.init(request, None)
    return bucket.metadata


def _meta(name):
    return storage_pb2.Object(name=name)


def _request():
    return testbench.common.FakeRequest(args={}, headers={}, environ={})


class _OtherMedia(Media):
    """A non-BytesMedia Media so the identity-passthrough behaviour is driven by a
    failing test, not only by the Step-6 mutation check. Implements just enough of
    the read surface for Object.init (len/crc32c/md5) to reach the guard."""

    def __len__(self):
        return 0

    def to_bytes(self):
        return b""

    def crc32c(self):
        return crc32c.crc32c(b"")

    def md5(self):
        return hashlib.md5(b"").digest()


class TestMediaBase(unittest.TestCase):
    def test_bytesmedia_is_a_media(self):
        self.assertIsInstance(BytesMedia(b"x"), Media)

    def test_init_keeps_a_prebuilt_non_bytes_media_by_identity(self):
        # The load-bearing behaviour: a Media that is NOT a BytesMedia must NOT be
        # re-wrapped (re-wrapping would materialise a FileMedia into bytes). This
        # test FAILS under the old `isinstance(media, BytesMedia)` guard.
        m = _OtherMedia()
        obj, _ = gcs.object.Object.init(
            _request(), _meta("o"), m, _bucket(), False, None, csek=False
        )
        self.assertIs(obj.media, m)

    def test_init_wraps_raw_bytes(self):
        obj = gcs.object.Object(_meta("o"), b"hello", _bucket())
        self.assertIsInstance(obj.media, BytesMedia)


class TestStoreMediaFactory(unittest.TestCase):
    def test_nullstore_factory_returns_bytesmedia(self):
        s = NullStore()
        self.assertIsInstance(
            s.new_upload_media("projects/_/buckets/b", "u"), BytesMedia
        )
        self.assertIsInstance(
            s.new_staging_media("projects/_/buckets/b", "t"), BytesMedia
        )


if __name__ == "__main__":
    unittest.main()
