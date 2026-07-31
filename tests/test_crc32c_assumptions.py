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

"""Pin the third-party assumptions the file backend design depends on.

The file backend replaces whole-buffer checksums with incremental ones. That
is only correct if `crc32c.crc32c()` accepts a seed and chains, and if
`hashlib.md5()` supports incremental updates. If either assumption breaks
under a dependency upgrade, this test fails loudly rather than silently
producing wrong checksums for every uploaded object.
"""

import hashlib
import unittest

import crc32c


class TestCrc32cAssumptions(unittest.TestCase):
    def test_crc32c_accepts_a_seed_and_chains(self):
        whole = crc32c.crc32c(b"helloworld")
        chained = crc32c.crc32c(b"world", crc32c.crc32c(b"hello"))
        self.assertEqual(whole, chained)

    def test_crc32c_chains_across_many_chunks(self):
        payload = bytes(range(256)) * 97
        whole = crc32c.crc32c(payload)
        chained = 0
        for offset in range(0, len(payload), 1000):
            chained = crc32c.crc32c(payload[offset : offset + 1000], chained)
        self.assertEqual(whole, chained)

    def test_crc32c_of_empty_input_is_zero_seed_identity(self):
        self.assertEqual(crc32c.crc32c(b""), 0)
        self.assertEqual(crc32c.crc32c(b"", 12345), 12345)

    def test_md5_supports_incremental_update(self):
        payload = bytes(range(256)) * 97
        incremental = hashlib.md5()
        for offset in range(0, len(payload), 1000):
            incremental.update(payload[offset : offset + 1000])
        self.assertEqual(hashlib.md5(payload).digest(), incremental.digest())


if __name__ == "__main__":
    unittest.main()
