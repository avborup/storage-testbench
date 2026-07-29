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

"""Integration test for the real emulator subprocess.

Unlike test_recorder.py, which never touches a socket, this test starts a
genuine testbench subprocess: Tasks 5 and 6 depend absolutely on `Emulator`
being able to launch the emulator, serve real REST and gRPC traffic, and tear
down without leaking a process that would hold a port and break later runs.
"""

import contextlib
import os
import socket
import time
import unittest
from unittest import mock

import grpc
import requests

from google.storage.v2 import storage_pb2, storage_pb2_grpc
from tests.conformance import emulator as emulator_module
from tests.conformance.emulator import Emulator

_PORT_RELEASE_TIMEOUT_SECONDS = 5


def _port_is_free(port):
    """True if nothing is listening on 127.0.0.1:port.

    Rebinding the exact port the emulator used is a black-box way to prove no
    process -- including a gunicorn worker invisible to the harness's own
    Popen handle -- is still holding it, without shelling out to `ps` or
    depending on a platform-specific process-listing tool. SO_REUSEADDR is
    required here: the client sockets this test itself opened against the
    emulator linger in TIME_WAIT for a while after closing, and without it
    that lingering -- not a leaked process -- would make bind() fail.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with contextlib.closing(sock):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _assert_port_released(test_case, port):
    """Poll for the port to become bindable, with a deadline.

    The OS drains the sockets this test's own client connections leave
    behind (TIME_WAIT, CLOSE_WAIT) asynchronously and independently of
    whether the emulator process itself has exited; that draining is
    normal and, empirically, takes under a second, not a sign anything
    leaked. A single immediate check conflates that ordinary drain with an
    actual leaked process, so this polls up to a generous deadline instead
    of asserting instantaneously -- a real leak still fails the test, it
    just does so after the deadline rather than after zero seconds.
    """
    deadline = time.time() + _PORT_RELEASE_TIMEOUT_SECONDS
    while time.time() < deadline:
        if _port_is_free(port):
            return
        time.sleep(0.1)
    test_case.fail(
        "port %d still held %ds after the emulator was torn down"
        % (port, _PORT_RELEASE_TIMEOUT_SECONDS)
    )


class TestEmulator(unittest.TestCase):
    def test_serves_rest_and_grpc_then_leaves_no_process_behind(self):
        with Emulator() as emu:
            response = requests.get(emu.rest_url + "/")
            self.assertEqual(200, response.status_code)
            self.assertEqual("OK", response.text)

            created = requests.post(
                emu.rest_url + "/storage/v1/b",
                params={"project": "test-project"},
                json={"name": "probe-bucket"},
            )
            self.assertEqual(200, created.status_code, created.text)

            channel = grpc.insecure_channel(emu.grpc_target)
            grpc.channel_ready_future(channel).result(timeout=10)
            stub = storage_pb2_grpc.StorageStub(channel)
            got = stub.GetBucket(
                storage_pb2.GetBucketRequest(name="projects/_/buckets/probe-bucket")
            )
            self.assertEqual("projects/_/buckets/probe-bucket", got.name)
            channel.close()

            rest_port, grpc_port = emu.rest_port, emu.grpc_port

        # The context manager has returned: both ports must become free
        # again, proving teardown did not leak a process (this repo has
        # leaked one before).
        _assert_port_released(self, rest_port)
        _assert_port_released(self, grpc_port)

    def test_start_grpc_failure_does_not_leak_the_process(self):
        # Python never calls __exit__ when __enter__ itself raises, so if
        # _start_grpc (or _await_rest) fails after the subprocess is already
        # up, __enter__ has to reap it itself or the gunicorn master and
        # worker survive, orphaned, holding the port -- the exact failure
        # mode a leaked emulator process causes for later tasks.
        emulator = Emulator()
        with mock.patch.object(
            Emulator, "_start_grpc", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                with emulator:
                    self.fail("__enter__ should have raised before yielding")

        self.assertIsNotNone(
            emulator._process, "no subprocess was even started before the failure"
        )
        self.assertIsNotNone(
            emulator._process.poll(),
            "child process is still running after __enter__ raised",
        )
        _assert_port_released(self, emulator.rest_port)

    def test_a_poisoned_parent_environment_does_not_reach_the_child(self):
        # A developer's or CI runner's shell must not be able to change a
        # recorded golden. testbench/acl.py reads
        # GOOGLE_CLOUD_CPP_STORAGE_EMULATOR_PROJECT_NUMBER at import time and
        # it lands verbatim in a bucket's `projectNumber` field, which
        # canonicalize.py's NONDETERMINISTIC_FIELDS has no rule for.
        # testbench/database.py separately reads
        # GOOGLE_CLOUD_CPP_STORAGE_TEST_BUCKET_NAME on (almost) every request
        # to auto-create a bucket by that name. Both must be neutralized
        # regardless of what the parent process has set.
        poisoned = {
            "GOOGLE_CLOUD_CPP_STORAGE_EMULATOR_PROJECT_NUMBER": "999999999",
            "GOOGLE_CLOUD_CPP_STORAGE_TEST_BUCKET_NAME": "leaked-bucket",
        }
        with mock.patch.dict(os.environ, poisoned):
            with Emulator() as emu:
                requests.post(
                    emu.rest_url + "/storage/v1/b",
                    params={"project": "test-project"},
                    json={"name": "probe-bucket"},
                )
                got = requests.get(emu.rest_url + "/storage/v1/b/probe-bucket")
                self.assertEqual(200, got.status_code, got.text)
                self.assertEqual("123456789", got.json()["projectNumber"])

                listed = requests.get(
                    emu.rest_url + "/storage/v1/b", params={"project": "test-project"}
                )
                self.assertEqual(200, listed.status_code, listed.text)
                names = [b["name"] for b in listed.json().get("items", [])]
                self.assertEqual(["probe-bucket"], names)


class TestPortCollisionGuard(unittest.TestCase):
    """`rest_port` and `grpc_port` are drawn independently; a collision must
    not silently bind both listeners to the same port."""

    def test_collision_between_two_auto_drawn_ports_is_redrawn(self):
        with mock.patch.object(
            emulator_module, "free_port", side_effect=[4000, 4000, 5000]
        ):
            emu = Emulator()
        self.assertEqual(4000, emu.rest_port)
        self.assertEqual(5000, emu.grpc_port)

    def test_caller_pinning_both_ports_to_the_same_value_raises(self):
        with self.assertRaises(ValueError):
            Emulator(rest_port=4000, grpc_port=4000)


class TestLogsSurviveTeardown(unittest.TestCase):
    """`logs()` must stay callable across the point where `_terminate()`
    closes the underlying file -- it used to raise `ValueError: read of
    closed file` instead of returning the last snapshot it captured."""

    def test_logs_after_terminate_does_not_raise_and_is_idempotent(self):
        with Emulator() as emu:
            requests.get(emu.rest_url + "/")
        # Must not raise, unlike before this fix; and repeated calls after
        # teardown must keep returning the same cached snapshot rather than
        # re-reading (and failing on) the now-closed file each time.
        after = emu.logs()
        self.assertIn("gunicorn", after)
        self.assertEqual(after, emu.logs())


class _FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError("status %d" % self.status_code)


class TestStartGrpcRetry(unittest.TestCase):
    """`_start_grpc` in isolation, with `requests.get` mocked out entirely.

    No real subprocess is involved: Task 5 observed one bare `HTTPError` from
    `/start_grpc` in 90+ launches, never reproduced on demand, so there is no
    way to force the real flake. These tests instead pin down the retry
    *policy* -- transient failures are retried up to a bound, a genuine port
    mismatch is not -- against a fake `requests.get`.
    """

    def setUp(self):
        self.emulator = Emulator(rest_port=12345, grpc_port=54321)
        # Real delays would make this test slow for no benefit; the retry
        # count, not the wait, is what is under test.
        patcher = mock.patch.object(emulator_module.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_retries_transient_failure_then_succeeds(self):
        responses = [
            requests.exceptions.ConnectionError("connection refused"),
            _FakeResponse("54321"),
        ]
        with mock.patch.object(
            emulator_module.requests, "get", side_effect=responses
        ) as get:
            self.emulator._start_grpc()  # must not raise
        self.assertEqual(2, get.call_count)

    def test_gives_up_after_bounded_retries(self):
        error = requests.exceptions.ConnectionError("connection refused")
        with mock.patch.object(
            emulator_module.requests, "get", side_effect=error
        ) as get:
            with self.assertRaises(RuntimeError):
                self.emulator._start_grpc()
        self.assertEqual(emulator_module._START_GRPC_ATTEMPTS, get.call_count)

    def test_port_mismatch_is_not_retried(self):
        # A real mismatch is a bug, not a flake: it must surface on the very
        # first attempt rather than being retried away.
        with mock.patch.object(
            emulator_module.requests, "get", return_value=_FakeResponse("1")
        ) as get:
            with self.assertRaises(AssertionError):
                self.emulator._start_grpc()
        self.assertEqual(1, get.call_count)


if __name__ == "__main__":
    unittest.main()
