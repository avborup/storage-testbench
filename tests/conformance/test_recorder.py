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

"""Unit tests for the conformance recorder."""

import unittest

import requests

from tests.conformance.recorder import Recorder


class FakeResponse:
    def __init__(self, status_code, headers, content):
        self.status_code = status_code
        self.headers = headers
        self.content = content

    def json(self):
        import json

        return json.loads(self.content)


class TestRecorder(unittest.TestCase):
    def test_records_status_headers_and_json_body(self):
        rec = Recorder("demo")
        rec.record_http(
            "create-bucket",
            FakeResponse(
                200, {"Content-Type": "application/json"}, b'{"generation":"7"}'
            ),
        )
        out = rec.finish()
        entry = out["interactions"][0]
        self.assertEqual("create-bucket", entry["label"])
        self.assertEqual(200, entry["status"])
        self.assertEqual({"generation": "<GEN:1>"}, entry["body"])

    def test_records_binary_body_as_digest_and_length(self):
        # Media payloads must be compared without committing megabytes of
        # bytes to git, but a digest still catches any corruption.
        rec = Recorder("demo")
        rec.record_http(
            "download",
            FakeResponse(200, {"Content-Type": "application/octet-stream"}, b"abc"),
        )
        entry = rec.finish()["interactions"][0]
        self.assertEqual(3, entry["body"]["length"])
        self.assertEqual(
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            entry["body"]["sha256"],
        )

    def test_labels_must_be_unique(self):
        rec = Recorder("demo")
        rec.record_http("x", FakeResponse(200, {}, b"{}"))
        with self.assertRaises(AssertionError):
            rec.record_http("x", FakeResponse(200, {}, b"{}"))

    def test_records_stream_boundaries_and_digest(self):
        rec = Recorder("demo")
        rec.record_stream("read", [b"ab", b"cde", b"f"])
        entry = rec.finish()["interactions"][0]
        self.assertEqual([0, 2, 5], entry["offsets"])
        self.assertEqual(6, entry["length"])

    def test_finish_runs_canonicalizer_invariants(self):
        rec = Recorder("demo")
        rec.record_http(
            "a",
            FakeResponse(
                200, {"Content-Type": "application/json"}, b'{"generation":"9"}'
            ),
        )
        rec.record_http(
            "b",
            FakeResponse(
                200, {"Content-Type": "application/json"}, b'{"generation":"8"}'
            ),
        )
        with self.assertRaises(AssertionError):
            rec.finish()

    def test_transport_errors_normalize_to_one_token(self):
        # A broken stream surfaces as ReadTimeout on macOS and ConnectionError
        # on Linux for the same injected fault. Recording the subclass would
        # make goldens machine-specific and break the CI conformance job, so
        # every transport-level requests failure records identically.
        rec = Recorder("demo")
        rec.record_error("reset", requests.exceptions.ConnectionError("reset"))
        rec.record_error("slow", requests.exceptions.ReadTimeout("timed out"))
        entries = rec.finish()["interactions"]
        self.assertEqual("<TRANSPORT_ERROR>", entries[0]["type"])
        self.assertEqual("<TRANSPORT_ERROR>", entries[1]["type"])

    def test_non_transport_errors_keep_their_type(self):
        # gRPC status codes are chosen by the emulator, so they are
        # deterministic and must stay visible to the diff.
        rec = Recorder("demo")
        rec.record_error("boom", ValueError("nope"))
        self.assertEqual("ValueError", rec.finish()["interactions"][0]["type"])


if __name__ == "__main__":
    unittest.main()
