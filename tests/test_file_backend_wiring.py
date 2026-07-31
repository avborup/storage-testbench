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

"""End-to-end wiring guards for the file backend (Task 9).

* The disk-touch guard proves the file leg is NOT vacuous: a bucket created
  through the live Flask app must land as ``<root>/<bucket>/.gcs/bucket.json``
  on the per-test on-disk tree. It only runs under ``TESTBENCH_TEST_STORE=file``.
* The loopback-bind guards prove the traversal-capable file backend never
  listens off-loopback: gRPC binds ``127.0.0.1`` and the REST bootstrap refuses
  a non-loopback ``--bind`` host. These run in both legs (they drive the env
  directly) so a regression is caught under either configuration.
"""

import os
import sys

import pytest

from testbench import grpc_server, rest_server


@pytest.mark.skipif(
    os.environ.get("TESTBENCH_TEST_STORE") != "file", reason="file leg only"
)
def test_bucket_create_writes_bucket_json(file_root):
    client = rest_server.server.test_client()
    response = client.post(
        "/storage/v1/b",
        query_string={"project": "test-project"},
        json={"name": "disk-touch-bucket"},
    )
    assert response.status_code == 200
    assert file_root is not None
    assert os.path.exists(
        os.path.join(file_root, "disk-touch-bucket", ".gcs", "bucket.json")
    )


def test_file_backend_binds_loopback(monkeypatch):
    monkeypatch.setenv("TESTBENCH_STORE", "file")
    assert grpc_server._bind_host() == "127.0.0.1"
    monkeypatch.setenv("TESTBENCH_STORE", "memory")
    assert grpc_server._bind_host() == "0.0.0.0"
    monkeypatch.delenv("TESTBENCH_STORE", raising=False)
    assert grpc_server._bind_host() == "0.0.0.0"


def test_rest_bootstrap_refuses_non_loopback_bind_under_file_backend(monkeypatch):
    import testbench
    import testbench_run

    monkeypatch.setenv("TESTBENCH_STORE", "file")
    monkeypatch.setattr(sys, "argv", ["x", "0.0.0.0", "8080", "10"])
    # Stub the launchers: if the guard ever regressed and DID NOT raise, the
    # bootstrap would fall through to launch a real server. Neutralizing the
    # launchers makes that regression a clean "no SystemExit" failure rather
    # than a spawned gunicorn/waitress process (so the guard's mutation-check
    # can't hang).
    monkeypatch.setattr(testbench_run.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(testbench_run.waitress, "serve", lambda *a, **k: None)
    monkeypatch.setattr(testbench, "run", lambda: object())
    with pytest.raises(SystemExit):
        testbench_run.start_server()


def test_rest_bootstrap_allows_loopback_bind_under_file_backend(monkeypatch):
    # A loopback host must clear the guard. Stop before the real server launch
    # by stubbing the launchers so no gunicorn/waitress process is spawned.
    import testbench
    import testbench_run

    monkeypatch.setenv("TESTBENCH_STORE", "file")
    monkeypatch.setattr(sys, "argv", ["x", "127.0.0.1", "8080", "10"])
    launched = {}
    monkeypatch.setattr(
        testbench_run.subprocess,
        "run",
        lambda *a, **k: launched.setdefault("subprocess", (a, k)),
    )
    monkeypatch.setattr(
        testbench_run.waitress,
        "serve",
        lambda *a, **k: launched.setdefault("waitress", (a, k)),
    )
    monkeypatch.setattr(testbench, "run", lambda: object())
    testbench_run.start_server()  # must not raise
    assert launched  # a launcher was reached, i.e. the guard passed
