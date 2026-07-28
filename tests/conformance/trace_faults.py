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

INSTRUCTIONS = [
    ("return-broken-stream", "storage.objects.get"),
    ("return-corrupted-data", "storage.objects.get"),
    ("stall-always", "storage.objects.get"),
    ("stall-at-256KiB", "storage.objects.get"),
    ("return-503-after-256K", "storage.objects.get"),
    ("return-503-after-256K/retry-1", "storage.objects.get"),
    ("return-503-after-256K/retry-2", "storage.objects.get"),
    ("redirect-send-token-T", "storage.objects.get"),
    ("redirect-send-handle-and-token-T", "storage.objects.get"),
    ("redirect-expect-token-T", "storage.objects.get"),
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

    for instruction, method in INSTRUCTIONS:
        label = instruction.replace("/", "-")
        created = session.post(
            base + "/retry_test",
            json={"instructions": {method: [instruction]}},
            timeout=30,
        )
        if created.status_code != 200:
            # An unsupported instruction is itself a behavior worth pinning.
            rec.record_http("create-retry-test-%s" % label, created)
            continue
        rec.record_http("create-retry-test-%s" % label, created)
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
            else:
                response = session.get(
                    base + "/storage/v1/b/%s/o/%s" % (BUCKET, OBJECT),
                    params={"alt": "media"},
                    headers=headers,
                    timeout=STALL_TIMEOUT_SECONDS,
                    stream=False,
                )
            rec.record_http("effect-%s" % label, response)
        except requests.exceptions.RequestException as error:
            rec.record_error("effect-%s" % label, error)
        status = session.get(base + "/retry_test/%s" % test_id, timeout=30)
        rec.record_http("retry-test-status-%s" % label, status)
        session.delete(base + "/retry_test/%s" % test_id, timeout=30)

    return rec.finish()
