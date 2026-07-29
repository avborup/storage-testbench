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

"""Integration tests for the conformance traces.

Task 6 freezes each trace's canonicalized output as a committed golden and
diffs future runs against it. That is only a meaningful safety net if a
trace's output is stable: two runs against two independent emulator
instances -- not the same one, so nothing about process reuse or leftover
state can be doing the work -- must be byte-for-byte identical. This is the
property these tests pin, not just "the trace completes without raising".
"""

import gzip
import json
import unittest

from tests.conformance import trace_faults, trace_grpc, trace_rest
from tests.conformance.emulator import Emulator


def _run(module):
    with Emulator() as emu:
        return module.run(emu)


def _canonical_text(result):
    # sort_keys so two structurally-identical results serialize identically
    # regardless of the dict insertion order the interpreter happened to use.
    return json.dumps(result, indent=2, sort_keys=True)


class TestGzippedPayloadLiteral(unittest.TestCase):
    """Guard against re-introducing runtime gzip compression.

    trace_rest.py's GZIPPED_PAYLOAD is a fixed literal precisely because a
    runtime `gzip.compress(...)` call bakes in the capturing machine's zlib
    OS byte (see the comment at its definition), making the golden valid
    only on that machine. This pins the one property that matters: the
    literal must still decompress to the plaintext PAYLOAD.
    """

    def test_gzipped_payload_decompresses_to_payload(self):
        self.assertEqual(
            trace_rest.PAYLOAD, gzip.decompress(trace_rest.GZIPPED_PAYLOAD)
        )


class TestTracesRunEndToEnd(unittest.TestCase):
    """Each trace completes against a real emulator and returns interactions."""

    def test_trace_rest_runs_end_to_end(self):
        result = _run(trace_rest)
        self.assertEqual("rest", result["name"])
        self.assertGreater(len(result["interactions"]), 0)

    def test_trace_grpc_runs_end_to_end(self):
        result = _run(trace_grpc)
        self.assertEqual("grpc", result["name"])
        self.assertGreater(len(result["interactions"]), 0)

    def test_trace_faults_runs_end_to_end(self):
        result = _run(trace_faults)
        self.assertEqual("faults", result["name"])
        self.assertGreater(len(result["interactions"]), 0)


class TestTracesAreDeterministic(unittest.TestCase):
    """Two independent emulator instances must produce byte-identical output.

    This is the property Task 6's golden-diffing rests on: if the same trace
    against two freshly-launched emulators can differ, the goldens can never
    match twice and the harness is permanently red. Each trace gets its own
    pair of `Emulator` instances -- not one instance reused -- so a pass here
    cannot be explained by shared process state.
    """

    def test_trace_rest_is_deterministic_across_two_emulators(self):
        first = _canonical_text(_run(trace_rest))
        second = _canonical_text(_run(trace_rest))
        self.assertEqual(first, second)

    def test_trace_grpc_is_deterministic_across_two_emulators(self):
        first = _canonical_text(_run(trace_grpc))
        second = _canonical_text(_run(trace_grpc))
        self.assertEqual(first, second)

    def test_trace_faults_is_deterministic_across_two_emulators(self):
        first = _canonical_text(_run(trace_faults))
        second = _canonical_text(_run(trace_faults))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
