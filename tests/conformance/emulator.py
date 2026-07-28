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

"""Start and stop a testbench subprocess for black-box tracing."""

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time

import requests

_STARTUP_TIMEOUT_SECONDS = 30

# tests/conformance/emulator.py sits two directories below the repository
# root (tests/, then tests/conformance/). Three `dirname()` calls over its
# absolute path reach the root: the first strips the filename, the other
# two climb out of tests/conformance/. Resolving it this way, rather than
# from the caller's current working directory, means the launcher works
# regardless of where the test process was started from.
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_TESTBENCH_RUN = os.path.join(_REPO_ROOT, "testbench_run.py")

# The emulator's own knobs, pinned to the defaults in testbench/acl.py.
# These are read at import time and land in the owner/entity/project-number
# fields of recorded bucket and object bodies; the canonicalizer has no rule
# for those fields (they are not in NONDETERMINISTIC_FIELDS), so an inherited
# value -- rather than this pinned default -- would silently change the
# goldens for anyone whose shell happens to set these differently.
_PINNED_ENV = {
    "GOOGLE_CLOUD_CPP_STORAGE_EMULATOR_PROJECT_NUMBER": "123456789",
    "GOOGLE_CLOUD_CPP_STORAGE_EMULATOR_OBJECT_OWNER_ENTITY": (
        "user-object.owners@example.com"
    ),
    "GOOGLE_CLOUD_CPP_STORAGE_EMULATOR_OBJECT_READER_ENTITY": (
        "user-object.viewers@example.com"
    ),
}

# Only what the child needs to execute Python and bind a socket. Anything
# not named here is deliberately not forwarded -- in particular
# GOOGLE_CLOUD_CPP_STORAGE_TEST_BUCKET_NAME (testbench/database.py:216)
# would auto-create a bucket and add it to every trace; its documented
# default is to be unset, so there is nothing to pin, only to withhold.
_INHERITED_ENV = (
    "PATH",  # locate the interpreter and gunicorn
    "SYSTEMROOT",  # Windows: required for socket calls
    "TEMP",
    "TMP",
    "TMPDIR",  # gunicorn/waitress scratch files
    "HOME",
    "USERPROFILE",  # libraries that resolve a home dir on import
    "LANG",
    "LC_ALL",  # stable text encoding
    "VIRTUAL_ENV",  # keep the venv's interpreter consistent
)


def _child_env():
    """Build the emulator subprocess's environment explicitly.

    Copying the parent's environment wholesale would let anything a
    developer or a CI runner happens to have set reach the emulator; for
    the three GOOGLE_CLOUD_CPP_STORAGE_EMULATOR_* variables that means
    silently different goldens (see `_PINNED_ENV`). Only a fixed allowlist
    is forwarded, plus the emulator's own knobs pinned to their documented
    defaults, plus PYTHONPATH so `testbench` is importable.
    """
    env = {name: os.environ[name] for name in _INHERITED_ENV if name in os.environ}
    env.update(_PINNED_ENV)
    env["PYTHONPATH"] = _REPO_ROOT
    return env


def free_port():
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Emulator:
    """A testbench subprocess reachable over REST and gRPC.

    The emulator runs as a subprocess rather than in-process so that the
    trace is genuinely black-box: nothing the harness does can perturb the
    server's state except through its API.
    """

    def __init__(self, rest_port=None, grpc_port=None):
        self.rest_port = rest_port or free_port()
        self.grpc_port = grpc_port or free_port()
        self._process = None

    @property
    def rest_url(self):
        return "http://127.0.0.1:%d" % self.rest_port

    @property
    def grpc_target(self):
        return "127.0.0.1:%d" % self.grpc_port

    def __enter__(self):
        env = _child_env()
        # `testbench_run.py` picks the server for us: gunicorn with
        # --reload on POSIX, waitress on Windows (see its own
        # platform.system() check). Launching `python -m testbench` directly
        # was tried first, guarded by WERKZEUG_RUN_MAIN=true to make
        # Werkzeug's reloader serve in-process; that assumption was wrong for
        # the pinned Werkzeug (3.0.4), which requires a real, already-bound
        # socket fd via WERKZEUG_SERVER_FD whenever WERKZEUG_RUN_MAIN is set,
        # and raises KeyError without one. `testbench_run.py` sidesteps
        # Werkzeug's reloader (and that requirement) entirely.
        #
        # On POSIX, gunicorn's worker is itself forked from the gunicorn
        # master, which is a grandchild of this Popen and therefore invisible
        # to it. `start_new_session` makes this Popen's process the leader of
        # a new session/process group; gunicorn does not daemonize (no
        # --daemon flag) or otherwise detach, so the master and its worker
        # stay in that same group and can be reached as a group on teardown
        # -- see `_terminate`. `start_new_session` is a no-op on Windows,
        # where there is only the one waitress process to begin with.
        self._process = subprocess.Popen(
            [sys.executable, _TESTBENCH_RUN, "127.0.0.1", str(self.rest_port), "10"],
            cwd=_REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            self._await_rest()
            self._start_grpc()
        except BaseException:
            # Python never calls __exit__ when __enter__ itself raises, so
            # if readiness times out or /start_grpc fails, teardown has to
            # happen right here or the gunicorn master and worker survive,
            # orphaned, holding the port. Catching BaseException (not just
            # Exception) means a KeyboardInterrupt during startup reaps the
            # tree too.
            self._terminate()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._process is not None:
            self._terminate()
        return False

    def _terminate(self):
        proc = self._process
        if os.name == "nt":
            # A single waitress process; no children to worry about.
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        else:
            # Signal the whole process group (this process plus gunicorn's
            # master and worker) rather than just `proc`: terminating only
            # `proc` would leave gunicorn holding the port, exactly the
            # leaked-emulator failure mode this harness must not repeat.
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = None
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            # `proc.wait` always runs, even if the group was already gone by
            # the time it was signalled, so `proc` is reaped either way.
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if pgid is not None:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()

    def logs(self):
        if self._process is None or self._process.stdout is None:
            return ""
        return self._process.stdout.read().decode("utf-8", "replace")

    def _await_rest(self):
        deadline = time.time() + _STARTUP_TIMEOUT_SECONDS
        while time.time() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("emulator exited early:\n%s" % self.logs())
            try:
                response = requests.get(self.rest_url + "/", timeout=1)
                if response.status_code == 200:
                    return
            except requests.exceptions.RequestException:
                time.sleep(0.1)
        raise RuntimeError(
            "emulator did not become ready within %ds" % _STARTUP_TIMEOUT_SECONDS
        )

    def _start_grpc(self):
        # The gRPC server must be started inside the serving process so that
        # it shares one Database; see the comment at rest_server.start_grpc.
        response = requests.get(
            self.rest_url + "/start_grpc", params={"port": self.grpc_port}, timeout=10
        )
        response.raise_for_status()
        reported = int(response.text)
        assert reported == self.grpc_port, "gRPC started on %d, wanted %d" % (
            reported,
            self.grpc_port,
        )
