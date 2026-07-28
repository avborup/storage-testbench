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


if __name__ == "__main__":
    unittest.main()
