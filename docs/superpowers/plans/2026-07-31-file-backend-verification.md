# Phase 7 — Verification (durability / crash / concurrency) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discharge the spec's *Verification plan* for the file backend — Mechanism 6 (durability/crash), Mechanism 7 (concurrency), and the documentation of Mechanism 8 (real-GCS divergence) and Mechanism 9 (downstream client) as explicit manual/external jobs. These are the **final** phase. Every phase-7 test is a **file-backend-only, B-less, NEW-behaviour assertion**: it proves a property the memory backend has no counterpart for. It therefore adds **no golden**, does **not** touch `tests/conformance/allowlist.json`, and leaves the memory harness EMPTY-diff (digest `98fa2130…`) and the file harness B≡C (allow-list at exactly one entry, `create-bucket-traversal`).

**Architecture:** Four thin, self-contained additions that reuse phases 4–6 wholesale and add only what the survey proved genuinely missing:
1. A single **public `kill()` helper on `tests/conformance/emulator.py`** — the ONE `tests/conformance/` file permitted to touch process internals — that `SIGKILL`s the whole gunicorn process group (mirroring the existing `killpg` teardown at `emulator.py:283/293`), reaps the `Popen`, and snapshots logs. This keeps the durability tests out of the private `_process` attribute and off the leaked-worker footgun.
2. `tests/test_durability_restart.py` — Mechanism 6(a) live graceful stop/restart round-trip re-asserting read-only conformance over the wire, and 6(c) corrupt-sidecar + 6(d) inode-collision restart-path loud-fail assertions that extend (never duplicate) the in-process `tests/test_filestore_scan.py` guards through an actual `Emulator` launch — including the rebuild-time inode-collision path that phase 4 does **not** cover.
3. `tests/test_sigkill_upload.py` — Mechanism 6(b) `SIGKILL`-mid-upload → restart → partial-invisibility, with a **deterministic client-controlled kill point**: a resumable REST upload paused at a non-final `308 Resume Incomplete` chunk, so the staged bytes provably live under `.gcs/uploads/<upload_id>` and `object_inserted` has provably not run.
4. `tests/test_grpc_concurrency.py` — Mechanism 7 in-process concurrency suite driven through the **real** `grpc_server.run()` `ThreadPoolExecutor` (the bug lives in the pool sizing), using held-open `BidiWriteObject` streams as a deterministic thread-occupancy primitive; it is mutation-checked to FAIL at `TESTBENCH_GRPC_THREADS=2` — the exact regression the old `_GRPC_SERVER_THREAD_COUNT = 2` was.

Mechanisms 8 and 9 are **documented, not run**: a runnable-but-default-skipped `tests/conformance/real_gcs_divergence.py` harness mode (needs a project + credentials + money; skipped unless `TESTBENCH_REAL_GCS_PROJECT` is set) and a README/handoff note pointing M9 at the downstream application repo.

**Tech Stack:** Python 3.8–3.12, stdlib only (`os`, `signal`, `subprocess`, `tempfile`, `threading`, `concurrent.futures`, `hashlib`) plus the already-present, already-pinned `grpcio==1.70.0` (`storage_pb2_grpc.StorageStub`), `crc32c`, `requests`, and `protobuf`; the phase-1 conformance harness (`tests/conformance/`) and the phase-4/5 `FileStore`/`FileMedia`. **Zero new runtime or test dependencies** — everything used here is already installed.

## Global Constraints

- **Zero new runtime dependencies.** `setup.py` stays byte-for-byte unchanged. Phase 7 uses only stdlib + the already-present, already-pinned `grpcio`/`crc32c`/`requests`/`protobuf` and the existing `tests/conformance/` harness. Do NOT add anything to `setup.py`, `flake.nix`, or the CI `pip install`. Use `.venv/bin/python -m pytest`, `.venv/bin/isort`, `.venv/bin/black`, and `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness` as the toolchain — **nothing is on the bare `PATH`** (`nix develop` provisions the venv).
- **Python floor is 3.8.** Every new file must parse under `ast.parse(feature_version=(3, 8))`. CI runs a 3.8–3.12 matrix. The `killpg`/`SIGKILL`/`start_new_session` APIs the durability tests rely on are all POSIX and present since 3.3.
- **Phase-7 suites add NO golden and NEVER `--regenerate`.** These are file-backend-only NEW-behaviour assertions with no memory-backend counterpart. They do NOT capture a trace, do NOT write to `tests/conformance/golden/`, and do NOT touch `tests/conformance/allowlist.json`. `--regenerate` NEVER turns a red gate green.
- **The memory backend stays byte-identical.** The memory harness diff must stay **empty** and the golden digest unchanged at `98fa2130d213b04478474c5918a6ba36e3e52838823189f4093a9161f72987a7` (sha256 of `golden/{rest,grpc,faults}.json` concatenated).
- **The file backend stays B≡C with the allow-list at EXACTLY one entry.** `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file` must keep printing `OK`, diverging **only** on the single allow-listed `create-bucket-traversal` label. Phase 7 must not add, remove, or alter any allow-list entry.
- **The harness import rule holds.** Nothing in `tests/conformance/` may import `gcs`/`testbench` internals except `emulator.py`. The new `kill()` helper belongs IN `emulator.py` (which imports only stdlib `subprocess`/`signal`). The durability/SIGKILL/concurrency suites live under `tests/` (not `tests/conformance/`) and may import `tests.conformance.emulator.Emulator`; the concurrency suite imports `testbench`/`gcs` freely because it lives under `tests/`, not `tests/conformance/`. The `real_gcs_divergence.py` harness mode drives purely over the wire and imports no internals.
- **Mutation-check every guard.** After a guard passes, reintroduce the specific defect it guards and confirm the test FAILS. The concurrency starvation guard MUST be observed to fail at `TESTBENCH_GRPC_THREADS=2` — a guard never seen to fail is not known to work. The **equivalent-mutant carve-out** applies: a clause provably subsumed by a stricter downstream clause is exempt from individual killability provided the load-bearing clause it defers to *is* mutation-killed and the redundancy is documented.
- **Crash/concurrency tests must be ROBUST, not flaky.** Use **deterministic kill points** (a resumable `308` pause, never a timer racing a fast streaming upload), **deterministic readiness barriers** (a per-stream occupancy `Event` set on the server's first `state_lookup` response, never a fixed `time.sleep`), **generous timeouts** (reuse `TESTBENCH_CONFORMANCE_STARTUP_TIMEOUT_SECONDS=60` for restart readiness; per-RPC `timeout=` on every client call so a starved call surfaces as a clean `DeadlineExceeded`, never a hung suite), and **ratio/bound-based concurrency assertions**, never exact latencies. Always assert BOTH the positive (surviving objects readable) and the negative (partial invisible) so a silently-empty bucket cannot pass. Always `server.stop(grace)`/`addCleanup` and close channels; a leaked in-process server or emulator holds its root lock and wedges later tests.
- **Docker-optional.** Mechanisms 6 and 7 run via the in-process/subprocess `Emulator` (`tests.conformance.emulator`) and `grpc_server.run()`; they NEVER require `docker compose`. Note any container-only variant as deferred to CI/manual.
- **POSIX-only / Windows out of scope for the file backend.** `SIGKILL`/`killpg` and the fd-based `FileStore` are POSIX. The new modules are added to `conftest.py`'s `collect_ignore_glob` on Windows, matching the phase-4 pattern for the file-backend modules.
- **The one macOS-hang test** (`tests/test_testbench_continue_after_fault_injection.py`) is ignored locally but NEVER in CI. Do NOT model new crash tests on its fault-injection-continue pattern; keep kills deterministic and reaped.
- **Formatting is `isort` then `black`, in that order** (`isort==5.12.0`, `black==22.3.0`). CI enforces the combination.
- **Single gunicorn worker, `--reload` on.** Never edit a `.py` file while a harness run or emulator-backed test is in flight; it restarts the worker mid-trace and wipes state.

### The phase-7 exit gate (what "done" means)

From the spec's per-phase gates (row 7), phase 7 is green when **all** hold:

1. `TESTBENCH_TEST_STORE=memory` and `TESTBENCH_TEST_STORE=file` pytest legs are green (`--ignore=tests/test_testbench_continue_after_fault_injection.py`), now including `tests/test_durability_restart.py`, `tests/test_sigkill_upload.py`, and `tests/test_grpc_concurrency.py`.
2. `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store memory` prints `OK` for all three traces with an **empty diff** (digest `98fa2130…` unchanged).
3. `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file` prints `OK`, diverging **only** on `create-bucket-traversal` (allow-list still exactly one entry).
4. The concurrency starvation guard has been **observed to fail** at `TESTBENCH_GRPC_THREADS=2` and pass at the default 32 (mutation-check recorded in the commit body).
5. `nix develop --command make verify-linux` is green.
6. Mechanism 8's `real_gcs_divergence.py` is present, importable, and **skips cleanly** with no credentials (`TESTBENCH_REAL_GCS_PROJECT` unset); Mechanisms 8 and 9 are documented as manual/external in `README.md` and the handoff.
7. Nothing in `tests/conformance/` imports `gcs`/`testbench` except `emulator.py`; `setup.py` unchanged; `tests/conformance/allowlist.json` unchanged (one entry); `tests/conformance/golden/` unchanged.

---

## File Structure

- **Modify `tests/conformance/emulator.py`** — add ONE public method `kill(self)`: `os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)` (guarded by `ProcessLookupError`), then `self._process.wait()` to reap, then snapshot `self._stdout_file` into `self._logs_cache` exactly as `_terminate` does. Mirrors the existing SIGKILL-fallback logic (`emulator.py:293`) so the gunicorn master+worker die together. No other behaviour changes; `__exit__`/`_terminate` remain the graceful path.
- **Create `tests/test_durability_restart.py`** — Mechanism 6(a) live graceful stop/restart round-trip re-asserting read-only conformance over REST+gRPC; 6(c) corrupt-sidecar restart-path loud-fail (extends `test_filestore_scan.py::test_corrupt_sidecar_raises_loudly` end-to-end); 6(d) inode-collision restart-path loud-fail that plants a real hardlink so two distinct true-names resolve to one inode and asserts the rebuild-time detector (`_hydrate_live`'s `seen` `RuntimeError`, `filestore.py:499`) fails the launch loudly — a path phase-4 does NOT exercise. Imports `tests.conformance.emulator.Emulator`, `requests`, `grpc`, `storage_pb2_grpc`.
- **Create `tests/test_sigkill_upload.py`** — Mechanism 6(b) `SIGKILL`-mid-upload → restart → partial-invisible, deterministic resumable-`308` kill point. Its relaunch of e2 over the SAME root is also the observable for Task 1's `kill()` group-kill mutation-check.
- **Create `tests/test_grpc_concurrency.py`** — Mechanism 7 in-process concurrency suite via `grpc_server.run(0, db)` over a `FileStore` root; held-open `BidiWriteObject` starvation arm (mutation-killed at pool=2, occupancy proven by a `state_lookup` readiness barrier) + broad N-parallel-streams correctness arm. Reuses the env save/restore `setUp`/`tearDown` from `tests/test_grpc_threads.py` and the WriteObject request-generator shape from `tests/test_filemedia_restart.py`.
- **Create `tests/conformance/real_gcs_divergence.py`** — Mechanism 8 runnable-but-default-skipped divergence harness. It is INTENDED to drive the existing `trace_rest`/`trace_grpc` scripts against a real GCS project and report diffs; that trace-driving body is intentionally left unimplemented (operator-run, out of CI scope). The only executed behaviour is the credential-less `SKIP` (returns 0). With `TESTBENCH_REAL_GCS_PROJECT` set it exits with a "wire it here" `SystemExit`. Imports NO `gcs`/`testbench` internals (over-the-wire only), honouring the harness import rule.
- **Modify `conftest.py`** — extend the Windows `collect_ignore_glob` list (glob patterns inside the `if os.name == "nt":` block, `conftest.py:~66`) with the three new POSIX-only file-backend module paths (`test_durability_restart.py`, `test_sigkill_upload.py`, `test_grpc_concurrency.py`), which match NO existing glob and so must be listed explicitly. There is no `collect_ignore` list in this file; do NOT add a module-level unconditional `collect_ignore`, which would skip the phase-7 suites on Linux CI too and defeat exit-gate item 1.
- **Modify `README.md`** — add a short "Verification: manual/external jobs" section documenting Mechanism 8 (how to run `real_gcs_divergence.py` with credentials, why it is not in CI) and Mechanism 9 (downstream Rust-client smoke suite lives in the application repo).
- **Modify `.github/workflows/build.yaml`** — the three new modules run automatically under the existing Linux `python-tests` legs (they build their own `FileStore`/`Emulator`, so they run under either `TESTBENCH_TEST_STORE` value). Add an explicit comment noting the concurrency + durability suites; do NOT add a real-GCS job (Mechanism 8 is manual).

Each task ends with the safety gate below. No task adds a golden, touches the allow-list, or runs `--regenerate`.

---

### Task 1: `Emulator.kill()` + Mechanism 6(a) graceful stop/restart round-trip; 6(c) corrupt-sidecar & 6(d) inode-collision restart-path loud-fail

**Files:**
- Modify: `tests/conformance/emulator.py` (add public `kill(self)` after `_terminate` :262-310; reuse `killpg` pattern at :283/293, log snapshot at :297-306)
- Create: `tests/test_durability_restart.py`
- Modify: `conftest.py` (Windows `collect_ignore_glob`)

**Interfaces:**
- Produces: `Emulator.kill()` — hard-kills the whole process group with `SIGKILL`, reaps `self._process`, snapshots logs into `self._logs_cache`; safe to call once, and `__exit__` afterward is a no-op reap. 6(a) reuses the already-plumbed graceful path unchanged: `with Emulator(store="file", root=R) as e1: <upload>` then `with Emulator(store="file", root=R) as e2: <read>` (the second launch rebuilds over the persisted tree; `root=R` passed explicitly ⇒ `_own_root is False` ⇒ teardown does NOT `rmtree`, `emulator.py:136/307`, so the test owns and cleans the root).
- Consumes: `containment.claim_worker_lock` at boot (`rest_server.py:47`), which refuses a second LIVE holder loudly (`test_worker_lock.py::test_second_live_holder_fails_loudly`) but reclaims a DEAD holder's stale lock silently — the property the `kill()` mutation-check leans on.

- [ ] **Step 1: Add the public `kill()` helper to `emulator.py`**

```python
# tests/conformance/emulator.py -- add as a public method, after _terminate().
# The durability tests must NOT reach into self._process; a hard kill belongs
# here, the one tests/conformance/ file allowed to touch process internals.
def kill(self):
    """SIGKILL the whole emulator process group and reap it.

    Mirrors the SIGKILL-fallback in _terminate (:293): start_new_session=True
    (:241) put this Popen plus gunicorn's master and worker into one group, so
    killing only self._process would leave gunicorn's worker alive and holding
    the single-worker root lock (.gcs-worker.lock) -- the exact leaked-worker
    failure this harness guards against. os.killpg of the group makes them die
    together, so the lock the dead worker left behind is stale and a relaunch
    over the same root reclaims it. Snapshots logs so logs() keeps working
    after the kill, exactly as _terminate does."""
    proc = self._process
    if proc is None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    if self._stdout_file is not None:
        try:
            self._stdout_file.seek(0)
            self._logs_cache = self._stdout_file.read().decode("utf-8", "replace")
            self._stdout_file.close()
        except ValueError:
            pass
    self._process = None  # so a later __exit__ / _terminate is a clean no-op
```

Setting `self._process = None` at the end makes `__exit__` (`:257-260`) skip `_terminate`, so a `with`-block that calls `kill()` mid-body tears down cleanly.

- [ ] **Step 2: Write the failing 6(a) round-trip test**

```python
# tests/test_durability_restart.py
import hashlib
import os
import shutil
import tempfile
import unittest

import grpc
import requests

from google.storage.v2 import storage_pb2, storage_pb2_grpc
from tests.conformance.emulator import Emulator

_PROJECT = "test-project"


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
        # e1 exited gracefully (SIGTERM to the group). e2 rebuilds over the SAME
        # persisted root -> rebuild_index re-hydrates from the sidecars.
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
```

- [ ] **Step 3: Run to see it pass (the plumbing already exists), then add the 6(c) corrupt-sidecar and 6(d) inode-collision restart-path loud-fail tests**

```python
    def test_corrupt_sidecar_makes_restart_fail_loudly(self):
        # Extends test_filestore_scan.py::test_corrupt_sidecar_raises_loudly
        # (which proves rebuild_index raises ValueError IN-PROCESS) END TO END:
        # a re-launched emulator over a corrupted tree must FAIL to become ready
        # and surface the failure, never boot clean and silently 404 the object.
        with Emulator(store="file", root=self.root) as e1:
            _seed_objects(e1.rest_url, self.bucket, {"clip.wav": b"x"})
        sidecar = os.path.join(self.root, self.bucket, "clip.wav.gcsmeta")
        self.assertTrue(os.path.exists(sidecar))
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
```

Run: `TESTBENCH_TEST_STORE=file PYTHONPATH=. .venv/bin/python -m pytest tests/test_durability_restart.py -q`
Expected: `test_graceful_restart_preserves_read_only_conformance` PASS; `test_corrupt_sidecar_makes_restart_fail_loudly` PASS (the launch raises `RuntimeError` whose log embeds `corrupt sidecar`); `test_inode_collision_makes_restart_fail_loudly` PASS (the launch raises `RuntimeError` whose log embeds `collision`).

- [ ] **Step 4: Add the three modules to the Windows `collect_ignore_glob` in `conftest.py`**

Append the three exact paths `"tests/test_durability_restart.py"`, `"tests/test_sigkill_upload.py"`, `"tests/test_grpc_concurrency.py"` to the existing `collect_ignore_glob` list inside the `if os.name == "nt":` block (`conftest.py:~66`). Literal paths are valid glob patterns; these three match none of the existing globs (`test_filestore*.py`, `test_filemedia*.py`, …), so they must be listed explicitly, or the Windows CI leg (`windows-2022`, py3.11) would try to collect `signal.SIGKILL`/fd-based modules and error. Do NOT introduce a module-level unconditional `collect_ignore` — that would skip these suites on Linux CI too.

- [ ] **Step 5: Mutation-check `kill()`, the corrupt-sidecar guard, and the collision guard**

- **`kill()` group-kill (observed via Task 2's same-root relaunch):** in `kill()`, replace `os.killpg(pgid, signal.SIGKILL)` with `proc.kill()` (kill only the Popen leader, leaving gunicorn's master+worker alive in the group). Re-run `tests/test_sigkill_upload.py`. Expected: the follow-up `with Emulator(store="file", root=self.root) as e2:` FAILS to become ready and `__enter__` raises `RuntimeError`, because the orphaned LIVE gunicorn worker still holds the single-worker root lock (`containment.claim_worker_lock`, `rest_server.py:47`; `test_worker_lock.py::test_second_live_holder_fails_loudly`) — proving the group-kill is load-bearing. Do NOT assert a port-bind failure: e2 draws FRESH ephemeral ports (`emulator.py:144`), so an orphan on e1's old port would not block e2's bind; the ROOT LOCK, not the port, is the observable. Revert.
- **corrupt-sidecar loud-fail:** in `test_corrupt_sidecar_makes_restart_fail_loudly`, replace the truncated write with a valid re-dump of the sidecar (no corruption). Expected: the test FAILS (no `RuntimeError`; the emulator boots clean), proving the assertion catches a real loud failure rather than any launch error. Revert.
- **inode-collision loud-fail:** this guards `filestore.py`'s `_hydrate_live` `seen` `RuntimeError` (`:499`). Temporarily neuter it (drop the `if previous is not None and previous != name: raise`). Expected: `test_inode_collision_makes_restart_fail_loudly` FAILS (the emulator boots clean over the collided inode). Record in the commit body that this defect lives in `filestore.py`, so the revert is mandatory before any merge. Revert.

- [ ] **Step 6: 3.8 parse, format, commit**

```bash
PYTHONPATH=. .venv/bin/python -c "import ast; [ast.parse(open(f).read(), feature_version=(3,8)) for f in ('tests/conformance/emulator.py','tests/test_durability_restart.py')]"
.venv/bin/isort --quiet tests/conformance/emulator.py tests/test_durability_restart.py conftest.py && .venv/bin/black --quiet tests/conformance/emulator.py tests/test_durability_restart.py conftest.py
git add tests/conformance/emulator.py tests/test_durability_restart.py conftest.py
git commit -m "test(durability): Mechanism 6a graceful restart round-trip + 6c/6d restart-path loud-fail; Emulator.kill()"
```

**Safety gate:** memory harness EMPTY diff (digest `98fa2130…`); file harness B≡C (one entry); no golden added, allow-list untouched. `kill()` group-kill (via the same-root root lock), the corrupt-sidecar guard, and the inode-collision guard all mutation-checked.

---

### Task 2: Mechanism 6(b) — `SIGKILL`-mid-upload → restart → partial object INVISIBLE (deterministic kill point)

**Files:**
- Create: `tests/test_sigkill_upload.py`

**Interfaces:**
- Consumes: `Emulator.kill()` (Task 1); the resumable REST protocol (`rest_server.py:1185 resumable_upload_chunk`, staging under `.gcs/uploads/<upload_id>` via `new_upload_media` at `rest_server.py:1129`/`filestore.py:155`); the on-disk write-order guarantee (`filestore.py:192` writes media then sidecar only at `object_inserted`, which fires on the FINAL chunk when `upload.complete` becomes true, `rest_server.py:~1331`; `rebuild_index`/`_scan_bucket` at `filestore.py:388/418` are sidecar-driven and explicitly skip `.gcs/uploads/`, `filestore.py:445`).
- Produces: a robust proof that a hard kill after a **non-final** resumable chunk leaves the in-flight object invisible after restart, while a normally-inserted control object and `bucket.json` survive intact. Its relaunch of e2 over the SAME root is also the observable for Task 1 Step 5's `kill()` group-kill mutation-check.

- [ ] **Step 1: Write the failing SIGKILL test**

```python
# tests/test_sigkill_upload.py
import os
import shutil
import tempfile
import unittest

import requests

from tests.conformance.emulator import Emulator

_PROJECT = "test-project"


class TestSigkillMidUpload(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="testbench-sigkill-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.bucket = "kill-bucket"

    def test_partial_upload_is_invisible_after_sigkill_restart(self):
        control = b"i-am-fully-committed" * 50
        partial_first = b"A" * 512  # a single NON-final resumable chunk
        with Emulator(store="file", root=self.root) as e1:
            base = e1.rest_url
            requests.post(
                base + "/storage/v1/b",
                params={"project": _PROJECT},
                json={"name": self.bucket},
                timeout=30,
            ).raise_for_status()
            # Control object: fully inserted, must survive the kill intact.
            requests.post(
                base + "/upload/storage/v1/b/%s/o" % self.bucket,
                params={"uploadType": "media", "name": "control.bin"},
                data=control,
                timeout=30,
            ).raise_for_status()
            # Start a resumable upload for the DOOMED object.
            start = requests.post(
                base + "/upload/storage/v1/b/%s/o" % self.bucket,
                params={"uploadType": "resumable", "name": "doomed.bin"},
                json={"name": "doomed.bin"},
                timeout=30,
            )
            start.raise_for_status()
            upload_path = start.headers["Location"]
            if upload_path.startswith(base):
                upload_path = upload_path[len(base):]
            # Send exactly ONE non-final chunk with an OPEN-ENDED total
            # (Content-Range ".../*" => "more to come"). upload.complete stays
            # False -- neither `total_object_size == len(upload.media)` nor
            # `chunk_last_byte + 1 == total_object_size` holds (rest_server.py:
            # ~1331) -- so the server MUST answer 308 Resume Incomplete: a
            # stable, client-controlled pause, NOT a timer racing the upload.
            chunk = requests.put(
                base + upload_path,
                data=partial_first,
                headers={
                    "Content-Range": "bytes 0-%d/*" % (len(partial_first) - 1)
                },
                timeout=30,
            )
            self.assertEqual(308, chunk.status_code)  # assert the pause BEFORE killing
            # At this point the bytes are provably staged under
            # .gcs/uploads/<upload_id> and object_inserted has NOT run: there is
            # no doomed.bin at its natural path and no doomed.bin.gcsmeta.
            self.assertFalse(
                os.path.exists(os.path.join(self.root, self.bucket, "doomed.bin"))
            )
            self.assertFalse(
                os.path.exists(
                    os.path.join(self.root, self.bucket, "doomed.bin.gcsmeta")
                )
            )
            e1.kill()  # SIGKILL the whole group; no graceful sidecar flush
        # Restart over the SAME root. rebuild_index is sidecar-driven and skips
        # .gcs/uploads/, so the staged bytes are never adopted. (This relaunch
        # over the same root is also the kill() group-kill observable, Task 1
        # Step 5: a leaked live worker would hold the root lock and fail e2.)
        with Emulator(store="file", root=self.root) as e2:
            base = e2.rest_url
            # NEGATIVE: the partial object is invisible.
            doomed = requests.get(
                base + "/storage/v1/b/%s/o/%s" % (self.bucket, "doomed.bin"),
                timeout=30,
            )
            self.assertEqual(404, doomed.status_code)
            # POSITIVE: the control object is intact, byte-for-byte.
            got = requests.get(
                base + "/storage/v1/b/%s/o/%s" % (self.bucket, "control.bin"),
                params={"alt": "media"},
                timeout=30,
            )
            got.raise_for_status()
            self.assertEqual(control, got.content)
            # The bucket itself survived (bucket.json intact).
            bkt = requests.get(
                base + "/storage/v1/b/%s" % self.bucket, timeout=30
            )
            self.assertEqual(200, bkt.status_code)
```

- [ ] **Step 2: Run to green**

Run: `TESTBENCH_TEST_STORE=file PYTHONPATH=. .venv/bin/python -m pytest tests/test_sigkill_upload.py -q`
Expected: PASS. The `assertEqual(308, ...)` and the two pre-kill `assertFalse` checks pin the deterministic kill point; the post-restart trio asserts invisible+intact+bucket-alive. This is valid **without** `TESTBENCH_FSYNC`: a `SIGKILL` of the *process* does not lose page-cache writes (only a machine crash would); invisibility comes from the write-order + `os.replace` atomicity + `rebuild_index` skipping `.gcs/uploads/`, not from fsync.

- [ ] **Step 3: Mutation-check the invisibility guarantee and the kill-point determinism**

- **invisibility:** the guarantee under test is `_scan_bucket` skipping `.gcs/uploads/` (`filestore.py:445`). Reintroduce the defect: temporarily make `_scan_bucket` also hydrate a staged `.gcs/uploads/<id>` entry into the index (a one-line adoption that violates spec Rule 1 "a media file with no sidecar is not a live object"). Expected: `test_partial_upload_is_invisible_after_sigkill_restart` FAILS on `assertEqual(404, doomed.status_code)`. Revert. (Record in the commit body that this defect lives in `filestore.py`, so the revert is mandatory before any merge.)
- **kill-point determinism:** confirm the kill point is a real pause, not a race. Change the single chunk to a FINAL chunk that completes the object (`Content-Range: bytes 0-511/512`, so `total_object_size == len(media)` ⇒ `upload.complete` ⇒ `object_inserted`). Expected: the `assertEqual(308, ...)` reddens (the server returns 200) and/or the pre-kill `assertFalse(...doomed.bin.gcsmeta)` reddens — proving the `/*` open-ended non-final chunk is what keeps the object un-inserted. Revert.

- [ ] **Step 4: 3.8 parse, format, commit**

```bash
PYTHONPATH=. .venv/bin/python -c "import ast; ast.parse(open('tests/test_sigkill_upload.py').read(), feature_version=(3,8))"
.venv/bin/isort --quiet tests/test_sigkill_upload.py && .venv/bin/black --quiet tests/test_sigkill_upload.py
git add tests/test_sigkill_upload.py
git commit -m "test(durability): Mechanism 6b SIGKILL-mid-upload -> restart -> partial object invisible"
```

**Safety gate:** memory harness EMPTY diff; file harness B≡C (one entry); no golden, allow-list untouched. Deterministic 308 kill point; invisibility guarantee and kill-point determinism both mutation-checked; both positive and negative post-restart assertions present.

---

### Task 3: Mechanism 7 — concurrency suite through the real `grpc_server.run()` thread pool

**Files:**
- Create: `tests/test_grpc_concurrency.py`

**Interfaces:**
- Consumes: `grpc_server.run(0, db)` → `(bound_port, server)` building `ThreadPoolExecutor(max_workers=_grpc_thread_count())` (`grpc_server.py:1403-1411`, `_grpc_thread_count` at `:47-51`, default 32); loopback bind under `TESTBENCH_STORE=file` (`_bind_host` at `:1393`); held-open `BidiWriteObject` (`:1162`, worker parks inside `process_bidi_write_object_grpc`'s consumption of `request_iterator`), which yields a `persisted_size` `BidiWriteObjectResponse` for a `state_lookup=True` request (`gcs/upload.py:663-668`) — the deterministic occupancy signal; `ReadObject` (`:615`) streaming media **outside** `_resources_lock`; `FileStore(root)` + `Database.init(store=...)` seeding. Reuses the env save/restore `setUp`/`tearDown` of `tests/test_grpc_threads.py` and the WriteObject request-generator shape of `tests/test_filemedia_restart.py`.
- Produces: (Arm A) a deterministic starvation guard that PASSES at a healthy pool and FAILS at `TESTBENCH_GRPC_THREADS=2`; (Arm B) a broad N-parallel-streams + interleaved-metadata correctness arm proving no starvation and no cross-stream mix-ups, transitively exercising "bulk media I/O outside the lock."

- [ ] **Step 1: Write the module skeleton, seeding, and env discipline**

```python
# tests/test_grpc_concurrency.py
import concurrent.futures
import hashlib
import os
import shutil
import tempfile
import threading
import unittest

import crc32c
import grpc

import testbench.database
import testbench.grpc_server
from google.storage.v2 import storage_pb2, storage_pb2_grpc
from testbench.filestore import FileStore

MiB = 1024 * 1024
_BUCKET = "projects/_/buckets/conc-bucket"


def _content(i, size):
    seed = b"obj-%04d-" % i
    return (seed * (size // len(seed) + 1))[:size]


class TestGrpcConcurrency(unittest.TestCase):
    def setUp(self):
        # Reuse test_grpc_threads.py's env save/restore discipline.
        self._saved_threads = os.environ.pop("TESTBENCH_GRPC_THREADS", None)
        self._saved_store = os.environ.get("TESTBENCH_STORE")
        os.environ["TESTBENCH_STORE"] = "file"  # forces loopback bind
        self.root = tempfile.mkdtemp(prefix="testbench-conc-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.n_objects = 12
        self.obj_size = 4 * MiB
        self.expected = {}
        db = testbench.database.Database.init(store=FileStore(self.root))
        import gcs.bucket
        import testbench.common

        req = testbench.common.FakeRequest(
            args={}, data='{"name": "conc-bucket"}'
        )
        bucket, _ = gcs.bucket.Bucket.init(req, None)
        db.insert_bucket(bucket, None)
        self.db = db

    def tearDown(self):
        if self._saved_threads is None:
            os.environ.pop("TESTBENCH_GRPC_THREADS", None)
        else:
            os.environ["TESTBENCH_GRPC_THREADS"] = self._saved_threads
        if self._saved_store is None:
            os.environ.pop("TESTBENCH_STORE", None)
        else:
            os.environ["TESTBENCH_STORE"] = self._saved_store

    def _mock_context(self):
        import unittest.mock

        ctx = unittest.mock.Mock()
        ctx.invocation_metadata = unittest.mock.Mock(return_value=dict())
        return ctx

    def _seed(self):
        # Seed distinct per-object content via the real WriteObject servicer
        # (the from-existing hydration path), recording expected crc32c so a
        # cross-stream mix-up on a shared worker is detectable, not just
        # completion. Request-generator shape mirrors test_filemedia_restart.py.
        grpc_servicer = testbench.grpc_server.StorageServicer(self.db)
        for i in range(self.n_objects):
            data = _content(i, self.obj_size)
            self.expected["obj-%d" % i] = crc32c.crc32c(data)

            def reqs(name=("obj-%d" % i), payload=data):
                yield storage_pb2.WriteObjectRequest(
                    write_object_spec=storage_pb2.WriteObjectSpec(
                        resource={"name": name, "bucket": _BUCKET},
                    ),
                    write_offset=0,
                    checksummed_data=storage_pb2.ChecksummedData(
                        content=payload, crc32c=crc32c.crc32c(payload)
                    ),
                    finish_write=True,
                )

            grpc_servicer.WriteObject(reqs(), context=self._mock_context())

    def _start_server(self):
        port, server = testbench.grpc_server.run(0, self.db)
        self.addCleanup(server.stop, None)
        channel = grpc.insecure_channel("127.0.0.1:%d" % port)
        self.addCleanup(channel.close)
        return storage_pb2_grpc.StorageStub(channel)
```

- [ ] **Step 2: Arm A — deterministic starvation guard (killed at pool=2, occupancy proven by a readiness barrier)**

```python
    def test_metadata_not_starved_by_held_streams(self):
        # Pin a healthy-but-small pool so the contrast is sharp and fast: with 8
        # workers, park 4 with held-open BidiWriteObject streams, leaving
        # headroom; an interleaved GetObject with a deadline MUST still complete.
        # At pool=2 (< the 4 parked) every worker is consumed by a parked stream
        # and the GetObject starves -> DeadlineExceeded -> this FAILS.
        os.environ["TESTBENCH_GRPC_THREADS"] = "8"
        self._seed()
        stub = self._start_server()
        release = threading.Event()
        self.addCleanup(release.set)
        parked = 4
        occupied = [threading.Event() for _ in range(parked)]

        def held_stream(idx):
            # First request carries state_lookup=True: the server yields a
            # persisted_size BidiWriteObjectResponse (gcs/upload.py:663-668),
            # which PROVES its worker is occupied. We set occupied[idx] on that
            # first response, THEN the generator blocks on `release`, parking the
            # worker deterministically. BidiWrite has NO 10s auto-cancel (only
            # BidiRead does, grpc_server.py:842), so the park is stable.
            def gen():
                yield storage_pb2.BidiWriteObjectRequest(
                    write_object_spec=storage_pb2.WriteObjectSpec(
                        resource={"name": "held-%d" % idx, "bucket": _BUCKET},
                    ),
                    write_offset=0,
                    state_lookup=True,
                )
                release.wait(30)  # released in the cleanup below
            try:
                for _resp in stub.BidiWriteObject(gen(), timeout=30):
                    occupied[idx].set()  # first response => worker parked
            except grpc.RpcError:
                pass  # cancelled at teardown -- expected

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=parked)
        self.addCleanup(pool.shutdown, False)
        for i in range(parked):
            pool.submit(held_stream, i)
        # Readiness barrier: wait until ALL parked streams have proven occupancy
        # via their first server response -- deterministic regardless of machine
        # speed, with NO fixed sleep.
        for i, ev in enumerate(occupied):
            self.assertTrue(
                ev.wait(30), "held stream %d never occupied a worker" % i
            )
        # Interleaved metadata op with a generous client deadline. With headroom
        # (8 - 4 = 4 free) this returns promptly; at pool=2 it raises.
        resp = stub.GetObject(
            storage_pb2.GetObjectRequest(bucket=_BUCKET, object="obj-0"),
            timeout=10,
        )
        self.assertEqual("obj-0", resp.name)
        release.set()
```

- [ ] **Step 3: Arm B — broad correctness (no starvation, no mix-ups, outside-the-lock)**

```python
    def test_parallel_streams_and_metadata_all_correct(self):
        # Default-healthy pool. N concurrent streaming ReadObject transfers of
        # distinct multi-MiB objects + interleaved GetObject/ListObjects, each
        # with a per-RPC deadline. Every stream is verified by crc32c over its
        # concatenated bytes so a cross-stream mix-up (wrong object on a shared
        # worker) is caught, not just completion. At pool=2 the streams serialize
        # and the metadata ops queue past the deadline -> DeadlineExceeded.
        os.environ["TESTBENCH_GRPC_THREADS"] = "32"
        self._seed()
        stub = self._start_server()

        def read_stream(i):
            name = "obj-%d" % i
            body = b"".join(
                r.checksummed_data.content
                for r in stub.ReadObject(
                    storage_pb2.ReadObjectRequest(bucket=_BUCKET, object=name),
                    timeout=30,
                )
            )
            return name, crc32c.crc32c(body)

        def meta_op(k):
            if k % 2 == 0:
                r = stub.GetObject(
                    storage_pb2.GetObjectRequest(
                        bucket=_BUCKET, object="obj-%d" % (k % self.n_objects)
                    ),
                    timeout=10,
                )
                return r.name
            r = stub.ListObjects(
                storage_pb2.ListObjectsRequest(parent=_BUCKET), timeout=10
            )
            return len(r.objects)

        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
            read_futs = [pool.submit(read_stream, i) for i in range(self.n_objects)]
            meta_futs = [pool.submit(meta_op, k) for k in range(40)]
            for f in read_futs:
                name, got_crc = f.result(timeout=60)
                self.assertEqual(
                    self.expected[name], got_crc, "stream %r bytes wrong" % name
                )
            for f in meta_futs:
                f.result(timeout=60)  # no DeadlineExceeded == no starvation
```

- [ ] **Step 4: Run both arms green at the healthy pool**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_grpc_concurrency.py -q
```
Expected: both tests PASS. All client calls carry a `timeout=`, so any starvation surfaces as a clean `DeadlineExceeded` (a test failure), never a hung suite; the server is `stop()`ped and channels closed via `addCleanup`.

- [ ] **Step 5: Mutation-check — the guard MUST fail at pool=2 (the whole point)**

Temporarily hard-pin the pool small and confirm each arm reddens:
```bash
# In test_metadata_not_starved_by_held_streams, force the small pool:
#   os.environ["TESTBENCH_GRPC_THREADS"] = "2"   # < the 4 parked streams
# In test_parallel_streams_and_metadata_all_correct likewise force "2".
PYTHONPATH=. .venv/bin/python -m pytest tests/test_grpc_concurrency.py -q
```
Expected: Arm A FAILS with `DeadlineExceeded` on the interleaved `GetObject` (all workers parked; the readiness barrier still passes because the 2 parked streams that get workers signal, but the barrier waits on all 4 — so pin the barrier count down to `parked=2` alongside the pool pin, or observe the barrier's own timeout as the failure — either reddens). Arm B FAILS with `DeadlineExceeded` on the queued metadata / serialized streams. This reproduces exactly what the old `_GRPC_SERVER_THREAD_COUNT = 2` would have caused. **Revert the pins** back to `"8"`/`"32"` (and `parked` back to `4`). Record the observed failure in the commit body — a guard never seen to fail is not known to work.

- [ ] **Step 6: 3.8 parse, format, commit**

```bash
PYTHONPATH=. .venv/bin/python -c "import ast; ast.parse(open('tests/test_grpc_concurrency.py').read(), feature_version=(3,8))"
.venv/bin/isort --quiet tests/test_grpc_concurrency.py && .venv/bin/black --quiet tests/test_grpc_concurrency.py
git add tests/test_grpc_concurrency.py
git commit -m "test(concurrency): Mechanism 7 N-parallel-streams + metadata; starvation guard killed at pool=2"
```

**Safety gate:** memory harness EMPTY diff; file harness B≡C (one entry); no golden, allow-list untouched. Both arms ratio/bound-based and non-flaky (deterministic `state_lookup` occupancy barrier, generous per-RPC deadlines); starvation guard observed to fail at pool=2 and pass at the default.

---

### Task 4: Document Mechanisms 8 (real-GCS divergence) & 9 (downstream client) as manual/external

**Files:**
- Create: `tests/conformance/real_gcs_divergence.py`
- Modify: `README.md`, `.github/workflows/build.yaml` (comment only), `.claude/handoff.md`, `tests/test_durability_restart.py` (add the skip-guard test)

**Interfaces:**
- Produces: a runnable-but-default-skipped harness mode whose ONLY executed behaviour is a clean `SKIP` (returns 0) when `TESTBENCH_REAL_GCS_PROJECT` is unset; with credentials set it exits with a "wire it here" message (the trace-driving body is intentionally unimplemented, operator-run, out of CI scope). Imports NO `gcs`/`testbench` internals (over-the-wire only), preserving the harness import rule. Plus README/handoff prose that pins the gaps as *chosen, not forgotten*.

- [ ] **Step 1: Create the default-skipped divergence harness**

```python
# tests/conformance/real_gcs_divergence.py
"""Mechanism 8 (opt-in): cross-validate the conformance trace against a REAL
GCS bucket and report divergences.

NOT run in CI. It needs a real project, credentials, and money, so it is a
MANUALLY triggered job (spec Verification plan, Mechanism 8). It is INTENDED to
reuse the same black-box trace scripts and canonicalization as the emulator
harness, driving purely over the wire (importing NO testbench/gcs internals,
exactly like tests/conformance/harness.py, so the harness import rule is
preserved). That trace-driving body is INTENTIONALLY LEFT UNIMPLEMENTED: it is
operator-run and out of scope for autonomous CI, per the design.

The ONLY executed behaviour is the credential-less SKIP below (returns 0), which
keeps the module importable and CI-safe as a no-op. With TESTBENCH_REAL_GCS_
PROJECT set it exits with a "wire it here" SystemExit rather than silently
pretending to have run.

Run:  TESTBENCH_REAL_GCS_PROJECT=my-proj \\
      GOOGLE_APPLICATION_CREDENTIALS=/path/key.json \\
      PYTHONPATH=. .venv/bin/python -m tests.conformance.real_gcs_divergence
"""

import os
import sys


def main():
    project = os.environ.get("TESTBENCH_REAL_GCS_PROJECT")
    if not project:
        print(
            "SKIP: real-GCS divergence is a manual job; set "
            "TESTBENCH_REAL_GCS_PROJECT (+ credentials) to run it."
        )
        return 0
    # The trace-driving body (run trace_rest/trace_grpc against the real REST/
    # gRPC endpoint, apply the harness canonicalizer, print a per-interaction
    # divergence report) is intentionally NOT implemented here: it is an
    # operator-run job, not a CI gate, and known divergences (ACL/IAM
    # non-enforcement, signed-URL non-verification) are recorded as KNOWN gaps,
    # not failures. See README "Verification: manual/external jobs".
    raise SystemExit(
        "real-GCS divergence harness is operator-run and unimplemented: wire "
        "trace_rest/trace_grpc at the real endpoint here. See README."
    )


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Add a guard test that the module imports and skips with no credentials**

Add to `tests/test_durability_restart.py` a check that `real_gcs_divergence.main()` returns 0 and prints `SKIP` when `TESTBENCH_REAL_GCS_PROJECT` is unset — so the module stays importable and CI-safe (this credential-less path is the module's only executed behaviour):

```python
    def test_real_gcs_harness_skips_without_credentials(self):
        saved = os.environ.pop("TESTBENCH_REAL_GCS_PROJECT", None)
        try:
            from tests.conformance import real_gcs_divergence
            self.assertEqual(0, real_gcs_divergence.main())
        finally:
            if saved is not None:
                os.environ["TESTBENCH_REAL_GCS_PROJECT"] = saved
```

- [ ] **Step 3: Document Mechanisms 8 and 9 in `README.md`**

Add a section:

```markdown
## Verification: manual/external jobs

Two verification mechanisms from the design are intentionally NOT run in CI:

- **Mechanism 8 — real-GCS divergence (manual).** `tests/conformance/real_gcs_divergence.py`
  is the opt-in harness for running the conformance trace against a real GCS
  bucket and reporting divergences. It needs a project, credentials, and money,
  so it is operator-run, not per-commit; the trace-driving body is left
  unimplemented on purpose and the module is a no-op skip in CI. Known
  divergences (the testbench performs no ACL/IAM enforcement and no signed-URL
  verification) are recorded as KNOWN gaps, not failures.
  Run: `TESTBENCH_REAL_GCS_PROJECT=<proj> GOOGLE_APPLICATION_CREDENTIALS=<key> \
        PYTHONPATH=. python -m tests.conformance.real_gcs_divergence`.
  With the env var unset it skips and exits 0.

- **Mechanism 9 — downstream client (external).** The final acceptance check is
  the downstream application's own Rust-client smoke suite run against the
  emulator in both memory and file configurations. That suite lives in the
  application repository, not here; it is the last gate before adopting the file
  backend for local development.
```

- [ ] **Step 4: Note the CI stance and update the handoff**

In `.github/workflows/build.yaml`, add a comment near the `python-tests` step that the durability (`test_durability_restart.py`, `test_sigkill_upload.py`) and concurrency (`test_grpc_concurrency.py`) suites run under the existing Linux legs, and that Mechanism 8 is deliberately NOT a CI job. Update `.claude/handoff.md` to mark phase 7 complete, restate the invariants (memory digest `98fa2130…`, file B≡C one entry, zero new deps), and record that M8/M9 are chosen-not-forgotten manual/external gaps.

- [ ] **Step 5: Format and commit**

```bash
PYTHONPATH=. .venv/bin/python -c "import ast; ast.parse(open('tests/conformance/real_gcs_divergence.py').read(), feature_version=(3,8))"
.venv/bin/isort --quiet tests/conformance/real_gcs_divergence.py tests/test_durability_restart.py && .venv/bin/black --quiet tests/conformance/real_gcs_divergence.py tests/test_durability_restart.py
git add tests/conformance/real_gcs_divergence.py README.md .github/workflows/build.yaml .claude/handoff.md tests/test_durability_restart.py
git commit -m "docs(verification): document Mechanism 8 (manual real-GCS) & 9 (downstream); default-skipped harness"
```

**Safety gate:** memory harness EMPTY diff; file harness B≡C (one entry); no golden, allow-list untouched. `real_gcs_divergence.py` imports no internals and its only executed path is a clean credential-less skip; M8/M9 documented as manual/external.

---

## Final phase-7 verification (run before declaring done)

```bash
# Both pytest legs green (new modules included), macOS-hang test ignored locally only.
TESTBENCH_TEST_STORE=memory PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
TESTBENCH_TEST_STORE=file   PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
# Memory EMPTY diff, digest unchanged.
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store memory
cat tests/conformance/golden/rest.json tests/conformance/golden/grpc.json tests/conformance/golden/faults.json | shasum -a 256
#   -> 98fa2130d213b04478474c5918a6ba36e3e52838823189f4093a9161f72987a7
# File B==C, diverges only on create-bucket-traversal; allow-list still one entry.
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file
python -c "import json; print(len(json.load(open('tests/conformance/allowlist.json'))))"   # -> 1
# Linux gate.
nix develop --command make verify-linux
```

All must pass, with the concurrency starvation guard having been observed to fail at `TESTBENCH_GRPC_THREADS=2` (Task 3 Step 5) and every reintroduced defect (the `kill()` group-kill mutant, the corrupt-sidecar/inode-collision mutants in `filestore.py`, the `.gcs/uploads/` adoption mutant) reverted. No golden added, `tests/conformance/allowlist.json` and `tests/conformance/golden/` untouched, `setup.py` unchanged.