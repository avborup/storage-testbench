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

Determinism (two hardenings over the naive one-shot combine)
------------------------------------------------------------
1. *Isolation.* Every data file lives in a private per-run temp dir pointed at
   by ``COVERAGE_FILE`` (forwarded to the worker through the emulator env
   allowlist), never the shared repo root. A stale ``.coverage.*`` or a
   concurrently-spawned emulator can then neither contaminate this measurement
   nor be clobbered by it.
2. *Late-flush tolerance.* The emulator reaps the gunicorn *master* with
   ``proc.wait``, but the coverage-writing *worker* is a grandchild that
   flushes on SIGTERM; under load that flush can land just after the master
   exits. A single one-shot ``combine()`` therefore occasionally raced the
   worker and saw a not-yet-written data file, reddening the gate spuriously.
   We instead retry ``combine()`` on the SAME (accumulating) ``Coverage``
   object until every listed site is present or a bounded deadline passes. A
   genuinely uncovered site never appears no matter how long we wait, so the
   gate still fails correctly -- only flush LATENCY is tolerated, never a
   missing site.
"""

import os
import shutil
import tempfile
import time
import unittest

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST = os.path.join(REPO, "tests", "media_call_sites.txt")
COVERAGERC = os.path.join(REPO, ".coveragerc")

# Upper bound on how long we wait for a slow worker SIGTERM-flush to land. Only
# ever fully spent on a genuine miss (an uncovered site never appears), so it
# does not slow the common green path.
_COMBINE_DEADLINE_SECONDS = 30.0


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


def _missing_sites(sites, data):
    missing = []
    for path, num in sites:
        abspath = os.path.join(REPO, path)
        executed = set(data.lines(abspath) or [])
        if num not in executed:
            missing.append("%s:%d" % (path, num))
    return missing


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
        cls._prev_coverage_file = os.environ.get("COVERAGE_FILE")

        # Isolate every data file in a private temp dir (worker inherits
        # COVERAGE_FILE via the emulator env allowlist). Set BEFORE any Emulator
        # spawns: the worker reads both env vars at import time.
        cls._cov_dir = tempfile.mkdtemp(prefix="media-cov-")
        data_file = os.path.join(cls._cov_dir, ".coverage")
        os.environ["COVERAGE_PROCESS_START"] = COVERAGERC
        os.environ["COVERAGE_FILE"] = data_file

        cov = coverage.Coverage(config_file=COVERAGERC, data_file=data_file)
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
        # not contain the worker's lines. Retry on the SAME accumulating object
        # so a late worker flush is absorbed once its file lands (see the
        # module docstring's late-flush note); stop as soon as every site is
        # covered, or when the bounded deadline passes.
        combined = coverage.Coverage(config_file=COVERAGERC, data_file=data_file)
        sites = _listed_sites()
        deadline = time.monotonic() + _COMBINE_DEADLINE_SECONDS
        while True:
            combined.combine(strict=False, keep=False)
            data = combined.get_data()
            if not _missing_sites(sites, data) or time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        cls.data = data

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._cov_dir, ignore_errors=True)
        for name, prev in (
            ("COVERAGE_PROCESS_START", cls._prev_process_start),
            ("COVERAGE_FILE", cls._prev_coverage_file),
        ):
            if prev is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prev

    def test_every_listed_site_executed(self):
        missing = _missing_sites(_listed_sites(), self.data)
        self.assertEqual([], missing, "media call sites never executed: %s" % missing)
