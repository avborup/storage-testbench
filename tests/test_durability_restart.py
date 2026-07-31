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

"""Mechanism 6 durability: graceful restart round-trip (6a) plus corrupt-sidecar
(6c) and inode-collision (6d) restart-path loud-fail assertions, all driven
through a real Emulator launch over the wire."""

import os
import shutil
import tempfile
import time
import unittest

import grpc
import requests

from google.storage.v2 import storage_pb2, storage_pb2_grpc
from tests.conformance.emulator import Emulator

_PROJECT = "test-project"

# Reuse the harness's restart-readiness bound so a wedged worker surfaces as a
# clean failure, never a hung suite (matches emulator._STARTUP_TIMEOUT_SECONDS).
_STARTUP_TIMEOUT_SECONDS = int(
    os.environ.get("TESTBENCH_CONFORMANCE_STARTUP_TIMEOUT_SECONDS", "60")
)


def _await_worker_lock_released(root):
    """Block until the prior emulator's single-worker lock is stale or gone.

    On graceful teardown the emulator SIGTERMs the whole group; the Popen
    launcher dies at once (so _terminate's proc.wait returns), but gunicorn's
    worker finishes its graceful shutdown -- and only THEN runs the atexit that
    unlinks .gcs-worker.lock -- a beat later. Relaunching over the SAME root
    before that beat elapses would hit claim_worker_lock's LIVE-holder refusal
    (containment.py:199) instead of the property under test. claim reclaims a
    DEAD holder's stale lock silently, so the deterministic barrier is: wait
    until the lock file is gone or its recorded pid is no longer alive. Bounded
    by the startup timeout so a genuinely wedged worker fails cleanly.
    """
    lock = os.path.join(root, ".gcs-worker.lock")
    deadline = time.time() + _STARTUP_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            with open(lock) as fh:
                pid = int(fh.read().strip())
        except FileNotFoundError:
            return  # released
        except (OSError, ValueError):
            return  # unreadable/nascent -> claim's own guard handles it
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return  # dead holder -> claim reclaims silently
        time.sleep(0.05)
    raise AssertionError(
        "prior emulator worker still holds %s after %ds"
        % (lock, _STARTUP_TIMEOUT_SECONDS)
    )


def _seed_objects(rest_url, bucket, payloads):
    requests.post(
        rest_url + "/storage/v1/b",
        params={"project": _PROJECT},
        json={"name": bucket},
        timeout=30,
    ).raise_for_status()
    for name, data in payloads.items():
        r = requests.post(
            rest_url + "/upload/storage/v1/b/%s/o" % bucket,
            params={"uploadType": "media", "name": name},
            data=data,
            timeout=30,
        )
        r.raise_for_status()


def _read_all(rest_url, bucket, name):
    r = requests.get(
        rest_url + "/storage/v1/b/%s/o/%s" % (bucket, name),
        params={"alt": "media"},
        timeout=30,
    )
    r.raise_for_status()
    return r.content


class TestDurabilityRestart(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="testbench-durability-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.bucket = "durable-bucket"
        self.payloads = {
            "audio/clip.wav": b"clip-bytes-" * 100,
            "notes/readme.txt": b"hello world",
            "empty.bin": b"",
        }

    def test_graceful_restart_preserves_read_only_conformance(self):
        # e1: seed over the wire, capture the read-only responses.
        before = {}
        with Emulator(store="file", root=self.root) as e1:
            _seed_objects(e1.rest_url, self.bucket, self.payloads)
            for name in self.payloads:
                before[name] = _read_all(e1.rest_url, self.bucket, name)
            # gRPC read of one object too, so the restart is proven over both
            # transports (loopback bind is forced under store=file).
            with grpc.insecure_channel(e1.grpc_target) as ch:
                stub = storage_pb2_grpc.StorageStub(ch)
                chunks = b"".join(
                    resp.checksummed_data.content
                    for resp in stub.ReadObject(
                        storage_pb2.ReadObjectRequest(
                            bucket="projects/_/buckets/%s" % self.bucket,
                            object="audio/clip.wav",
                        ),
                        timeout=30,
                    )
                )
            self.assertEqual(before["audio/clip.wav"], chunks)
        # e1 exited gracefully (SIGTERM to the group). Wait for its worker to
        # actually die and drop the root lock before relaunching (the launcher
        # is reaped before gunicorn's worker finishes graceful shutdown).
        _await_worker_lock_released(self.root)
        # e2 rebuilds over the SAME persisted root -> rebuild_index re-hydrates
        # from the sidecars.
        with Emulator(store="file", root=self.root) as e2:
            for name, expected in before.items():
                self.assertEqual(
                    expected,
                    _read_all(e2.rest_url, self.bucket, name),
                    "restart changed bytes of %r" % name,
                )
            # Positive AND negative: a name never written stays 404 after restart
            # (a silently-repopulated or wrong bucket cannot pass).
            missing = requests.get(
                e2.rest_url + "/storage/v1/b/%s/o/%s" % (self.bucket, "never.bin"),
                timeout=30,
            )
            self.assertEqual(404, missing.status_code)

    def test_corrupt_sidecar_makes_restart_fail_loudly(self):
        # Extends test_filestore_scan.py::test_corrupt_sidecar_raises_loudly
        # (which proves rebuild_index raises ValueError IN-PROCESS) END TO END:
        # a re-launched emulator over a corrupted tree must FAIL to become ready
        # and surface the failure, never boot clean and silently 404 the object.
        with Emulator(store="file", root=self.root) as e1:
            _seed_objects(e1.rest_url, self.bucket, {"clip.wav": b"x"})
        sidecar = os.path.join(self.root, self.bucket, "clip.wav.gcsmeta")
        self.assertTrue(os.path.exists(sidecar))
        _await_worker_lock_released(self.root)
        with open(sidecar, "w") as fh:
            fh.write('{"schema_version":1,"proto"')  # truncated -> ValueError
        # rebuild_index runs at worker boot (Database.init on rest_server import),
        # so sidecar.load's ValueError crashes the gunicorn worker BEFORE it
        # serves. gunicorn aborts the initial boot (WORKER_BOOT_ERROR) and the
        # master exits, so _await_rest sees the process exit (poll() != None,
        # emulator.py:328) and raises "emulator exited early" within a second or
        # two -- well under the 60s startup bound; this is NOT a 60s hang.
        with self.assertRaises(RuntimeError) as ctx:
            with Emulator(store="file", root=self.root):
                pass
        # The RuntimeError embeds the worker log. sidecar.load raises
        # ValueError("corrupt sidecar: %s" % exc) (sidecar.py:37), so that exact,
        # code-guaranteed substring appears in the captured traceback. We do NOT
        # assert the object name: the error carries only the JSON decode message,
        # never the path (verified -- sidecar.py/filestore.py add no filename).
        self.assertIsInstance(ctx.exception, RuntimeError)
        self.assertIn("corrupt sidecar", str(ctx.exception))
        # Loud, not silent: a data-loss regression would instead start clean and
        # 404 the object -- which this assertRaises forbids.

    def test_inode_collision_makes_restart_fail_loudly(self):
        # Mechanism 6(d): plant two DISTINCT object names that resolve to the
        # SAME on-disk inode and assert a re-launched emulator fails LOUDLY at
        # rebuild. This exercises the REBUILD-TIME inode-collision detector
        # (_hydrate_live's `seen` RuntimeError, filestore.py:499) -- a path
        # phase-4's test_filestore_scan.py does NOT cover: its case-collision
        # test drives the WRITE-TIME guard on a case-insensitive FS, and its
        # case-sensitive branch asserts NO collision. Using a real hardlink (not
        # FS case collapse) makes the collision deterministic on ANY filesystem.
        with Emulator(store="file", root=self.root) as e1:
            _seed_objects(
                e1.rest_url,
                self.bucket,
                {"alpha.bin": b"aaaa", "beta.bin": b"bbbbbb"},
            )
        alpha = os.path.join(self.root, self.bucket, "alpha.bin")
        beta = os.path.join(self.root, self.bucket, "beta.bin")
        self.assertTrue(os.path.exists(alpha) and os.path.exists(beta))
        _await_worker_lock_released(self.root)
        # Collapse beta's media onto alpha's inode; both sidecars survive intact,
        # so two distinct true-names now resolve to one (st_dev, st_ino).
        os.remove(beta)
        os.link(alpha, beta)
        with self.assertRaises(RuntimeError) as ctx:
            with Emulator(store="file", root=self.root):
                pass
        self.assertIn("collision", str(ctx.exception))
        # Loud, not silent: a dropped guard would instead boot clean and serve
        # both objects off the shared inode -- which this assertRaises forbids.

    def test_real_gcs_harness_skips_without_credentials(self):
        # Mechanism 8 is a manual/external job (see README "Verification:
        # manual/external jobs"). Its module must stay importable and CI-safe:
        # with TESTBENCH_REAL_GCS_PROJECT unset the ONLY executed path is a clean
        # SKIP that returns 0 (never contacts real GCS, never fails CI).
        saved = os.environ.pop("TESTBENCH_REAL_GCS_PROJECT", None)
        try:
            from tests.conformance import real_gcs_divergence

            self.assertEqual(0, real_gcs_divergence.main())
        finally:
            if saved is not None:
                os.environ["TESTBENCH_REAL_GCS_PROJECT"] = saved
