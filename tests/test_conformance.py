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

"""Assert the emulator's external behavior matches the committed baseline.

This is the regression gate for the file backend work: the Store and Media
seams are refactors, so every trace must match byte for byte.

Skipped on Windows: the goldens are captured on Linux against gunicorn, and
fault-injection outcomes can differ under Windows' waitress server, so a
Windows run of this test would not be validating against a baseline that
applies to it -- it would just be noise on an otherwise-green job. The
dedicated `conformance` CI job runs this on ubuntu-22.04 only.

Also skipped except on Python 3.12: the goldens are byte-exact captures, and
C1 proved that is not just a Linux/Windows-server-flavor question but a
Python/zlib one too (a runtime `gzip.compress` call picked up a different
OS byte across Python versions on the *same* platform). Nothing about this
comparison is meaningful run under an interpreter the goldens were not
captured with -- it would fail (or coincidentally pass) for reasons that
have nothing to do with the emulator. The dedicated `conformance` CI job
(this module's own `python -m tests.conformance.harness` entry point) pins
Python 3.12 and is the one place this comparison is actually exercised;
this module is also collected by the 3.8-3.12 python-tests matrix job,
which is why the skip is needed here at all.
"""

import os
import sys
import unittest

from tests.conformance import harness


@unittest.skipIf(
    os.name == "nt",
    "goldens are captured on Linux; see module docstring",
)
@unittest.skipUnless(
    sys.version_info[:2] == (3, 12),
    "goldens are captured with Python 3.12; the dedicated conformance job "
    "owns this check -- see module docstring",
)
class TestConformance(unittest.TestCase):
    maxDiff = None

    def _check(self, name):
        diff = harness.verify(name)
        self.assertEqual("", diff, "conformance diff for %s:\n%s" % (name, diff))

    def test_rest_trace_matches_golden(self):
        self._check("rest")

    def test_grpc_trace_matches_golden(self):
        self._check("grpc")

    def test_faults_trace_matches_golden(self):
        self._check("faults")


if __name__ == "__main__":
    unittest.main()
