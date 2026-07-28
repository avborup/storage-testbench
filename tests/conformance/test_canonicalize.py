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

    def test_soft_delete_policy_effective_time_is_replaced(self):
        # Found by running trace_grpc twice and diffing: a bucket's
        # `soft_delete_policy.effective_time` (and the analogous
        # `retention_policy.effective_time`) is set from `datetime.now()`
        # at creation time and was leaking through uncanonicalized.
        out = self.canon.body(
            {
                "softDeletePolicy": {"effectiveTime": "2026-07-28T12:18:04.189067Z"},
                "soft_delete_policy": {"effective_time": "2026-07-28T12:18:04.189067Z"},
            }
        )
        self.assertEqual("<TIME:1>", out["softDeletePolicy"]["effectiveTime"])
        self.assertEqual("<TIME:1>", out["soft_delete_policy"]["effective_time"])

    def test_metageneration_and_etag_are_preserved(self):
        payload = {"metageneration": "3", "etag": "eccbc87e4b5ce2fe28308fd9f2a7baf3"}
        self.assertEqual(payload, self.canon.body(payload))

    def test_iam_policy_etag_is_replaced(self):
        # `Bucket.__iam_etag` (gcs/bucket.py) returns `uuid.uuid4().hex`: a
        # genuinely random value, unlike a bucket's or object's own
        # deterministic, content-derived `etag` -- despite the two sharing
        # the same field name. Found by running trace_rest and trace_grpc
        # twice and diffing.
        rest_policy = {
            "kind": "storage#policy",
            "bindings": [{"role": "roles/storage.legacyBucketReader"}],
            "etag": "MzgwNjNjMzMyY2Y2NDFhZjlkMDA4YWJjZTk2YmI3ODU=",
        }
        grpc_policy = {
            "version": 1,
            "bindings": [{"role": "roles/storage.legacyBucketReader"}],
            "etag": "MjZiMGUxYmQxODZkNGQyNGE0MGM1YzI3YzA3NmVhYmE=",
        }
        out = self.canon.body({"rest": rest_policy, "grpc": grpc_policy})
        self.assertEqual("<ETAG:1>", out["rest"]["etag"])
        self.assertEqual("<ETAG:2>", out["grpc"]["etag"])

    def test_an_etag_outside_an_iam_policy_shape_is_preserved(self):
        # A dict that merely happens to have an "etag" key, without also
        # looking like an IAM policy (no "bindings"), must not be affected --
        # this is what keeps an ordinary object/bucket etag visible to a diff.
        payload = {"etag": "eccbc87e4b5ce2fe28308fd9f2a7baf3", "version": 1}
        self.assertEqual(payload, self.canon.body(payload))

    def test_generation_embedded_in_an_error_message_is_replaced(self):
        # testbench/error.py's JSON envelope embeds raw internal state as
        # free-form diagnostic text -- e.g. a precondition-mismatch error
        # naming the object's actual generation -- rather than a structured
        # field. Found by running trace_rest twice and diffing.
        out = self.canon.body(
            {
                "generation": "1785234485144",
                "error": {
                    "code": 412,
                    "message": (
                        "ifGenerationMatch validation failed. "
                        "Expected = 1 vs Actual = 1785234485144."
                    ),
                },
            }
        )
        self.assertEqual("<GEN:1>", out["generation"])
        self.assertEqual(
            "ifGenerationMatch validation failed. Expected = 1 vs Actual = <GEN:1>.",
            out["error"]["message"],
        )

    def test_a_message_outside_an_error_envelope_is_preserved(self):
        # A dict with a "message" key but no "code" sibling is not an error
        # envelope, so its text must not be rewritten even if it happens to
        # contain an already-bound value as a substring.
        out = self.canon.body(
            {"generation": "10", "note": {"message": "see generation 10"}}
        )
        self.assertEqual("see generation 10", out["note"]["message"])

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

    def test_location_header_origin_and_upload_id_are_replaced(self):
        # A resumable upload session's `Location` header is a composite URL
        # like `selfLink`/`mediaLink`, not an opaque token: its origin embeds
        # the ephemeral port, and its `upload_id` query parameter is the
        # *only* place that value appears anywhere in the interaction (no
        # sibling body field to bind it from first). Found by running
        # trace_rest twice and diffing.
        out = self.canon.headers(
            {
                "Location": (
                    "http://127.0.0.1:51423/upload/storage/v1/b/bk/o"
                    "?uploadType=resumable&upload_id=abc123"
                )
            }
        )
        self.assertEqual(
            "<ORIGIN:1>/upload/storage/v1/b/bk/o?uploadType=resumable&upload_id=<UPLOAD:1>",
            out["location"],
        )
        self.assertNotIn("51423", out["location"])
        self.assertNotIn("abc123", out["location"])

    def test_body_and_headers_share_one_symbol_space(self):
        body = self.canon.body({"generation": "10"})
        headers = self.canon.headers({"x-goog-generation": "10"})
        self.assertEqual(body["generation"], headers["x-goog-generation"])

    def test_link_origin_is_erased_but_path_is_kept(self):
        # The emulator binds an ephemeral port, so the origin must not reach
        # the golden. The path and query must, or a change to the emulator's
        # URL scheme would be invisible to the diff.
        out = self.canon.body(
            {
                "generation": "10",
                "mediaLink": "http://127.0.0.1:51423/download/storage/v1/b/bk/o/o?generation=10&alt=media",
            }
        )
        self.assertEqual(
            "<ORIGIN:1>/download/storage/v1/b/bk/o/o?generation=<GEN:1>&alt=media",
            out["mediaLink"],
        )
        self.assertNotIn("51423", out["mediaLink"])

    def test_same_origin_across_links_reuses_its_placeholder(self):
        out = self.canon.body(
            {
                "selfLink": "http://127.0.0.1:51423/storage/v1/b/bk/o/o",
                "mediaLink": "http://127.0.0.1:51423/download/storage/v1/b/bk/o/o",
            }
        )
        self.assertTrue(out["selfLink"].startswith("<ORIGIN:1>"))
        self.assertTrue(out["mediaLink"].startswith("<ORIGIN:1>"))

    def test_a_bound_value_does_not_corrupt_an_unrelated_string(self):
        # The canonicalizer must not rewrite fields that merely contain a
        # bound value as a substring; doing so would hide real regressions
        # behind mangled data.
        out = self.canon.body({"generation": "1", "name": "file-v1-final.txt"})
        self.assertEqual("<GEN:1>", out["generation"])
        self.assertEqual("file-v1-final.txt", out["name"])

    def test_a_link_substitutes_a_generation_bound_at_another_depth(self):
        out = self.canon.body(
            {
                "selfLink": "http://h/o?generation=10",
                "inner": {"generation": "10"},
            }
        )
        self.assertEqual("<ORIGIN:1>/o?generation=<GEN:1>", out["selfLink"])
        self.assertEqual("<GEN:1>", out["inner"]["generation"])

    def test_an_uppercase_scheme_origin_is_still_erased(self):
        out = self.canon.body({"selfLink": "HTTP://127.0.0.1:51423/storage/v1/b/bk"})
        self.assertEqual("<ORIGIN:1>/storage/v1/b/bk", out["selfLink"])
        self.assertNotIn("51423", out["selfLink"])

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

    def test_rereading_an_older_generation_passes_invariants(self):
        # A trace that creates object A (generation 10), then creates object
        # B (generation 11, newer), then reads A again (generation 10, the
        # same value as before) is an entirely ordinary access pattern -- not
        # evidence the server's counter went backwards. Only a *value's*
        # first sighting is such evidence; a later reference to that same,
        # already-bound value must not be compared against B's unrelated 11.
        self.canon.body({"generation": "10"})
        self.canon.body({"generation": "11"})
        self.canon.body({"generation": "10"})
        self.canon.assert_invariants()

    def test_malformed_timestamp_fails_invariants(self):
        self.canon.body({"timeCreated": "not-a-timestamp"})
        with self.assertRaises(AssertionError):
            self.canon.assert_invariants()


if __name__ == "__main__":
    unittest.main()
