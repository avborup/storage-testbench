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
import tempfile
import time

import requests

# A loaded CI runner is slower than a developer's laptop to fork gunicorn's
# worker and bind a socket; Task 5 observed a 30s timeout fail once in 90+
# launches on this machine with plenty of headroom to spare, so the default
# here is doubled and left overridable per-environment without a code change.
_STARTUP_TIMEOUT_SECONDS = int(
    os.environ.get("TESTBENCH_CONFORMANCE_STARTUP_TIMEOUT_SECONDS", "60")
)

# `/start_grpc` is idempotent (testbench/rest_server.py guards it with
# `if grpc_port == 0` and returns the already-chosen port on a repeat call),
# so a transient failure -- Task 5 also observed one bare `HTTPError` here in
# 90+ launches, never reproduced in 45+ targeted retries -- is safe to retry.
# The bound keeps a *genuine* failure (the emulator wedged, or a real
# port mismatch) from retrying forever; a port mismatch is not a transient
# failure and is never retried; see `_start_grpc` below.
_START_GRPC_ATTEMPTS = 3
_START_GRPC_RETRY_DELAY_SECONDS = 1

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
    # Pinned, not inherited, despite `_INHERITED_ENV` below also listing
    # LANG/LC_ALL: inheriting is what the old comment there claimed gave
    # "stable text encoding", which is backwards -- a developer's or CI
    # runner's locale is exactly what varies. Pinning here means whatever
    # is in `_INHERITED_ENV` for these two names is always overridden.
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    # The one remaining ordering hazard the two-run determinism test can't
    # tell apart from a real regression: dict iteration order is guaranteed
    # by insertion in CPython, but a handful of places iterate a `set` (e.g.
    # ACL entities, listing prefixes), whose order depends on hash values,
    # which depend on PYTHONHASHSEED when it is randomized (the default).
    "PYTHONHASHSEED": "0",
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
    "VIRTUAL_ENV",  # keep the venv's interpreter consistent
    # LANG/LC_ALL are deliberately absent here: they are pinned in
    # `_PINNED_ENV` instead of inherited, for stable text encoding that
    # actually is stable regardless of the parent shell's locale.
    #
    # Coverage-only: forwarded so the gunicorn worker can start subprocess
    # coverage under the media call-site gate (tests/test_media_call_sites.py
    # sets it to the abs path of .coveragerc, which testbench/__init__.py
    # reads to call coverage.process_startup()). A no-op when unset, so
    # normal emulator operation and the goldens are unaffected.
    "COVERAGE_PROCESS_START",
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
    # `testbench_run.py` launches gunicorn (on POSIX) by bare name via
    # `subprocess.run(["gunicorn", ...])`, which is resolved against *this*
    # child's PATH, not against `sys.executable`. Running this file's own
    # interpreter directly (`./.venv/bin/python3 -m pytest ...`, without
    # activating the venv first) leaves the venv's `bin/` off the inherited
    # PATH, so the child would fail to find `gunicorn` with a misleading
    # FileNotFoundError. Prepending the directory `sys.executable` lives in
    # covers both that case and the already-activated case (where it is a
    # harmless duplicate of what activation already put on PATH).
    venv_bin = os.path.dirname(sys.executable)
    inherited_path = env.get("PATH", "")
    env["PATH"] = venv_bin + os.pathsep + inherited_path if inherited_path else venv_bin
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
        if self.rest_port == self.grpc_port:
            # `free_port()` draws two independent ephemeral ports; a
            # collision is unlikely but not impossible, and binding both
            # listeners to the same port would make one of them fail to
            # start with a confusing bind error instead of this clear one.
            # Only redraw a port this call itself picked -- if the caller
            # pinned both explicitly to the same value, that is their bug to
            # surface, not ours to silently paper over.
            if grpc_port is None:
                self.grpc_port = free_port()
            elif rest_port is None:
                self.rest_port = free_port()
            else:
                raise ValueError(
                    "rest_port and grpc_port must differ, both were %d" % rest_port
                )
            assert self.rest_port != self.grpc_port
        self._process = None
        self._stdout_file = None
        self._logs_cache = ""

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
        # A real file, not `subprocess.PIPE`: a pipe has a small, fixed OS
        # buffer (16 KiB on macOS), and nothing reads it until `logs()` is
        # called after the trace finishes. gunicorn's `--access-logfile=-`
        # writes a line per request, so a long enough trace fills the pipe,
        # and the worker then blocks on `write()` -- wedging the emulator
        # mid-trace with its own diagnostics stuck unread in the pipe. A
        # `TemporaryFile` has no such ceiling.
        self._stdout_file = tempfile.TemporaryFile()
        self._process = subprocess.Popen(
            [sys.executable, _TESTBENCH_RUN, "127.0.0.1", str(self.rest_port), "10"],
            cwd=_REPO_ROOT,
            env=env,
            stdout=self._stdout_file,
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
        if self._stdout_file is not None:
            # Snapshot whatever the child wrote before closing the handle:
            # `logs()` must keep working (returning this snapshot) even
            # after teardown, rather than raising on a closed file.
            try:
                self._stdout_file.seek(0)
                self._logs_cache = self._stdout_file.read().decode("utf-8", "replace")
            except ValueError:
                pass
            self._stdout_file.close()

    def logs(self):
        if self._stdout_file is None:
            return self._logs_cache
        try:
            self._stdout_file.seek(0)
            return self._stdout_file.read().decode("utf-8", "replace")
        except ValueError:
            # Already closed by `_terminate()`; the cache it left behind is
            # the most recent content available, and returning it (rather
            # than raising) is what makes `logs()` safe to call at any point
            # in the emulator's lifecycle, including after teardown.
            return self._logs_cache

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
                pass
            # Applies whether the last attempt raised or came back with a
            # non-200: a server that is up but unhealthy (e.g. answering
            # 500) must not be polled in a tight loop, which the previous
            # placement of this sleep -- only inside the `except` -- did.
            time.sleep(0.1)
        # Terminate before formatting the message, not after: the caller
        # (`__enter__`) also tears down on any exception from here, but by
        # then this message would already be frozen without the child's
        # diagnostics. Terminating here first, and letting `_terminate()`
        # snapshot the logs before it closes the file, is what makes them
        # available to be embedded below -- this used to be the one failure
        # mode (a genuine readiness timeout) that reported nothing but a
        # bare line, which is exactly the flake this harness has seen.
        self._terminate()
        raise RuntimeError(
            "emulator did not become ready within %ds:\n%s"
            % (_STARTUP_TIMEOUT_SECONDS, self.logs())
        )

    def _start_grpc(self):
        # The gRPC server must be started inside the serving process so that
        # it shares one Database; see the comment at rest_server.start_grpc.
        #
        # Bounded retry on transient request failures only: a genuine
        # port mismatch is a real bug, not a flake, and must not be retried
        # away, so the assertion below is outside the except clause and
        # propagates immediately on the first attempt that gets a response.
        last_error = None
        for attempt in range(_START_GRPC_ATTEMPTS):
            try:
                response = requests.get(
                    self.rest_url + "/start_grpc",
                    params={"port": self.grpc_port},
                    timeout=10,
                )
                response.raise_for_status()
            except requests.exceptions.RequestException as error:
                last_error = error
                if attempt + 1 < _START_GRPC_ATTEMPTS:
                    time.sleep(_START_GRPC_RETRY_DELAY_SECONDS)
                continue
            reported = int(response.text)
            assert reported == self.grpc_port, "gRPC started on %d, wanted %d" % (
                reported,
                self.grpc_port,
            )
            return
        raise RuntimeError(
            "could not start gRPC after %d attempts: %s"
            % (_START_GRPC_ATTEMPTS, last_error)
        )
