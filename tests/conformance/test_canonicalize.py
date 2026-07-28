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

"""Unit tests for conformance canonicalization."""

import unittest

from tests.conformance.canonicalize import Canonicalizer


class TestCanonicalizer(unittest.TestCase):
    def setUp(self):
        self.canon = Canonicalizer()

    def test_generation_is_replaced(self):
        out = self.canon.body({"generation": "1753000000000001"})
        self.assertEqual({"generation": "<GEN:1>"}, out)

    def test_timestamps_are_replaced(self):
        out = self.canon.body({"timeCreated": "2026-07-28T10:00:00.000Z"})
        self.assertEqual({"timeCreated": "<TIME:1>"}, out)

    def test_metageneration_and_etag_are_preserved(self):
        payload = {"metageneration": "3", "etag": "eccbc87e4b5ce2fe28308fd9f2a7baf3"}
        self.assertEqual(payload, self.canon.body(payload))

    def test_nested_and_listed_values_are_replaced(self):
        out = self.canon.body({"items": [{"generation": "10"}, {"generation": "11"}]})
        self.assertEqual(
            {"items": [{"generation": "<GEN:1>"}, {"generation": "<GEN:2>"}]}, out
        )

    def test_repeated_generation_reuses_its_placeholder(self):
        out = self.canon.body({"a": {"generation": "10"}, "b": {"generation": "10"}})
        self.assertEqual(out["a"]["generation"], out["b"]["generation"])

    def test_generation_embedded_in_a_link_is_replaced(self):
        out = self.canon.body(
            {
                "generation": "10",
                "mediaLink": "http://h/download/storage/v1/b/bk/o/o?generation=10&alt=media",
            }
        )
        self.assertEqual("<GEN:1>", out["generation"])
        self.assertIn("<GEN:1>", out["mediaLink"])
        self.assertNotIn("10", out["mediaLink"].replace("v1", ""))

    def test_volatile_headers_are_dropped(self):
        out = self.canon.headers({"Date": "Tue, 28 Jul 2026 10:00:00 GMT", "ETag": "x"})
        self.assertNotIn("Date", out)
        self.assertEqual("x", out["etag"])

    def test_header_names_are_lowercased_for_stability(self):
        out = self.canon.headers({"X-Goog-Generation": "10"})
        self.assertEqual({"x-goog-generation": "<GEN:1>"}, out)

    def test_body_and_headers_share_one_symbol_space(self):
        body = self.canon.body({"generation": "10"})
        headers = self.canon.headers({"x-goog-generation": "10"})
        self.assertEqual(body["generation"], headers["x-goog-generation"])

    def test_monotonic_generations_pass_invariants(self):
        self.canon.body({"generation": "10"})
        self.canon.body({"generation": "11"})
        self.canon.assert_invariants()

    def test_non_monotonic_generations_fail_invariants(self):
        # Generations are assigned from a monotonic counter, so a decrease is
        # a real defect and must not be canonicalized away.
        self.canon.body({"generation": "11"})
        self.canon.body({"generation": "10"})
        with self.assertRaises(AssertionError):
            self.canon.assert_invariants()

    def test_malformed_timestamp_fails_invariants(self):
        self.canon.body({"timeCreated": "not-a-timestamp"})
        with self.assertRaises(AssertionError):
            self.canon.assert_invariants()


if __name__ == "__main__":
    unittest.main()
