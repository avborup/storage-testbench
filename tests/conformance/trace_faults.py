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

"""Fault-injection conformance trace.

Covers every instruction documented in README.md. Outcomes include connection
resets and timeouts, which are recorded as errors: a change from "the client
saw a reset" to "the client got data" is exactly the regression this trace
exists to catch.

The emulator has TWO independent injection mechanisms with different
grammars, and an instruction only fires through the one that owns it. An
earlier revision of this trace pushed all twelve instructions through the
Retry Test API, and nine of them recorded either a creation-time 400 or an
unmodified 200 -- stable, meaningless interactions that read as coverage.
A reviewer disproved that by probing a live emulator directly, so the split
below is empirical, not inferred:

1. The `x-goog-emulator-instructions` REQUEST HEADER (README lines 146-199).
   `x-goog-testbench-instructions` is the deprecated spelling of the same
   header; the current one is used here. Verified to produce real faults
   this way: `return-corrupted-data` (200, corrupted bytes),
   `stall-always` (`ReadTimeout`), `stall-at-256KiB` (`ConnectionError`),
   `return-503-after-256K` and its `/retry-N` spellings (`ChunkedEncodingError`
   -- the response starts streaming, then the connection is reset mid-body,
   because `gcs/object.py`'s `rest_media` only distinguishes the `/retry-N`
   suffix for a *ranged* request; an unranged GET, which is all this trace
   sends, hits the same reset-connection branch regardless of suffix).
2. The Retry Test API, whose instruction grammar is a separate set of
   *anchored* regexes in `testbench/common.py:41-55` --
   `return-<code>-after-<N>K`, `stall-for-<T>s-after-<N>K`,
   `redirect-send-token-<lowercase>`, and friends. The anchoring is why a
   `/retry-1` suffix cannot match here. Verified reachable through this API:
   `return-broken-stream`, `return-reset-connection` (both dedicated branches
   in `handle_retry_test_instruction`), and `return-503` (the generic
   `return-<code>` pattern, and the only one of the three that manifests as
   a clean HTTP error rather than a torn connection).

The `redirect-*` trio is gRPC-only -- README marks it `[HTTP] Unsupported`,
and a probe confirms a clean 200 over HTTP even with the header set -- so it
is covered in `trace_grpc.py` instead, driven through gRPC initial metadata
and the Retry Test API's gRPC transport.

Every step below asserts the fault actually manifested, not just that a
response came back: a corrupted-data step whose body matches the untouched
payload, or a stall/reset step that returns a clean 200, is exactly the kind
of stable-but-meaningless recording this split was written to prevent.
"""

import requests

from tests.conformance.recorder import Recorder

BUCKET = "faults-bucket"
OBJECT = "faults.bin"
# `return-broken-stream` (gcs/object.py's `rest_media`) only breaks the
# connection once 1 MiB has been sent; a payload under that threshold makes
# the instruction a silent no-op (a plain, successful 200 download) rather
# than exercising the fault. The brief's own 512 KiB was sized against the
# *other* instructions' 256 KiB threshold and does not clear this one; see
# the trace-5 report for how this was found. 2 MiB clears both thresholds.
PAYLOAD = b"0123456789abcdef" * 128 * 1024  # 2 MiB
STALL_TIMEOUT_SECONDS = 2

# Fired by setting `x-goog-emulator-instructions` directly on the triggering
# request -- no Retry Test API resource involved at all for these.
HEADER_INSTRUCTIONS = [
    "return-corrupted-data",
    "stall-always",
    "stall-at-256KiB",
    "return-503-after-256K",
    "return-503-after-256K/retry-1",
    "return-503-after-256K/retry-2",
]

# Fired through the Retry Test API: create a resource scoped to `method`,
# then send a request that is actually routed through that method's handler
# carrying `x-retry-test-id`.
RETRY_TEST_INSTRUCTIONS = [
    ("return-broken-stream", "storage.objects.get"),
    ("return-503", "storage.objects.insert"),
    ("return-reset-connection", "storage.objects.get"),
]


def run(emulator):
    rec = Recorder("faults")
    base = emulator.rest_url
    session = requests.Session()

    setup = session.post(
        base + "/storage/v1/b",
        params={"project": "test-project"},
        json={"name": BUCKET},
        timeout=30,
    )
    rec.record_http("setup-create-bucket", setup)
    upload = session.post(
        base + "/upload/storage/v1/b/%s/o" % BUCKET,
        params={"uploadType": "media", "name": OBJECT},
        data=PAYLOAD,
        headers={"Content-Type": "application/octet-stream"},
        timeout=30,
    )
    rec.record_http("setup-upload", upload)

    for instruction in HEADER_INSTRUCTIONS:
        label = instruction.replace("/", "-")
        headers = {"x-goog-emulator-instructions": instruction}
        try:
            response = session.get(
                base + "/storage/v1/b/%s/o/%s" % (BUCKET, OBJECT),
                params={"alt": "media"},
                headers=headers,
                timeout=STALL_TIMEOUT_SECONDS,
                stream=False,
            )
            if instruction == "return-corrupted-data":
                # The only header instruction that completes as a normal
                # 200; its fault is in the body, not the transport.
                assert response.content != PAYLOAD, (
                    "return-corrupted-data returned the untouched payload; "
                    "the fault did not fire"
                )
            else:
                raise AssertionError(
                    "%s returned a clean %d instead of a transport failure"
                    % (instruction, response.status_code)
                )
            rec.record_http("effect-%s" % label, response)
        except requests.exceptions.RequestException as error:
            rec.record_error("effect-%s" % label, error)

    for instruction, method in RETRY_TEST_INSTRUCTIONS:
        label = instruction.replace("/", "-")
        created = session.post(
            base + "/retry_test",
            json={"instructions": {method: [instruction]}},
            timeout=30,
        )
        rec.record_http("create-retry-test-%s" % label, created)
        if created.status_code != 200:
            # An unsupported instruction is itself a behavior worth pinning;
            # all three of these are verified supported, so reaching this
            # branch is itself a signal something regressed.
            raise AssertionError(
                "%s was rejected at retry-test creation: %s"
                % (instruction, created.text)
            )
        test_id = created.json()["id"]
        headers = {"x-retry-test-id": test_id}
        try:
            # The retry-test resource's instruction is keyed by `method`, and
            # the testbench only consumes it from a request that is actually
            # routed through that method's handler (`storage.objects.insert`
            # vs `storage.objects.get`) -- see `handle_retry_test_instruction`
            # in testbench/common.py. Always issuing a GET, regardless of
            # `method`, would leave a `storage.objects.insert`-scoped
            # instruction (`return-503` here) uninjected: the download simply
            # succeeds and the fault is never exercised.
            if method == "storage.objects.insert":
                response = session.post(
                    base + "/upload/storage/v1/b/%s/o" % BUCKET,
                    params={"uploadType": "media", "name": "%s.bin" % label},
                    data=PAYLOAD,
                    headers=dict(
                        headers, **{"Content-Type": "application/octet-stream"}
                    ),
                    timeout=STALL_TIMEOUT_SECONDS,
                )
                # return-503 manifests as a clean HTTP error, not a torn
                # connection -- the only Retry Test API instruction that does.
                assert response.status_code == 503, (
                    "return-503 returned %d instead of 503" % response.status_code
                )
                rec.record_http("effect-%s" % label, response)
            else:
                response = session.get(
                    base + "/storage/v1/b/%s/o/%s" % (BUCKET, OBJECT),
                    params={"alt": "media"},
                    headers=headers,
                    timeout=STALL_TIMEOUT_SECONDS,
                    stream=False,
                )
                raise AssertionError(
                    "%s returned a clean %d instead of a transport failure"
                    % (instruction, response.status_code)
                )
        except requests.exceptions.RequestException as error:
            rec.record_error("effect-%s" % label, error)
        status = session.get(base + "/retry_test/%s" % test_id, timeout=30)
        rec.record_http("retry-test-status-%s" % label, status)
        session.delete(base + "/retry_test/%s" % test_id, timeout=30)

    return rec.finish()
