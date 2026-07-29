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

"""Coverage gate for the media call-site audit.

Every uncommented `path:line` in ``tests/media_call_sites.txt`` must be
executed by the conformance trace. A new ``.media`` migration added without
trace coverage fails here -- converting the ~71-site audit from a claim into a
check.

Subprocess-coverage caveat and the chosen resolution
-----------------------------------------------------
The conformance ``Emulator`` runs the app in a gunicorn worker that is a
*grandchild* of this test process (Popen -> testbench_run.py -> gunicorn master
-> forked worker). Every ``.media`` call site executes only in that worker, so
an in-process ``Coverage`` here would see none of them -- a green gate over an
empty measurement.

We resolve it with option (a) from the task brief: subprocess coverage.
``setUpClass`` sets ``COVERAGE_PROCESS_START`` to the absolute path of
``.coveragerc`` *before* launching any emulator; ``tests/conformance/emulator.py``
forwards it through its env allowlist, and ``testbench/__init__.py`` calls
``coverage.process_startup()`` in the worker. ``.coveragerc`` has
``parallel = true`` (each process writes its own ``.coverage.<host>.<pid>``) and
``sigterm = true`` (so the worker flushes when the emulator tears it down with
``os.killpg(SIGTERM)`` -- without it the worker's atexit save never runs and no
data is written). After the traces run we ``combine()`` the parallel data files
and assert against the *combined* data, not the near-empty in-process data.
"""

import glob
import os
import unittest

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST = os.path.join(REPO, "tests", "media_call_sites.txt")
COVERAGERC = os.path.join(REPO, ".coveragerc")
# Anchor every data file at the repo root so the in-process save, the worker's
# save (its cwd is the repo root) and combine() all agree regardless of the
# directory pytest was launched from.
DATA_FILE = os.path.join(REPO, ".coverage")


def _listed_sites():
    sites = []
    with open(LIST) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            path, num = line.rsplit(":", 1)
            sites.append((path, int(num)))
    return sites


def _erase_data_files():
    # Only the data files (exact-name .coverage and the parallel
    # .coverage.<host>.<pid> siblings) -- never .coveragerc.
    for f in glob.glob(DATA_FILE) + glob.glob(DATA_FILE + ".*"):
        os.remove(f)


@pytest.mark.skipif(
    os.name == "nt", reason="coverage line numbers pinned on the Linux/macOS capture"
)
class TestMediaCallSitesAreCovered(unittest.TestCase):
    """Every media call site in the committed audit must be executed by the
    conformance trace. A new `.media` migration added without trace coverage
    fails here -- converting the ~71-site audit from a claim into a check."""

    @classmethod
    def setUpClass(cls):
        import coverage

        cls._prev_process_start = os.environ.get("COVERAGE_PROCESS_START")
        # Set BEFORE any Emulator spawns: the worker reads it at import time.
        os.environ["COVERAGE_PROCESS_START"] = COVERAGERC
        _erase_data_files()

        cov = coverage.Coverage(config_file=COVERAGERC, data_file=DATA_FILE)
        cov.start()
        # Each trace runs against its own freshly-launched emulator subprocess;
        # the worker records gcs/ and testbench/ lines to its own parallel data
        # file via coverage.process_startup().
        from tests.conformance import trace_faults, trace_grpc, trace_rest
        from tests.conformance.emulator import Emulator

        for module in (trace_rest, trace_grpc, trace_faults):
            with Emulator() as emu:
                module.run(emu)
        cov.stop()
        cov.save()

        # Combine the parallel data files (this process's plus every worker's)
        # into one dataset and read *that* -- the in-process data alone does
        # not contain the worker's lines.
        combined = coverage.Coverage(config_file=COVERAGERC, data_file=DATA_FILE)
        combined.combine()
        cls.data = combined.get_data()

    @classmethod
    def tearDownClass(cls):
        _erase_data_files()
        if cls._prev_process_start is None:
            os.environ.pop("COVERAGE_PROCESS_START", None)
        else:
            os.environ["COVERAGE_PROCESS_START"] = cls._prev_process_start

    def test_every_listed_site_executed(self):
        missing = []
        for path, num in _listed_sites():
            abspath = os.path.join(REPO, path)
            executed = set(self.data.lines(abspath) or [])
            if num not in executed:
                missing.append("%s:%d" % (path, num))
        self.assertEqual([], missing, "media call sites never executed: %s" % missing)
