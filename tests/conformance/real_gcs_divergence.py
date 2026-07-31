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

"""Mechanism 8 (opt-in): cross-validate the conformance trace against a REAL
GCS bucket and report divergences.

NOT run in CI. It needs a real project, credentials, and money, so it is a
MANUALLY triggered job (spec Verification plan, Mechanism 8). It is INTENDED to
reuse the same black-box trace scripts and canonicalization as the emulator
harness, driving purely over the wire (importing NO testbench/gcs internals,
exactly like tests/conformance/harness.py, so the harness import rule is
preserved). That trace-driving body is INTENTIONALLY LEFT UNIMPLEMENTED: it is
operator-run and out of scope for autonomous CI, per the design.

The ONLY executed behaviour is the credential-less SKIP below (returns 0), which
keeps the module importable and CI-safe as a no-op. With TESTBENCH_REAL_GCS_
PROJECT set it exits with a "wire it here" SystemExit rather than silently
pretending to have run.

Run:  TESTBENCH_REAL_GCS_PROJECT=my-proj \\
      GOOGLE_APPLICATION_CREDENTIALS=/path/key.json \\
      PYTHONPATH=. .venv/bin/python -m tests.conformance.real_gcs_divergence
"""

import os
import sys


def main():
    project = os.environ.get("TESTBENCH_REAL_GCS_PROJECT")
    if not project:
        print(
            "SKIP: real-GCS divergence is a manual job; set "
            "TESTBENCH_REAL_GCS_PROJECT (+ credentials) to run it."
        )
        return 0
    # The trace-driving body (run trace_rest/trace_grpc against the real REST/
    # gRPC endpoint, apply the harness canonicalizer, print a per-interaction
    # divergence report) is intentionally NOT implemented here: it is an
    # operator-run job, not a CI gate, and known divergences (ACL/IAM
    # non-enforcement, signed-URL non-verification) are recorded as KNOWN gaps,
    # not failures. See README "Verification: manual/external jobs".
    raise SystemExit(
        "real-GCS divergence harness is operator-run and unimplemented: wire "
        "trace_rest/trace_grpc at the real endpoint here. See README."
    )


if __name__ == "__main__":
    sys.exit(main())
