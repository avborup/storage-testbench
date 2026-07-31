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

"""Mechanism-1 backend switch for the test suite.

Keyed on ``TESTBENCH_TEST_STORE=memory|file`` (default memory). In file mode an
autouse fixture swaps the store on the live ``testbench.rest_server.db``
singleton to a ``FileStore`` rooted at a fresh per-test temp dir, so every
Flask-client endpoint test (and the disk-touch guard) drives real, contained
disk writes. The conformance harness (a separate process, env-driven) and the
explicit ``FileStore`` unit suites (``tests/test_filestore*.py``) cover the
rest of the file-backend surface.

Deliberately NOT overriding ``Database.init``: the plan sketch monkeypatched it
so no-store ``init()`` callers became file-backed too, but that is provably
irreconcilable with ``tests/test_store.py::test_default_store_is_a_null_store``,
which asserts the PRODUCTION default (``Database.init().store`` is a
``NullStore``) -- a fact no store-injecting override can satisfy. The override
also forced store-agnostic ``Database``/servicer unit tests onto ``FileStore``,
where they exercised an out-of-scope object-layout gap (a GCS object named
``x`` and another named ``x/y`` cannot coexist as a file and a directory on a
real filesystem). Both are orthogonal to this task's wiring goal, so the
singleton swap alone is the correct, green Mechanism-1 switch.
"""

import os
import shutil
import tempfile

import pytest

import testbench.database
import testbench.rest_server

_ACTIVE_ROOT = {"path": None}

# The file backend is POSIX-only and explicitly out of scope on Windows (it
# requires openat/O_NOFOLLOW/dir_fd and os.replace(src_dir_fd=, dst_dir_fd=),
# none of which Windows provides). Do not even COLLECT its test modules there:
# a module-level skipif cannot prevent test_pathing.py's top-level
# `from hypothesis import given` from erroring at import time, and
# test_sidecar/test_filestore*/wiring exercise fd-based ops that raise on
# Windows. Skipping collection keeps the Windows leg a pure memory-backend run
# (the memory backend must stay unaffected) and avoids adding hypothesis to the
# Windows CI env for tests that would only be skipped anyway.
if os.name == "nt":
    # Glob patterns (collect_ignore_glob) so new file-backend test modules are
    # excluded on Windows without editing this list each time. test_media_base.py
    # is deliberately NOT matched: it tests the Media base + Store factory on the
    # memory path (NullStore -> BytesMedia) with no hypothesis/FileMedia/fd deps.
    collect_ignore_glob = [
        "tests/test_pathing.py",
        "tests/test_containment.py",
        "tests/test_sidecar.py",
        "tests/test_filestore*.py",  # test_filestore.py, _scan.py, _filemedia.py
        "tests/test_filemedia*.py",  # all FileMedia read/staging/upload/download/... suites
        "tests/test_appendable_filemedia.py",
        "tests/test_file_backend_wiring.py",
    ]


@pytest.fixture
def file_root():
    return _ACTIVE_ROOT["path"]


@pytest.fixture(autouse=True)
def _backend():
    if os.environ.get("TESTBENCH_TEST_STORE", "memory") != "file":
        yield
        return
    root = tempfile.mkdtemp(prefix="testbench-unit-")
    _ACTIVE_ROOT["path"] = root
    from testbench.filestore import FileStore

    # Swap the store on the LIVE singleton so Flask-client endpoint tests hit
    # disk. Same object -> retry_test's closure and gRPC's shared db stay valid.
    db = testbench.rest_server.db
    prev_store = db._store
    db._store = FileStore(root)
    db.clear()  # cleared() wipes the (empty) tree; index reset; env bucket re-seeded to disk
    try:
        yield
    finally:
        db._store = prev_store
        _ACTIVE_ROOT["path"] = None
        shutil.rmtree(root, ignore_errors=True)
