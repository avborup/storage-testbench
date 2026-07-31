# Bootstrap (startup wiring + deployment) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the six deployment environment variables the spec's "Configuration and deployment" table defines (`TESTBENCH_STORE`/`TESTBENCH_ROOT` are already done; this phase adds `TESTBENCH_BUCKETS`, `TESTBENCH_GRPC_PORT`, `TESTBENCH_GRPC_THREADS`, `TESTBENCH_FSYNC`) into the one startup seam that runs inside the gunicorn worker (`testbench/rest_server.py` module import), add the load-bearing single-worker assertion the file backend needs to protect its on-disk index, generalize the loopback-bind wiring just enough for container port-mapping, and ship the two-service docker compose (persistent named volume + ephemeral tmpfs) from one image — all while the MEMORY backend stays byte-identical (golden digest unmoved, EMPTY diff) and the FILE backend stays B ≡ C (allow-list at exactly one entry), because **every new env var, when UNSET, executes today's exact statements**.

**Architecture:** All boot wiring hangs off the single import-time anchor at `rest_server.py:50` (`db = _init_db_from_env()`), which runs once per gunicorn worker in the process that owns the in-memory index and (when booted) the gRPC server — the same reason `/start_grpc` starts gRPC in-worker (rest_server.py:183-189). In strict order after the DB is built: (1) claim a cross-worker file lock so the file backend fails loudly on a second worker rather than corrupting its index (a `containment.claim_worker_lock` using `O_CREAT|O_EXCL` + PID-liveness reclaim, POSIX-only, sitting beside the other fd primitives); (2) seed `TESTBENCH_BUCKETS` idempotently via a new `Database.seed_buckets` that mirrors the existing `insert_test_bucket` presence-check pattern, superseding the single `GOOGLE_CLOUD_CPP_STORAGE_TEST_BUCKET_NAME` auto-create without removing it; (3) boot the gRPC server on `TESTBENCH_GRPC_PORT` by reusing the exact `grpc_server.run(port, db, ...)` the `/start_grpc` route calls, setting the module globals so a later `/start_grpc` is a no-op. `TESTBENCH_GRPC_THREADS` (default 32, replacing the hardcoded `_GRPC_SERVER_THREAD_COUNT = 2`) is read inside `grpc_server.run()`, the shared entry both boot-start and `/start_grpc` use. `TESTBENCH_FSYNC` is a single process-global flag in `testbench/containment.py` (the choke point for every metadata write via `write_bytes_atomic`, and imported by `filemedia` for media writes) that gates `os.fsync` calls off by default so durability, not bytes, is what changes. The loopback guard is generalized with one opt-out (`TESTBENCH_ALLOW_NONLOOPBACK=1`) honored by both `testbench_run.py`'s REST bind refusal and `grpc_server._bind_host()`, so a container can bind `0.0.0.0` and be reachable through host-loopback-published ports; default-off keeps today's file-backend loopback safety intact. Two compose services (`gcs-dev` named volume, `gcs-test` tmpfs) from one image drive the same code path, differing only in env.

**Tech Stack:** Python 3.8–3.12, stdlib only (`os`, `atexit`, `json`, `subprocess`) plus the already-present deps; no new runtime dependency (`setup.py` unchanged). The phase-1 conformance harness with its `--store {memory,file}` byte-exact masked overlay; `docker` + `docker compose` (dev-shell tools, already in `flake.nix`) for the compose gate. `.venv/bin/python -m pytest`, `.venv/bin/isort`, `.venv/bin/black`, `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness` — nothing on the bare `PATH`.

## Global Constraints

- **Zero new RUNTIME dependencies.** `setup.py` stays as-is. Bootstrap uses only stdlib (`os`, `atexit`, `json`, `subprocess`) + already-present deps. `hypothesis`/`coverage` are dev deps in `flake.nix` only; `docker`/`docker-compose` are dev-shell tools, never in `setup.py`. Use `.venv/bin/python -m pytest`, `.venv/bin/isort`, `.venv/bin/black`, `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness` as the toolchain (**nothing is on the bare `PATH`**).
- **Python floor is 3.8.** Every new/edited file must parse under `ast.parse(feature_version=(3, 8))`. CI runs a 3.8–3.12 matrix. `os.open(..., dir_fd=)`, `os.fsync`, `os.kill(pid, 0)`, `atexit.register` are all present since ≤3.3.
- **The MEMORY backend must stay byte-identical.** `sha256(rest.json + grpc.json + faults.json) = 98fa2130d213b04478474c5918a6ba36e3e52838823189f4093a9161f72987a7` and `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness` (memory) must print `OK` for all three traces with an **EMPTY diff**. When the new env vars are UNSET and `TESTBENCH_STORE != file`, the import/startup path must execute exactly today's statements — no bucket seeding, no gRPC boot, no worker lock, no fsync, no bind-behaviour change.
- **The FILE backend must remain B ≡ C byte-identical.** `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file` must print `OK` for all three traces via the byte-exact masked comparison, diverging **only** on the single allow-listed `create-bucket-traversal` label. `tests/conformance/allowlist.json` holds **exactly one** entry (`stale_allowlist_labels` fails the gate if it ever stops diverging); no bootstrap change may add a second entry. REST `Content-Length`/framing and gRPC read chunk boundaries + the trailing-empty asymmetry must NOT move — in particular `TESTBENCH_GRPC_THREADS` changing the default thread count from 2 to 32 must not move any recorded chunk boundary (the harness drives sequential single-threaded traces; **verify** the diff stays empty, do not assume it).
- **`--regenerate` NEVER turns a red gate green.** A golden diff without a reviewed, intended behaviour change is a defect — diagnose, don't regenerate.
- **Mutation-check every guard clause.** After a guard passes, reintroduce the specific defect it guards and confirm the named test FAILS, then revert. **Defense-in-depth / equivalent-mutant carve-out** (from the phase-4/5 plans): a clause provably subsumed by a stricter downstream clause is exempt from individual-killability PROVIDED (a) the load-bearing clause it defers to *is* mutation-killed by a test, and (b) the redundant clause is documented in code as intentional defense-in-depth. Deleting a security/correctness backstop to make a metric go green is the wrong trade; documenting the equivalence honestly is the right one.
- **Nothing in `tests/conformance/` may import `gcs`/`testbench` internals except `emulator.py`.** All bootstrap unit tests live in `tests/` (top level) or drive the harness over the wire.
- **`gcs/bucket.py` `__validate_json_bucket_name` is DELIBERATELY LEFT UNCHANGED**, so the memory golden and A ≡ B hold; the file backend's own `validate_bucket_name` is the only check. A `TESTBENCH_BUCKETS` entry that is an illegal name therefore fails loudly at boot **on the file backend** (via `insert_bucket` → `FileStore.validate_bucket_name`), which is the desired behaviour.
- **Interpreter/OS/library-internals hazard.** `resource`/RSS units, gzip OS byte, and `crc32c` arch-sensitivity were the phase-4/5 traps; the bootstrap trap is that the single-worker lock and gRPC boot-at-import both rely on the app-factory running **without `--preload`** (each worker imports `testbench.rest_server` fresh — confirmed by `testbench/__init__.py:27-37` eagerly importing `rest_server`, whose module body runs `db = _init_db_from_env()` at :50). A future `--preload` would break both. `make verify-linux` reproduces the Linux gate; treat CI as authoritative.
- **Formatting is `isort` then `black`, in that order** (`isort==5.12.0`, `black==22.3.0`), run from `.venv/bin`.
- **Single gunicorn worker, `--reload` on.** Never edit a `.py` file while a harness run or emulator-backed test is in flight; it restarts the worker mid-trace and wipes state.

### The golden digests (irreproducible baselines)

- MEMORY golden digest (unchanged by every task): `98fa2130d213b04478474c5918a6ba36e3e52838823189f4093a9161f72987a7`.
- FILE backend: B ≡ C via the byte-exact masked overlay, allow-list held at exactly one entry (`create-bucket-traversal`), on every task.

### The phase-6 exit gate (what "done" means)

From the spec's per-phase gates (row 6), phase 6 is green when **all** hold:

1. `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness` (memory) prints `OK` for all three traces with an EMPTY diff (digest `98fa2130…`).
2. `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file` prints `OK` via the byte-exact masked comparison, diverging only on the one allow-listed `create-bucket-traversal` label.
3. `PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py` is green under both `TESTBENCH_TEST_STORE=memory` and `TESTBENCH_TEST_STORE=file`.
4. The bootstrap suites pass — `tests/test_grpc_threads.py`, `tests/test_fsync.py`, `tests/test_seed_buckets.py`, `tests/test_grpc_boot.py`, `tests/test_worker_lock.py`, `tests/test_bind_host.py`, `tests/test_run_argv.py` — each new guard clause mutation-checked (with the documented carve-out).
5. Boot-time gRPC (`TESTBENCH_GRPC_PORT`) and bucket seeding (`TESTBENCH_BUCKETS`) are demonstrated end-to-end; the single-worker assertion fires on a second worker and is mutation-checked; `docker compose config` validates both services and the manual compose smoke (both services reachable, `gcs-dev` buckets seeded, gRPC up at boot) is recorded in the handoff.
6. `nix develop --command make verify-linux` is green on both legs; `setup.py` unchanged; nothing in `tests/conformance/` imports internals except `emulator.py`.

---

## File Structure

- **Modify `testbench/grpc_server.py`** — replace the hardcoded `_GRPC_SERVER_THREAD_COUNT = 2` (:46) with `_grpc_thread_count()` reading `TESTBENCH_GRPC_THREADS` (default 32), used at `run()`'s `ThreadPoolExecutor` (:1395); add the `TESTBENCH_ALLOW_NONLOOPBACK` opt-out to `_bind_host()` (:1388-1390). No change to `run()`'s `(port, server)` return contract.
- **Modify `testbench/containment.py`** — add `import atexit` to the module head (isort-ordered with `os`, `shutil`; it is NOT currently imported — verified: containment.py imports only `os`, `shutil`, `from testbench import pathing`). Add the process-global `FSYNC` flag (env-derived at import) + `maybe_fsync(fd)` helper; fsync inside `write_bytes_atomic` (:63-75) only when set (maybe_fsync is called UNCONDITIONALLY so its internal `if FSYNC` guard is the single load-bearing gate); add `claim_worker_lock(root)` (`O_CREAT|O_EXCL` marker + PID-liveness reclaim, fail-safe refuse on an unreadable/nascent marker + `atexit` release). Fd/POSIX primitives only; no name/media logic.
- **Modify `testbench/filestore.py`** — `_index_names` (:61-62) filters `os.listdir(self._root)` to **directory entries only**, matching `rebuild_index`'s isdir discipline (:389). This keeps the `.gcs-worker.lock` sibling FILE (and any `.tmp-*`) out of `cleared()` (:522-526) and `bucket_deleted` (:127), both of which pass `_index_names()` into `containment.constrained_rmtree` — whose non-directory guard (containment.py:152) would otherwise **raise** on the lock marker. No behaviour change for real bucket dirs, so B ≡ C is unmoved.
- **Modify `testbench/filemedia.py`** — `finalize` (:193-205) and `seal` (:218-229) call `containment.maybe_fsync(append_fd)` before closing the append fd, plus (in `finalize` only) a dest-dir fsync, when `FSYNC` is on. `append`/reads unchanged. Default off = zero extra syscalls.
- **Modify `testbench/database.py`** — add `seed_buckets(self, names)` after `insert_test_bucket` (:233-255), mirroring its presence-check + `Bucket.init` + `insert_bucket` flow; idempotent (skip names already present), so a restart against a persistent named volume does not error. `insert_test_bucket` left intact.
- **Modify `testbench/rest_server.py`** — factor the `/start_grpc` (:181-199) `run()` call into a shared `_start_grpc(port, echo_metadata)` setting the module globals; after `db = _init_db_from_env()` (:50) add the strictly env-gated boot block: single-worker lock (file only) → `TESTBENCH_BUCKETS` seed → `TESTBENCH_GRPC_PORT` boot. The lock claim itself moves into `_init_db_from_env`'s file branch (earliest point the root is known). Every step guarded by `if os.environ.get(VAR)` / `if TESTBENCH_STORE == file`.
- **Modify `testbench_run.py`** — add explicit `--workers=1` to the gunicorn argv (:57-67, belt); honor `TESTBENCH_ALLOW_NONLOOPBACK` in the loopback bind refusal (:37-44, suspenders for the container case).
- **Modify `Dockerfile`** — no CMD change required (the compose services supply `TESTBENCH_ALLOW_NONLOOPBACK=1`, so the existing `0.0.0.0 9000` bind is honored); add a one-line comment documenting the coupling. `testbench_run.py`'s new `--workers=1` flows through the CMD unchanged.
- **Create `docker-compose.yml`** — `gcs-dev` (file, named volume `gcs-data:/data`, `TESTBENCH_BUCKETS=audio,transcripts,models`, `TESTBENCH_GRPC_PORT=9001`, host-loopback-published ports) + `gcs-test` (file, `tmpfs: /data`, `TESTBENCH_BUCKETS=""`); top-level `volumes: gcs-data:`. Both `build: .`, same code path, env-only difference.
- **Create `tests/test_grpc_threads.py` / `test_fsync.py` / `test_seed_buckets.py` / `test_grpc_boot.py` / `test_worker_lock.py` / `test_bind_host.py` / `test_run_argv.py`** — focused unit + mutation suites for each wiring (`test_worker_lock.py` also carries the `cleared()`-with-lock-marker regression).

Each task ends with the safety gate below. **Every task** must land with the memory harness EMPTY diff (digest `98fa2130…`) AND the file harness B ≡ C (allow-list at one entry), because each new behaviour is env-gated OFF by default.

---

### Task 1: `TESTBENCH_GRPC_THREADS` — default 32, replace the hardcoded 2 (prove goldens unmoved on both backends)

**Files:**
- Modify: `testbench/grpc_server.py` (`_GRPC_SERVER_THREAD_COUNT = 2` :46; `run()` `ThreadPoolExecutor(max_workers=...)` :1394-1396)
- Create: `tests/test_grpc_threads.py`

**Interfaces:**
- Produces: `grpc_server._grpc_thread_count() -> int` reading `int(os.environ.get("TESTBENCH_GRPC_THREADS", "32"))`; `run(port, database, echo_metadata=False)` unchanged in signature and `(port, server)` return, but its executor is sized by `_grpc_thread_count()`. `_GRPC_SERVER_THREAD_COUNT = 2` is removed (referenced only at :1395 today — verify with the grep in Step 3).

- [ ] **Step 1: Write the failing unit test**

```python
# tests/test_grpc_threads.py
import os
import unittest

from testbench import grpc_server


class TestGrpcThreadCount(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("TESTBENCH_GRPC_THREADS", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("TESTBENCH_GRPC_THREADS", None)
        else:
            os.environ["TESTBENCH_GRPC_THREADS"] = self._saved

    def test_default_is_32(self):
        os.environ.pop("TESTBENCH_GRPC_THREADS", None)
        self.assertEqual(32, grpc_server._grpc_thread_count())

    def test_env_overrides(self):
        os.environ["TESTBENCH_GRPC_THREADS"] = "7"
        self.assertEqual(7, grpc_server._grpc_thread_count())

    def test_run_sizes_executor_from_env(self):
        # run() must actually use the count. Bind an ephemeral port on loopback,
        # then read the ThreadPoolExecutor's configured max_workers off the server.
        # NOTE: server._state.thread_pool._max_workers is a grpcio PRIVATE attribute
        # chain, stable under the pinned grpcio==1.70.0 (setup.py:50). If a version
        # bump moves it, re-pin the observable by passing a futures.ThreadPoolExecutor
        # via a seam and asserting on that instead.
        os.environ["TESTBENCH_GRPC_THREADS"] = "5"
        import testbench.database

        db = testbench.database.Database.init()
        port, server = grpc_server.run(0, db)
        try:
            self.assertEqual(5, server._state.thread_pool._max_workers)
        finally:
            server.stop(None)
```

- [ ] **Step 2: Run to see it fail** — `PYTHONPATH=. .venv/bin/python -m pytest tests/test_grpc_threads.py -q` → FAIL: `AttributeError: module 'testbench.grpc_server' has no attribute '_grpc_thread_count'`.

- [ ] **Step 3: Implement**

```python
# testbench/grpc_server.py -- delete the hardcoded constant at :46 and add:
def _grpc_thread_count():
    # Two long-lived streaming RPCs (ReadObject/BidiRead/BidiWrite) each hold a
    # thread for a whole transfer; a shared container + parallel suite starves
    # every other gRPC call at 2 threads. Default 32 (spec gRPC-concurrency).
    return int(os.environ.get("TESTBENCH_GRPC_THREADS", "32"))
```

```python
# testbench/grpc_server.py -- run() :1394-1396:
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=_grpc_thread_count())
    )
```

Confirm the old name is fully removed:
```bash
grep -n "_GRPC_SERVER_THREAD_COUNT" testbench/  # expect: no matches
```

- [ ] **Step 4: Run to green + prove both goldens are UNMOVED (this is the load-bearing check)**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_grpc_threads.py tests/test_grpc_server.py -q
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness              # memory: OK all three, EMPTY diff, digest 98fa2130…
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file # file: OK, diverges only on create-bucket-traversal
```
The emulator does NOT set `TESTBENCH_GRPC_THREADS`, so both harness legs now run the gRPC server with 32 threads instead of 2. Because the traces are sequential single-threaded scripts, chunk boundaries are thread-count-independent; the diffs must be exactly as before. If either diff is non-empty, STOP — do not `--regenerate`; a moved boundary would be a real defect.

- [ ] **Step 5: Mutation-check**
- Change the default in `_grpc_thread_count` from `"32"` to `"2"`. Expected: `test_default_is_32` FAILS. Revert.
- Drop the env read (hardcode `return 32`). Expected: `test_env_overrides` and `test_run_sizes_executor_from_env` FAIL. Revert.

- [ ] **Step 6: 3.8 parse, format, commit**

```bash
PYTHONPATH=. .venv/bin/python -c "import ast; ast.parse(open('testbench/grpc_server.py').read(), feature_version=(3,8))"
.venv/bin/isort testbench/grpc_server.py tests/test_grpc_threads.py && .venv/bin/black testbench/grpc_server.py tests/test_grpc_threads.py
git add testbench/grpc_server.py tests/test_grpc_threads.py
git commit -m "feat(bootstrap): TESTBENCH_GRPC_THREADS (default 32) replaces hardcoded 2-thread pool"
```

**Safety gate:** memory harness EMPTY diff (digest `98fa2130…`); file harness B ≡ C (one entry) — **explicitly re-verified after the 2→32 default change**, not assumed. Thread count read via `_grpc_thread_count`, mutation-checked on default and env-read.

---

### Task 2: `TESTBENCH_FSYNC` — opt-in fsync, OFF by default (zero extra syscalls when unset)

**Files:**
- Modify: `testbench/containment.py` (module head; `write_bytes_atomic` :63-75)
- Modify: `testbench/filemedia.py` (`finalize` :193-205, `seal` :218-229)
- Create: `tests/test_fsync.py`

**Interfaces:**
- Produces: `containment.FSYNC` (module-level bool, `os.environ.get("TESTBENCH_FSYNC") == "1"` at import; referenced by attribute at call time so tests can flip it) and `containment.maybe_fsync(fd) -> None` (`if FSYNC: os.fsync(fd)`). `write_bytes_atomic` calls `maybe_fsync(handle.fileno())` (after `handle.flush()`) before `os.replace`, and `maybe_fsync(dir_fd)` after — **both calls are UNCONDITIONAL**, so `maybe_fsync`'s own `if FSYNC` guard is the SINGLE gate (no double-guard; see the mutation-check note). `FileMedia.finalize` fsyncs the `append_fd` and the dest dir; `FileMedia.seal` fsyncs the `append_fd` — each before closing, via `maybe_fsync`, so no-op unless `FSYNC`. Signatures are unchanged; `FileStore.__init__(self, root)` is NOT given an fsync kwarg (see decision below), so it still constructs with a bare `root`.

**Decision (recorded):** `TESTBENCH_FSYNC` is a *process/deployment* concern, not per-`FileStore` state, and the writes it governs live in `containment`/`filemedia`, not in `FileStore`. Threading an `fsync=` param through `FileStore.__init__` would add dead surface (`FileStore` never fsyncs directly) and risk moving the `FileStore(root)` call sites in `rest_server._init_db_from_env`, `conftest.py`, and `tests/test_store.py`. A single module-global in `containment` (the choke point that BOTH metadata and, via import, media writes already funnel through) is the minimal, testable source of truth and keeps `FileStore(root)` byte-identical. The memory backend never imports `containment`, so the flag cannot affect it.

**Decision (recorded, single-gate):** the fsync calls in `write_bytes_atomic` are invoked UNCONDITIONALLY (not wrapped in a call-site `if FSYNC:`). A call-site guard PLUS `maybe_fsync`'s internal guard would be a double-guard: dropping the internal guard would then leave the call-site guard still suppressing the call, so a mutation removing the internal guard would SURVIVE on the metadata path and the "no fsync when off" test would still pass — a false mutation-check. Making `maybe_fsync` the single gate keeps the guard genuinely load-bearing (and mutation-killable) on every path. `handle.flush()` is unconditional and harmless when off (the `with` close would flush anyway); it changes no bytes, only guarantees the userspace buffer is on the fd before an `FSYNC=1` fsync.

- [ ] **Step 1: Write the failing test (syscall-counting; default OFF must add zero fsyncs)**

```python
# tests/test_fsync.py
import os
import tempfile
import unittest

from testbench import containment
from testbench.filemedia import FileMedia


class _FsyncCounter:
    def __init__(self):
        self.calls = 0

    def __call__(self, fd):
        self.calls += 1


class TestFsyncGate(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.dfd = os.open(self.root, os.O_RDONLY)
        self.addCleanup(os.close, self.dfd)
        self._saved_flag = containment.FSYNC
        self._real_fsync = os.fsync
        self.counter = _FsyncCounter()
        os.fsync = self.counter  # count fsyncs regardless of flag
        self.addCleanup(self._restore)

    def _restore(self):
        os.fsync = self._real_fsync
        containment.FSYNC = self._saved_flag

    def test_metadata_write_does_not_fsync_when_off(self):
        containment.FSYNC = False
        containment.write_bytes_atomic(self.dfd, "m", b"payload")
        self.assertEqual(0, self.counter.calls)
        self.assertEqual(b"payload", open(os.path.join(self.root, "m"), "rb").read())

    def test_metadata_write_fsyncs_file_and_dir_when_on(self):
        containment.FSYNC = True
        containment.write_bytes_atomic(self.dfd, "m", b"payload")
        self.assertEqual(2, self.counter.calls)  # temp-file fd + parent dir_fd
        self.assertEqual(b"payload", open(os.path.join(self.root, "m"), "rb").read())

    def test_finalize_does_not_fsync_when_off(self):
        containment.FSYNC = False
        os.mkdir(os.path.join(self.root, "dst"))
        ddfd = os.open(os.path.join(self.root, "dst"), os.O_RDONLY)
        self.addCleanup(os.close, ddfd)
        fm = FileMedia.new_staging(self.dfd, "u")
        fm.append(b"abc")
        fm.finalize((ddfd, "final"))
        self.assertEqual(0, self.counter.calls)

    def test_finalize_fsyncs_media_when_on(self):
        containment.FSYNC = True
        os.mkdir(os.path.join(self.root, "dst"))
        ddfd = os.open(os.path.join(self.root, "dst"), os.O_RDONLY)
        self.addCleanup(os.close, ddfd)
        fm = FileMedia.new_staging(self.dfd, "u")
        fm.append(b"abc")
        fm.finalize((ddfd, "final"))
        self.assertGreaterEqual(self.counter.calls, 1)  # append_fd fsynced pre-close
```

- [ ] **Step 2: Run to see it fail** — `AttributeError: module 'testbench.containment' has no attribute 'FSYNC'`.

- [ ] **Step 3: Implement**

```python
# testbench/containment.py -- module head (after the imports):
FSYNC = os.environ.get("TESTBENCH_FSYNC") == "1"


def maybe_fsync(fd):
    # No-op unless TESTBENCH_FSYNC=1. fsync changes DURABILITY, not bytes, so the
    # B==C golden and the memory digest are unaffected; this internal guard is the
    # SINGLE gate (callers invoke unconditionally) so the default path adds ZERO
    # syscalls (spec: os.replace ordering is the default durability guarantee).
    # Referenced as a module attribute at call time so a test can flip
    # `containment.FSYNC` without re-import.
    if FSYNC:
        os.fsync(fd)
```

```python
# testbench/containment.py -- write_bytes_atomic (:63-75), single-gated fsync:
def write_bytes_atomic(dir_fd, name, data):
    tmp = ".tmp-%d-%s" % (os.getpid(), name)
    fd = safe_open(dir_fd, tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()              # ensure the userspace buffer is on the fd
            maybe_fsync(handle.fileno())  # UNCONDITIONAL; no-op unless FSYNC
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        maybe_fsync(dir_fd)             # UNCONDITIONAL; durably link the new name in
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise
```

```python
# testbench/filemedia.py -- finalize (:193-205), fsync media + dest dir before it is referenced:
    def finalize(self, dest):
        if self._staging is None:
            return None
        sdir, sname, append_fd = self._staging
        dst_dir_fd, dst_name = dest
        containment.maybe_fsync(append_fd)      # data durable before the rename
        os.close(append_fd)
        containment.promote(sdir, sname, dst_dir_fd, dst_name)
        containment.maybe_fsync(dst_dir_fd)     # rename durable in the dest dir
        os.close(sdir)
        self._md5 = self._md5.digest()
        self._staging = None
        return None

# filemedia.py -- seal (:218-229): add `containment.maybe_fsync(append_fd)` immediately
# before `os.close(append_fd)` (the dest hardlink established at link_into already points
# at the inode, so the append_fd fsync makes the appended bytes durable). seal does NOT
# fsync a dest dir -- the destination NAME was created earlier by link_into, not by seal.
```

- [ ] **Step 4: Run to green + both harness legs (default OFF)**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_fsync.py -q
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness              # memory EMPTY diff
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file # B==C, one entry
```
Neither harness sets `TESTBENCH_FSYNC`, so `FSYNC` is `False` and every fsync branch is skipped — bytes and timing unchanged.

- [ ] **Step 5: Mutation-check**
- In `maybe_fsync`, drop the `if FSYNC:` guard (always fsync). Expected: `test_metadata_write_does_not_fsync_when_off` AND `test_finalize_does_not_fsync_when_off` FAIL (fsync called when off) — this now genuinely fails on the metadata path because the call site is unconditional and `maybe_fsync` is the single gate. Revert.
- In `write_bytes_atomic`, delete the post-`os.replace` `maybe_fsync(dir_fd)`. Expected: `test_metadata_write_fsyncs_file_and_dir_when_on` FAILS (count 1, not 2). Revert.

- [ ] **Step 6: 3.8 parse, format, commit**

```bash
PYTHONPATH=. .venv/bin/python -c "import ast; [ast.parse(open(f).read(), feature_version=(3,8)) for f in ('testbench/containment.py','testbench/filemedia.py')]"
.venv/bin/isort testbench/containment.py testbench/filemedia.py tests/test_fsync.py && .venv/bin/black testbench/containment.py testbench/filemedia.py tests/test_fsync.py
git add testbench/containment.py testbench/filemedia.py tests/test_fsync.py
git commit -m "feat(bootstrap): TESTBENCH_FSYNC opt-in fsync for metadata + media writes (off by default)"
```

**Safety gate:** memory harness EMPTY diff; file harness B ≡ C (one entry) — fsync default OFF adds zero syscalls, so bytes and boundaries are unmoved. The single `if FSYNC` guard is mutation-checked on both the metadata and media paths (no double-guard survivor). `FileStore(root)` still constructs unchanged (no new param).

---

### Task 3: `TESTBENCH_BUCKETS` — idempotent startup seeding (supersedes the single auto-create)

**Files:**
- Modify: `testbench/database.py` (add `seed_buckets` after `insert_test_bucket` :233-255)
- Create: `tests/test_seed_buckets.py`

**Interfaces:**
- Produces: `Database.seed_buckets(self, names)` — for each name in `names`, under `_resources_lock`: skip if `self._buckets.get(self.__bucket_key(name, None)) is not None` (idempotent), else `gcs.bucket.Bucket.init(FakeRequest(name=...))` + `self.insert_bucket(bucket, None)`. No metageneration/versioning overrides (plain `Bucket.init` defaults — see decision). `insert_test_bucket` is untouched.

**Decision (recorded):** `seed_buckets` seeds *plain* buckets with `Bucket.init` defaults; it does NOT copy the `metageneration=4` / `versioning.enabled=True` that `insert_test_bucket` sets for the `google-cloud-cpp` well-known bucket, because those are that test suite's expectations, not a general seeding contract. Per-bucket JSON overrides (versioning/soft-delete) are a spec "nice-to-have" and are **out of scope** to avoid a new parsing surface (spec: `TESTBENCH_BUCKETS` is development-only; tests create their own UUID buckets). `insert_test_bucket` stays intact so the `GOOGLE_CLOUD_CPP_STORAGE_TEST_BUCKET_NAME` path and the memory golden are unchanged when `TESTBENCH_BUCKETS` is unset.

**Note on the actual env-gated call site:** the `if os.environ.get("TESTBENCH_BUCKETS")` guard and the empty-filter live in `rest_server.py` and land in Task 4's boot block (they share the same import anchor). This task adds and unit-tests `seed_buckets` in isolation; Task 4 wires it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_seed_buckets.py
import unittest

import testbench.database


class TestSeedBuckets(unittest.TestCase):
    def _db(self):
        return testbench.database.Database.init()  # memory backend

    def test_seeds_each_named_bucket(self):
        db = self._db()
        db.seed_buckets(["audio", "transcripts", "models"])
        for name in ("audio", "transcripts", "models"):
            self.assertIn("projects/_/buckets/%s" % name, db._buckets)

    def test_is_idempotent_across_calls(self):
        db = self._db()
        db.seed_buckets(["audio"])
        db.seed_buckets(["audio", "transcripts"])   # 'audio' already present -> skip
        self.assertIn("projects/_/buckets/audio", db._buckets)
        self.assertIn("projects/_/buckets/transcripts", db._buckets)

    def test_empty_list_creates_nothing(self):
        db = self._db()
        before = dict(db._buckets)
        db.seed_buckets([])
        self.assertEqual(before, db._buckets)
```

- [ ] **Step 2: Run to see it fail** — `AttributeError: 'Database' object has no attribute 'seed_buckets'`.

- [ ] **Step 3: Implement (mirror `insert_test_bucket`)**

```python
# testbench/database.py -- immediately after insert_test_bucket (:255):
    def seed_buckets(self, names):
        """Create each development bucket idempotently at startup (TESTBENCH_BUCKETS).
        Supersedes the single GOOGLE_CLOUD_CPP_STORAGE_TEST_BUCKET_NAME auto-create
        (insert_test_bucket, kept intact) as the multi-bucket mechanism. Idempotent:
        a name already present (e.g. rehydrated from a persistent named volume by
        FileStore.rebuild_index on restart) is skipped, so re-seeding never hits the
        already_exists error. Plain Bucket.init defaults (no metageneration/versioning
        override -- that override is specific to the cpp well-known bucket). On the
        FILE backend, insert_bucket -> FileStore.validate_bucket_name rejects an
        illegal name, so a bad TESTBENCH_BUCKETS entry fails LOUDLY at boot."""
        for name in names:
            with self._resources_lock:
                if self._buckets.get(self.__bucket_key(name, None)) is not None:
                    continue
                request = testbench.common.FakeRequest(
                    args={}, data=json.dumps({"name": name})
                )
                bucket, _ = gcs.bucket.Bucket.init(request, None)
                self.insert_bucket(bucket, None)
```

(`_resources_lock` is an `RLock`, so the nested `insert_bucket` re-acquire under the same lock is fine. `__bucket_key` is the name-mangled private used by `insert_test_bucket:245`; reference it inside the class the same way.)

- [ ] **Step 4: Run to green + harness legs unchanged (seed_buckets is unwired)**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_seed_buckets.py tests/test_store.py -q
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness              # memory EMPTY diff
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file # B==C, one entry
```
`seed_buckets` has no caller yet, so both harness legs are untouched; `insert_test_bucket` is unchanged.

- [ ] **Step 5: Mutation-check**
- Delete the `if self._buckets.get(...) is not None: continue` idempotency guard. Expected: `test_is_idempotent_across_calls` FAILS (the second call re-inserts `audio` and `insert_bucket` returns `already_exists`, which the test detects as a raised/errored second seed — assert on it explicitly if `already_exists(None)` is non-raising: capture the return and assert it is an error). Revert.

- [ ] **Step 6: 3.8 parse, format, commit**

```bash
PYTHONPATH=. .venv/bin/python -c "import ast; ast.parse(open('testbench/database.py').read(), feature_version=(3,8))"
.venv/bin/isort testbench/database.py tests/test_seed_buckets.py && .venv/bin/black testbench/database.py tests/test_seed_buckets.py
git add testbench/database.py tests/test_seed_buckets.py
git commit -m "feat(bootstrap): Database.seed_buckets for idempotent TESTBENCH_BUCKETS startup seeding"
```

**Safety gate:** memory harness EMPTY diff; file harness B ≡ C (one entry) — `seed_buckets` is unwired and `insert_test_bucket` is intact, so nothing observable moves. Idempotency guard mutation-checked.

---

### Task 4: `TESTBENCH_GRPC_PORT` boot-start + wire `TESTBENCH_BUCKETS` at the import anchor

**Files:**
- Modify: `testbench/rest_server.py` (`start_grpc` route :181-199; the module anchor after `db = _init_db_from_env()` :50; globals `grpc_port`/`grpc_service` :54-55)
- Create: `tests/test_grpc_boot.py`

**Interfaces:**
- Produces: `rest_server._start_grpc(port, echo_metadata=False) -> None` — the shared helper that both the `/start_grpc` route and boot-start call; it sets the module globals `grpc_port`/`grpc_service` via `testbench.grpc_server.run(int(port), db, echo_metadata=...)`, guarded by `if grpc_port == 0` so it is idempotent. `rest_server._bootstrap_from_env() -> None` — the env-gated boot block called once at import after `db`: seeds `TESTBENCH_BUCKETS` (comma-split, empties filtered), then boot-starts gRPC on `TESTBENCH_GRPC_PORT`. When both vars are unset it is a complete no-op.

- [ ] **Step 1: Write the failing test (stub `grpc_server.run`; prove globals, idempotency, empty-filter)**

```python
# tests/test_grpc_boot.py
import os
import unittest

import testbench.rest_server as rs


class _RunSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, port, database, echo_metadata=False):
        self.calls.append((port, echo_metadata))
        return (port or 50051, object())  # (bound_port, fake server)


class TestGrpcBoot(unittest.TestCase):
    def setUp(self):
        self._saved = (rs.grpc_port, rs.grpc_service)
        rs.grpc_port, rs.grpc_service = 0, None
        self._real_run = rs.testbench.grpc_server.run
        self.spy = _RunSpy()
        rs.testbench.grpc_server.run = self.spy
        self.addCleanup(self._restore)

    def _restore(self):
        rs.testbench.grpc_server.run = self._real_run
        rs.grpc_port, rs.grpc_service = self._saved

    def test_start_grpc_sets_module_globals(self):
        rs._start_grpc(9001)
        self.assertEqual([(9001, False)], self.spy.calls)
        self.assertEqual(9001, rs.grpc_port)      # MODULE global, not a local
        self.assertIsNotNone(rs.grpc_service)

    def test_boot_then_start_grpc_route_is_a_noop(self):
        # boot-start claims the port; a later /start_grpc must NOT start a 2nd server.
        rs._start_grpc(9001)
        rs._start_grpc(0)                         # simulates the route's run() call
        self.assertEqual(1, len(self.spy.calls))  # exactly one server started

    def test_seed_and_boot_are_gated(self):
        for var in ("TESTBENCH_BUCKETS", "TESTBENCH_GRPC_PORT"):
            os.environ.pop(var, None)
        rs.grpc_port, rs.grpc_service = 0, None
        rs._bootstrap_from_env()
        self.assertEqual([], self.spy.calls)      # nothing booted when unset
        self.assertEqual(0, rs.grpc_port)

    def test_empty_buckets_var_creates_nothing(self):
        os.environ["TESTBENCH_BUCKETS"] = ""       # gcs-test sets this
        seen = []
        real_seed = rs.db.seed_buckets
        rs.db.seed_buckets = lambda names: seen.extend(names)
        try:
            rs._bootstrap_from_env()
        finally:
            rs.db.seed_buckets = real_seed
        self.assertEqual([], seen)                 # "" -> filtered -> no names
```

- [ ] **Step 2: Run to see it fail** — `AttributeError: module 'testbench.rest_server' has no attribute '_start_grpc'`.

- [ ] **Step 3: Implement — factor the route, add the boot block (EXACT definition order)**

The import-time `_bootstrap_from_env()` call references `_start_grpc`, `db`, `grpc_port`, `grpc_service`, so all four must exist before it runs. Lay the module out in this precise order:

1. `def _start_grpc(port, echo_metadata=False): ...` (references the globals `grpc_port`/`grpc_service`/`db` — Python resolves globals at call time, so the *definitions* below need only exist before the CALL, not before the `def`).
2. `def _bootstrap_from_env(): ...`
3. `db = _init_db_from_env()`
4. `retry_test = testbench.common.gen_retry_test_decorator(db)`
5. `grpc_port = 0`
6. `grpc_service = None`
7. `_bootstrap_from_env()`  ← the LAST line of the block, after 1–6 are all bound.

```python
# testbench/rest_server.py -- (1) shared helper, defined BEFORE the anchor:
def _start_grpc(port, echo_metadata=False):
    # Shared by the /start_grpc route and TESTBENCH_GRPC_PORT boot-start. Sets the
    # MODULE globals so a later /start_grpc sees grpc_port != 0 and is a no-op --
    # if this set locals instead, a second server would start on a random port.
    global grpc_port, grpc_service
    if grpc_port == 0:
        grpc_port, grpc_service = testbench.grpc_server.run(
            int(port), db, echo_metadata=echo_metadata
        )


# (2) env-gated boot block, defined BEFORE the anchor:
def _bootstrap_from_env():
    # Runs once per gunicorn worker, at import, AFTER db is built and (for the file
    # backend) its index is hydrated and the single-worker lock is held. Every step
    # is env-gated so that with all new vars UNSET this is a complete no-op and the
    # import path is byte-identical to today.
    buckets = os.environ.get("TESTBENCH_BUCKETS")
    if buckets:                                   # "" and unset both -> no seeding
        db.seed_buckets([n for n in buckets.split(",") if n])
    port = os.environ.get("TESTBENCH_GRPC_PORT")
    if port:
        _start_grpc(port)


# (3)-(7) the anchor, in this exact order:
db = _init_db_from_env()
# retry_test decorates a routing function to handle the Retry Test API, with
# method names based on the JSON API
retry_test = testbench.common.gen_retry_test_decorator(db)
grpc_port = 0
grpc_service = None
_bootstrap_from_env()   # LAST -- db, grpc_port, grpc_service now all bound
```

```python
# testbench/rest_server.py -- start_grpc route (:181-199), now delegating:
@root.route("/start_grpc")
def start_grpc():
    # gRPC must start inside the gunicorn worker so it shares this process's single
    # Database (a pre-fork server would get a copied, divergent index).
    _start_grpc(
        flask.request.args.get("port", "0"),
        echo_metadata=flask.request.args.get("echo-metadata", False),
    )
    return str(grpc_port)
```

(The single-worker lock claim lands in Task 5, inside `_init_db_from_env`'s file branch, so by the time `_bootstrap_from_env` runs the lock is already held.)

- [ ] **Step 4: Run to green + both harness legs unchanged (vars unset)**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_grpc_boot.py -q
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness              # memory EMPTY diff
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file # B==C, one entry
```
The harness/emulator sets neither `TESTBENCH_BUCKETS` nor `TESTBENCH_GRPC_PORT`, so `_bootstrap_from_env` is a no-op and the harness still boots gRPC via `/start_grpc` exactly as before — byte-identical.

- [ ] **Step 5: End-to-end proof of boot-start (real gRPC, real port)**

```bash
# Boot the file backend with a fixed gRPC port + seeded buckets, then confirm both.
TMP=$(mktemp -d)
TESTBENCH_STORE=file TESTBENCH_ROOT=$TMP TESTBENCH_BUCKETS=audio,models \
  TESTBENCH_GRPC_PORT=9001 TESTBENCH_ALLOW_NONLOOPBACK=0 \
  PYTHONPATH=. .venv/bin/python testbench_run.py 127.0.0.1 9000 4 &
sleep 2
curl -s http://127.0.0.1:9000/storage/v1/b?project=test-project | python -m json.tool | grep -E '"name"'  # expect audio + models
python - <<'PY'  # gRPC is already listening at boot -- no /start_grpc curl needed
import grpc; grpc.insecure_channel("127.0.0.1:9001").subscribe(lambda c: None); print("grpc up")
PY
kill %1
```
Expected: the two seeded buckets are listed; the gRPC channel connects without any `/start_grpc` call. (`TESTBENCH_ALLOW_NONLOOPBACK=0` keeps the loopback bind; Task 6 adds the container opt-out.)

- [ ] **Step 6: Mutation-check**
- In `_start_grpc`, change `global grpc_port, grpc_service` to set locals (drop the `global`). Expected: `test_start_grpc_sets_module_globals` FAILS (module `grpc_port` stays 0) and `test_boot_then_start_grpc_route_is_a_noop` FAILS (a second server starts). Revert.
- In `_bootstrap_from_env`, drop the `if n` empty-filter (`buckets.split(",")`). Expected: `test_empty_buckets_var_creates_nothing` FAILS (`[""]` reaches `seed_buckets`). Revert.

- [ ] **Step 7: 3.8 parse, format, commit**

```bash
PYTHONPATH=. .venv/bin/python -c "import ast; ast.parse(open('testbench/rest_server.py').read(), feature_version=(3,8))"
.venv/bin/isort testbench/rest_server.py tests/test_grpc_boot.py && .venv/bin/black testbench/rest_server.py tests/test_grpc_boot.py
git add testbench/rest_server.py tests/test_grpc_boot.py
git commit -m "feat(bootstrap): TESTBENCH_GRPC_PORT boot-start + TESTBENCH_BUCKETS seeding at the worker import anchor"
```

**Safety gate:** memory harness EMPTY diff; file harness B ≡ C (one entry) — boot block is a no-op with both vars unset; `/start_grpc` route behaviour is preserved (now delegating to `_start_grpc`). Boot-start proven to set the module globals (idempotent vs a later `/start_grpc`) and the empty-filter mutation-checked; end-to-end boot demonstrated. Definition/anchor order is fixed explicitly so the import-time call resolves all four names.

---

### Task 5: Single-worker assertion (file backend) — cross-worker lock + `--workers=1` belt

**Files:**
- Modify: `testbench/containment.py` (add `import atexit` to the module head; add `claim_worker_lock`)
- Modify: `testbench/filestore.py` (`_index_names` :61-62 — directory-only filter)
- Modify: `testbench/rest_server.py` (`_init_db_from_env` :34-47 file branch)
- Modify: `testbench_run.py` (gunicorn argv :57-67)
- Create: `tests/test_worker_lock.py` (lock claim/reclaim/refuse + the `cleared()`-with-lock-marker regression), `tests/test_run_argv.py`

**Interfaces:**
- Produces: `containment.claim_worker_lock(root) -> str` — `os.makedirs(root, exist_ok=True)`, then `os.open(<root>/.gcs-worker.lock, O_CREAT|O_EXCL|O_WRONLY)`; on success writes the pid and registers an `atexit` releaser that unlinks only if the file still holds this pid; on `FileExistsError`, reads the pid: an unreadable/empty/garbage marker is treated as **live and REFUSED** (fail-safe — an empty marker means a second worker raced the creator, exactly the misconfiguration we detect); a readable LIVE pid (`os.kill(pid, 0)`) raises `RuntimeError` loudly; a readable DEAD pid (`ProcessLookupError`) is reclaimed (unlink + retry). Returns the lock path. `_init_db_from_env` calls it in the file branch before `FileStore(root)`.
- Produces: `FileStore._index_names(self) -> set` now returns only **directory** entries of the root. It feeds `constrained_rmtree`'s index-membership allow-check in BOTH `cleared()` (:522-526) and `bucket_deleted` (:127). The `.gcs-worker.lock` marker is a sibling FILE, so without this filter `cleared()` would pass the marker to `constrained_rmtree`, whose non-directory guard (containment.py:152 `if os.path.islink(path) or not os.path.isdir(path): raise PermissionError`) would RAISE — breaking any file-backend reset. Filtering to directories mirrors `rebuild_index`'s existing isdir discipline (:389) and does not change the set for real bucket dirs, so B ≡ C is unmoved. `testbench_run.py` adds `--workers=1` to the gunicorn argv and honors the container opt-out (opt-out itself lands in Task 6; the belt lands here).

**Why a lock, not an env count:** gunicorn exposes no per-worker "worker count" to a sync worker, and the app-factory `testbench:run()` runs WITHOUT `--preload`, so each worker imports `testbench.rest_server` fresh and runs `_init_db_from_env` exactly once per worker (confirmed: `testbench/__init__.py:27-37` eagerly imports `rest_server`; its body runs `db = _init_db_from_env()` at :50). An `O_CREAT|O_EXCL` marker therefore genuinely fires: the first worker wins, a second fails loudly.

**Why the marker is safe (corrected — TWO `_index_names` consumers, not one):** the lock lives at `<root>/.gcs-worker.lock`, a sibling **file** of the bucket dirs. `rebuild_index` (:386-390) already skips it via its `isdir` guard. But `_index_names` (:61-62) does a bare `set(os.listdir(self._root))` with NO isdir filter, and it feeds `constrained_rmtree` in BOTH `bucket_deleted` and `cleared()` — the latter iterating every entry and calling `constrained_rmtree` on it, which RAISES on a non-directory (containment.py:152). So a naive marker WOULD break `cleared()` (and any future wire-exposed reset built on `Database.clear()` → `_store.cleared()`, database.py:100). This task fixes the property at the source: `_index_names` filters to directories only, and a regression test asserts `cleared()` succeeds with the marker present. (This is stronger than relying on the *unstated* fact that `Database.clear()` has no wire route today — the filter makes the safety real regardless of reachability.)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_worker_lock.py
import os
import tempfile
import unittest

from testbench import containment
from testbench.filestore import FileStore


class TestWorkerLock(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.lock = os.path.join(self.root, ".gcs-worker.lock")

    def test_first_claim_succeeds_and_writes_pid(self):
        path = containment.claim_worker_lock(self.root)
        self.assertEqual(self.lock, path)
        self.assertEqual(str(os.getpid()), open(self.lock).read().strip())

    def test_second_live_holder_fails_loudly(self):
        # A different, definitely-ALIVE pid holds the lock -> refuse.
        with open(self.lock, "w") as fh:
            fh.write(str(os.getpid()))     # our own pid: guaranteed alive
        with self.assertRaises(RuntimeError):
            containment.claim_worker_lock(self.root)

    def test_stale_lock_from_dead_pid_is_reclaimed(self):
        dead = self._a_dead_pid()
        with open(self.lock, "w") as fh:
            fh.write(str(dead))
        path = containment.claim_worker_lock(self.root)   # must reclaim, not raise
        self.assertEqual(str(os.getpid()), open(path).read().strip())

    def test_unreadable_marker_is_refused_fail_safe(self):
        # An empty/garbage marker cannot be proven stale -> fail safe, refuse.
        with open(self.lock, "w") as fh:
            fh.write("")                   # nascent-race sentinel
        with self.assertRaises(RuntimeError):
            containment.claim_worker_lock(self.root)

    def _a_dead_pid(self):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)                 # reap -> pid is now dead
        return pid


class TestClearedSkipsLockMarker(unittest.TestCase):
    # Regression: the sibling lock FILE must not reach constrained_rmtree via
    # cleared() -> _index_names (containment.constrained_rmtree raises on a
    # non-directory). _index_names must filter to directories only.
    def test_cleared_succeeds_with_lock_marker_present(self):
        root = tempfile.mkdtemp()
        os.mkdir(os.path.join(root, "abucket"))                 # a real bucket dir
        with open(os.path.join(root, ".gcs-worker.lock"), "w") as fh:
            fh.write(str(os.getpid()))                          # the sibling marker
        store = FileStore(root)
        store.cleared()                                         # must NOT raise
        self.assertFalse(os.path.exists(os.path.join(root, "abucket")))
        # The marker file itself is untouched by cleared() (not a bucket).
        self.assertTrue(os.path.exists(os.path.join(root, ".gcs-worker.lock")))
```

```python
# tests/test_run_argv.py
import subprocess
import sys
import unittest
from unittest import mock

import testbench_run


class TestRunArgv(unittest.TestCase):
    def test_gunicorn_argv_forces_single_worker(self):
        with mock.patch.object(subprocess, "run") as run, \
             mock.patch("platform.system", return_value="Linux"):
            sys.argv[:] = ["testbench_run.py", "127.0.0.1", "9000", "4"]
            testbench_run.start_server()
            argv = run.call_args[0][0]
            self.assertIn("--workers=1", argv)
```

- [ ] **Step 2: Run to see it fail** — `AttributeError: module 'testbench.containment' has no attribute 'claim_worker_lock'`; `TestClearedSkipsLockMarker` FAILS (`PermissionError: rmtree target ... is not a real directory`); `test_run_argv` FAILS (no `--workers=1`).

- [ ] **Step 3: Implement**

```python
# testbench/containment.py -- module head: ADD `import atexit` (NOT currently present;
# containment.py imports only os, shutil, pathing). isort orders it first:
import atexit
import os
import shutil

from testbench import pathing
```

```python
# testbench/containment.py -- add the lock primitives:
_WORKER_LOCK_NAME = ".gcs-worker.lock"


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True            # exists, owned by another user
    return True


def _release_worker_lock(path, owner_pid):
    try:
        with open(path) as fh:
            if fh.read().strip() == str(owner_pid):
                os.unlink(path)   # only remove OUR lock, never one reclaimed by another
    except OSError:
        pass


def claim_worker_lock(root):
    """N gunicorn workers over one TESTBENCH_ROOT means N divergent in-memory
    indexes -> silent corruption. The file backend claims an exclusive marker at
    startup; a second worker fails LOUDLY. O_CREAT|O_EXCL makes near-simultaneous
    forks safe (exactly one wins). A stale lock from a prior crash carries a
    readable pid -> reclaimed via PID-liveness. An UNREADABLE/empty marker cannot
    be proven stale (an empty read means a second worker raced the creator between
    O_CREAT|O_EXCL and the pid write -- exactly the misconfiguration we detect), so
    we FAIL SAFE and refuse rather than reclaim a possibly-live nascent lock. The
    marker is a sibling FILE of the bucket dirs, skipped by rebuild_index (isdir,
    :389) AND excluded from _index_names' directory-only filter (:61-62), so it
    never reaches constrained_rmtree via cleared()/bucket_deleted and never
    surfaces as a bucket."""
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, _WORKER_LOCK_NAME)
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                with open(path) as fh:
                    pid = int(fh.read().strip())
            except (OSError, ValueError):
                # Unreadable / empty / garbage -> cannot prove stale. Fail safe.
                raise RuntimeError(
                    "file backend single-worker lock at %s is unreadable/nascent; "
                    "refusing (a second worker likely raced the first)" % path
                )
            if _pid_alive(pid):
                raise RuntimeError(
                    "file backend requires a single gunicorn worker; "
                    "lock held by live pid %d at %s" % (pid, path)
                )
            try:
                os.unlink(path)          # readable dead pid -> stale -> reclaim
            except FileNotFoundError:
                pass
            continue
        else:
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            atexit.register(_release_worker_lock, path, os.getpid())
            return path
```

```python
# testbench/filestore.py -- _index_names (:61-62), directory-only filter:
    def _index_names(self):
        # Directory entries only -- bucket dirs. Skips sibling FILES (the
        # single-worker lock .gcs-worker.lock, transient .tmp-* files), matching
        # rebuild_index's isdir discipline (:389). constrained_rmtree raises on a
        # non-directory, so a non-dir sibling must never enter this set (consumed
        # by cleared() and bucket_deleted). Real bucket names are dirs, so the set
        # is unchanged for them and B==C is unmoved.
        return {
            name
            for name in os.listdir(self._root)
            if os.path.isdir(os.path.join(self._root, name))
        }
```

```python
# testbench/rest_server.py -- _init_db_from_env file branch (:40-46), claim BEFORE FileStore:
    if os.environ.get("TESTBENCH_STORE", "memory") == "file":
        root = os.environ.get("TESTBENCH_ROOT")
        if not root:
            raise RuntimeError("TESTBENCH_STORE=file requires TESTBENCH_ROOT")
        from testbench import containment
        from testbench.filestore import FileStore

        containment.claim_worker_lock(root)   # loud on a 2nd worker; earliest anchor
        return testbench.database.Database.init(store=FileStore(root))
    return testbench.database.Database.init()
```

```python
# testbench_run.py -- gunicorn argv (:57-67), add the belt:
            subprocess.run(
                [
                    "gunicorn",
                    f"--bind={sock_host}:{sock_port}",
                    "--workers=1",              # file backend: one index per root
                    "--worker-class=sync",
                    f"--threads={num_of_threads}",
                    "--reload",
                    "--access-logfile=-",
                    "testbench:run()",
                ]
            )
```

- [ ] **Step 4: Run to green + both harness legs**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_worker_lock.py tests/test_run_argv.py tests/test_store.py -q
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness              # memory EMPTY diff (no lock: store!=file)
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file # B==C: one worker, lock claimed once, released on teardown
```
The memory leg never claims a lock (`TESTBENCH_STORE` unset). The file leg's emulator runs one gunicorn worker, so the lock is claimed once; `_index_names`' directory filter keeps the marker out of the index-membership set, so B ≡ C is unmoved and the fresh per-run temp root (rmtree'd on teardown) removes the marker with it. The pytest file leg imports `rest_server` with `TESTBENCH_STORE` unset (conftest swaps the store on the singleton), so the pytest process claims no lock — the assertion fires only in a real file-backend gunicorn worker, exactly as intended.

- [ ] **Step 5: End-to-end: a second worker is refused**

```bash
TMP=$(mktemp -d)
TESTBENCH_STORE=file TESTBENCH_ROOT=$TMP WEB_CONCURRENCY=2 \
  PYTHONPATH=. .venv/bin/python -m gunicorn --bind=127.0.0.1:9000 --workers=2 \
  --worker-class=sync "testbench:run()" 2>&1 | grep -m1 -E "single gunicorn worker|unreadable/nascent"
```
Expected: at least one worker logs the loud `RuntimeError` — either "file backend requires a single gunicorn worker" (the `O_CREAT|O_EXCL` loser reading a live pid) or, if it races the creator before the pid write, "unreadable/nascent" (fail-safe refuse). Both are the correct loud failure. (This is the case a user bypassing `testbench_run.py`'s `--workers=1` belt with `--workers=2`/`WEB_CONCURRENCY` hits; the runtime lock is the load-bearing detector.)

- [ ] **Step 6: Mutation-check**
- In `claim_worker_lock`, drop `O_EXCL` (`O_CREAT|O_WRONLY`). Expected: `test_second_live_holder_fails_loudly` FAILS (the second claim opens the existing file instead of raising). Revert.
- In `claim_worker_lock`, remove the `_pid_alive(pid)` check (treat any readable existing lock as stale → always reclaim). Expected: `test_second_live_holder_fails_loudly` FAILS (a live holder is wrongly reclaimed). Revert.
- In `claim_worker_lock`, change the readable-dead-pid branch to raise instead of reclaim (treat any existing lock as live). Expected: `test_stale_lock_from_dead_pid_is_reclaimed` FAILS. Revert.
- In `_index_names`, drop the `if os.path.isdir(...)` filter (bare `set(os.listdir(...))`). Expected: `TestClearedSkipsLockMarker.test_cleared_succeeds_with_lock_marker_present` FAILS (`constrained_rmtree` raises on the marker). Revert.
- In `testbench_run.py`, remove `"--workers=1"`. Expected: `test_gunicorn_argv_forces_single_worker` FAILS. Revert.
- **Carve-out (documented defense-in-depth):** the fail-safe `RuntimeError` on an unreadable/nascent marker is exercised directly by `test_unreadable_marker_is_refused_fail_safe`. Its *concurrency* trigger (a live second worker racing the creator's pid write) is unreachable under `--workers=1` and is subsumed by the live-pid refuse a microsecond later; it is kept as an honest fail-safe backstop, not a vacuous guard, and the direct unit test keeps it individually killable (delete the `raise` → substitute `pid = -1; continue` → the test FAILS).

- [ ] **Step 7: 3.8 parse, format, commit**

```bash
PYTHONPATH=. .venv/bin/python -c "import ast; [ast.parse(open(f).read(), feature_version=(3,8)) for f in ('testbench/containment.py','testbench/filestore.py','testbench/rest_server.py','testbench_run.py')]"
.venv/bin/isort testbench/containment.py testbench/filestore.py testbench/rest_server.py testbench_run.py tests/test_worker_lock.py tests/test_run_argv.py && .venv/bin/black testbench/containment.py testbench/filestore.py testbench/rest_server.py testbench_run.py tests/test_worker_lock.py tests/test_run_argv.py
git add testbench/containment.py testbench/filestore.py testbench/rest_server.py testbench_run.py tests/test_worker_lock.py tests/test_run_argv.py
git commit -m "feat(bootstrap): single-worker lock for the file backend + --workers=1 gunicorn belt"
```

**Safety gate:** memory harness EMPTY diff (no lock when `TESTBENCH_STORE != file`); file harness B ≡ C (one entry) — lock claimed once by the single emulator worker; `_index_names`' directory-only filter keeps the marker out of `constrained_rmtree` (regression-tested via `cleared()`), and the set is unchanged for real bucket dirs so B ≡ C is unmoved. The lock is the load-bearing guard (`O_EXCL`, PID-liveness refuse, and dead-pid reclaim all mutation-checked); the `_index_names` filter and the `--workers=1` belt are mutation-checked; a second worker is proven to be refused loudly end-to-end.

---

### Task 6: Generalize the loopback bind for containers (`TESTBENCH_ALLOW_NONLOOPBACK`)

**Files:**
- Modify: `testbench/grpc_server.py` (`_bind_host` :1388-1390)
- Modify: `testbench_run.py` (loopback refusal :37-44)
- Create: `tests/test_bind_host.py`

**Interfaces:**
- Produces: `grpc_server._bind_host()` returns the requested `0.0.0.0` when `TESTBENCH_ALLOW_NONLOOPBACK == "1"`, else today's value (`127.0.0.1` under the file backend, `0.0.0.0` otherwise). `testbench_run.start_server`'s refusal skips when the opt-out is set. Default-off keeps today's file-backend loopback safety exactly.

**Decision (recorded — 7th env var, explicitly acknowledged):** the spec's "Configuration and deployment" table enumerates SIX vars (`STORE`, `ROOT`, `BUCKETS`, `GRPC_PORT`, `GRPC_THREADS`, `FSYNC`). `TESTBENCH_ALLOW_NONLOOPBACK` is a **seventh, new public env var** — this is a deliberate expansion of the config surface beyond the spec table, made because the spec's own compose is otherwise unreachable: a container process bound to `127.0.0.1` is invisible through docker's published ports (port mapping requires binding `0.0.0.0` in the container's netns). The `_bind_host` loopback (grpc_server.py:1390) and the `testbench_run.py` refusal (:37-44) — correct hardening for a bare-metal file backend — make the spec's compose impossible as written. This is the minimal generalization the spec's "phase 6 only generalizes the wiring" clause anticipates: ONE opt-out env, honored by BOTH bind sites. Compose sets it AND publishes to **host loopback** (`127.0.0.1:9000:9000`), so the service is reachable via published ports but never on the host's external interfaces — preserving spec Security rule 6 ("do not publish beyond the compose network"). Default-off means the harness/emulator file leg (which does not set it) still binds `127.0.0.1`, so B ≡ C is unmoved. **This 7th var is recorded against the spec's env-var table in the handoff (Task 7) for the spec owner to bless as the sanctioned generalization.**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bind_host.py
import os
import subprocess
import sys
import unittest
from unittest import mock

import testbench_run
from testbench import grpc_server


class TestBindHost(unittest.TestCase):
    def setUp(self):
        self._store = os.environ.pop("TESTBENCH_STORE", None)
        self._allow = os.environ.pop("TESTBENCH_ALLOW_NONLOOPBACK", None)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in (("TESTBENCH_STORE", self._store),
                     ("TESTBENCH_ALLOW_NONLOOPBACK", self._allow)):
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_file_backend_binds_loopback_by_default(self):
        os.environ["TESTBENCH_STORE"] = "file"
        self.assertEqual("127.0.0.1", grpc_server._bind_host())

    def test_memory_backend_binds_all_interfaces(self):
        self.assertEqual("0.0.0.0", grpc_server._bind_host())

    def test_opt_out_allows_all_interfaces_under_file_backend(self):
        os.environ["TESTBENCH_STORE"] = "file"
        os.environ["TESTBENCH_ALLOW_NONLOOPBACK"] = "1"
        self.assertEqual("0.0.0.0", grpc_server._bind_host())

    def test_run_refuses_nonloopback_file_bind_without_optout(self):
        os.environ["TESTBENCH_STORE"] = "file"
        sys.argv[:] = ["testbench_run.py", "0.0.0.0", "9000", "4"]
        with self.assertRaises(SystemExit):
            testbench_run.start_server()

    def test_run_allows_nonloopback_file_bind_with_optout(self):
        os.environ["TESTBENCH_STORE"] = "file"
        os.environ["TESTBENCH_ALLOW_NONLOOPBACK"] = "1"
        with mock.patch.object(subprocess, "run") as run, \
             mock.patch("platform.system", return_value="Linux"):
            sys.argv[:] = ["testbench_run.py", "0.0.0.0", "9000", "4"]
            testbench_run.start_server()          # must NOT SystemExit
            self.assertTrue(run.called)
```

- [ ] **Step 2: Run to see it fail** — `test_opt_out_*` and `test_run_allows_*` FAIL (opt-out not honored yet).

- [ ] **Step 3: Implement**

```python
# testbench/grpc_server.py -- _bind_host (:1388-1390):
def _bind_host():
    # The traversal-capable file backend binds loopback so it is never network
    # exposed -- UNLESS TESTBENCH_ALLOW_NONLOOPBACK=1 (the container case, where
    # published ports require binding 0.0.0.0 in the container netns; compose then
    # publishes to host loopback so it stays off external interfaces).
    if os.environ.get("TESTBENCH_ALLOW_NONLOOPBACK") == "1":
        return "0.0.0.0"
    return "127.0.0.1" if os.environ.get("TESTBENCH_STORE") == "file" else "0.0.0.0"
```

```python
# testbench_run.py -- loopback refusal (:37-44):
        if (
            os.environ.get("TESTBENCH_STORE") == "file"
            and os.environ.get("TESTBENCH_ALLOW_NONLOOPBACK") != "1"
            and sock_host not in ("127.0.0.1", "localhost", "::1")
        ):
            raise SystemExit(
                "file backend refuses non-loopback bind host %r "
                "(set TESTBENCH_ALLOW_NONLOOPBACK=1 for the container case)" % sock_host
            )
```

- [ ] **Step 4: Run to green + both harness legs (opt-out unset)**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_bind_host.py -q
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness              # memory EMPTY diff
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file # B==C: file leg still binds 127.0.0.1
```
The emulator's file leg does not set `TESTBENCH_ALLOW_NONLOOPBACK`, so `_bind_host` still returns `127.0.0.1` and the harness is unmoved.

- [ ] **Step 5: Mutation-check**
- In `_bind_host`, delete the opt-out clause. Expected: `test_opt_out_allows_all_interfaces_under_file_backend` FAILS. Revert.
- In `testbench_run.py`, delete the `and os.environ.get("TESTBENCH_ALLOW_NONLOOPBACK") != "1"` clause. Expected: `test_run_allows_nonloopback_file_bind_with_optout` FAILS (it wrongly SystemExits). Revert. Also confirm `test_run_refuses_nonloopback_file_bind_without_optout` still passes (the refusal is intact when the opt-out is absent — this is the load-bearing security clause; deleting the *host-not-loopback* check would fail it).

- [ ] **Step 6: 3.8 parse, format, commit**

```bash
PYTHONPATH=. .venv/bin/python -c "import ast; [ast.parse(open(f).read(), feature_version=(3,8)) for f in ('testbench/grpc_server.py','testbench_run.py')]"
.venv/bin/isort testbench/grpc_server.py testbench_run.py tests/test_bind_host.py && .venv/bin/black testbench/grpc_server.py testbench_run.py tests/test_bind_host.py
git add testbench/grpc_server.py testbench_run.py tests/test_bind_host.py
git commit -m "feat(bootstrap): TESTBENCH_ALLOW_NONLOOPBACK opt-out for container port-mapping (default loopback)"
```

**Safety gate:** memory harness EMPTY diff; file harness B ≡ C (one entry) — opt-out default-off, so both bind sites are byte-identical to today. Both the gRPC bind and the REST refusal honor the opt-out; the refusal's security clause and the opt-out are each mutation-checked. The 7th env var is flagged for the handoff's spec-table record.

---

### Task 7: docker compose (two services from one image) + Dockerfile coupling note + phase-6 exit

**Files:**
- Create: `docker-compose.yml`
- Modify: `Dockerfile` (comment only :23-27; no CMD change needed)
- Modify: `.github/workflows/build.yaml` (add a compose-config validation step) — optional if a Docker daemon is unavailable in CI; otherwise a documented manual gate
- Update the handoff.

**Interfaces:**
- Produces: `docker-compose.yml` with `gcs-dev` (persistent named volume) and `gcs-test` (ephemeral tmpfs), both `build: .`, differing only by env; top-level `volumes: gcs-data:`. Both set `TESTBENCH_ALLOW_NONLOOPBACK=1` (containers must bind `0.0.0.0`) and publish to host loopback. The image CMD (`testbench_run.py 0.0.0.0 9000 10`, Dockerfile:23-27) now works under the file backend because the opt-out is set and `--workers=1` flows through `testbench_run.py`.

- [ ] **Step 1: Write `docker-compose.yml`**

```yaml
# docker-compose.yml -- two services, one image, same code path, env-only difference.
services:
  gcs-dev:                              # requirement 3: persistent, file-backed
    build: .
    environment:
      TESTBENCH_STORE: file
      TESTBENCH_ROOT: /data
      TESTBENCH_BUCKETS: audio,transcripts,models
      TESTBENCH_GRPC_PORT: "9001"
      TESTBENCH_ALLOW_NONLOOPBACK: "1"  # container must bind 0.0.0.0 for published ports
    volumes:
      - gcs-data:/data                  # named volume (case-sensitive, fast), NOT a bind mount
    ports:
      - "127.0.0.1:9000:9000"           # published to HOST LOOPBACK only (spec Security rule 6)
      - "127.0.0.1:9001:9001"

  gcs-test:                             # requirement 2: ephemeral
    build: .
    environment:
      TESTBENCH_STORE: file
      TESTBENCH_ROOT: /data
      TESTBENCH_BUCKETS: ""             # tests create their own UUID buckets
      TESTBENCH_GRPC_PORT: "9001"
      TESTBENCH_ALLOW_NONLOOPBACK: "1"
    tmpfs:
      - /data                           # RAM-backed, container-lifetime backstop
    ports:
      - "127.0.0.1:9010:9000"           # distinct host ports so both can run at once
      - "127.0.0.1:9011:9001"

volumes:
  gcs-data:
```

(`TESTBENCH_GRPC_PORT`/`TESTBENCH_BUCKETS` are quoted so compose passes them as strings; `int(port)` in `_start_grpc` handles the numeric coercion. `gcs-test`'s empty `TESTBENCH_BUCKETS` exercises the empty-filter from Task 4. Host ports differ so `docker compose up` can run both.)

- [ ] **Step 2: Document the Dockerfile coupling (comment only)**

```dockerfile
# Dockerfile -- above the CMD (:23):
# The 0.0.0.0 bind is honored under the file backend ONLY when the compose service
# sets TESTBENCH_ALLOW_NONLOOPBACK=1 (see docker-compose.yml); testbench_run.py adds
# --workers=1 so one process owns one TESTBENCH_ROOT index. gRPC boots at import via
# TESTBENCH_GRPC_PORT -- no post-start /start_grpc curl needed.
CMD ["python3", \
      "testbench_run.py", \
      "0.0.0.0", \
      "9000", \
      "10"]
```

- [ ] **Step 3: Validate the compose file**

```bash
docker compose -f docker-compose.yml config >/dev/null && echo "compose OK"   # syntax + schema render
```
Expected: `compose OK`. If no Docker daemon is available locally, run this in the dev shell (`nix develop`) which provides `docker-compose`; record the result in the handoff.

- [ ] **Step 4: Manual compose smoke (record in handoff)**

```bash
docker compose up -d --build gcs-dev
sleep 5
curl -s http://127.0.0.1:9000/storage/v1/b?project=test-project | python -m json.tool | grep '"name"'  # expect audio, transcripts, models
docker compose exec gcs-dev ls /data                                                                    # expect the three bucket dirs + .gcs-worker.lock
python - <<'PY'
import grpc; grpc.insecure_channel("127.0.0.1:9001").subscribe(lambda c: None); print("grpc up at boot")
PY
docker compose down -v   # -v drops the named volume
```
Expected: the three seeded buckets are listed and browsable via `exec` (the `.gcs-worker.lock` marker is a sibling file, correctly not a bucket); gRPC is up at boot; a restart of `gcs-dev` (without `-v`) re-seeds idempotently (buckets already on the volume are skipped, no `already_exists`). Record the transcript in the handoff.

- [ ] **Step 5: Run the full phase-6 gate**

```bash
TESTBENCH_TEST_STORE=memory PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
TESTBENCH_TEST_STORE=file   PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness              # memory: OK, EMPTY diff, digest 98fa2130…
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file # file: OK, one allow-list entry
nix develop --command make verify-linux                                 # both legs
```

- [ ] **Step 6: Confirm invariants, commit, push, update handoff**

```bash
grep -rnE "import (gcs|testbench)" tests/conformance/ | grep -v emulator.py   # expect empty
git diff --name-only main..HEAD -- setup.py                                    # expect empty (zero new runtime deps)
git add docker-compose.yml Dockerfile .github/workflows/build.yaml
git commit -m "feat(bootstrap): docker compose (gcs-dev named volume + gcs-test tmpfs) from one image"
git push origin file-backend-design
gh run list --branch file-backend-design --limit 3
```
Update the handoff: phase 6 landed; memory digest unchanged (`98fa2130…`); allow-list still exactly one entry (`create-bucket-traversal`); the six spec-table env vars are wired (`TESTBENCH_STORE`/`ROOT` from phase 4, plus `BUCKETS`/`GRPC_PORT`/`GRPC_THREADS`/`FSYNC`); **a SEVENTH var, `TESTBENCH_ALLOW_NONLOOPBACK`, was added beyond the spec table as the sanctioned "generalize the wiring" opt-out (default loopback) — record it against the spec's "Configuration and deployment" table for the spec owner to bless**; the file backend fails loudly on a second worker (live-pid or nascent-race), and `_index_names` now filters to directories so the lock marker never trips `cleared()`; compose ships two services from one image; the rewrite-abort staging sweep remains a phase-7 follow-up (noted at filestore.py `delete_rewrite`).

**Safety gate (phase-6 exit):** suite green on both `TESTBENCH_TEST_STORE` legs; memory harness byte-identical (digest `98fa2130…`); file harness byte-identical except the one allow-list entry; `docker compose config` validates and the manual compose smoke (buckets seeded, gRPC at boot, single worker) is recorded; `make verify-linux` green on both legs; `setup.py` unchanged; nothing in `tests/conformance/` imports internals except `emulator.py`.

---

## Self-Review

**Spec coverage (phase-6 row of the per-phase gate + "Configuration and deployment"):**
- Env vars wired at the single worker import anchor (`rest_server.py:50`): `TESTBENCH_GRPC_THREADS` (Task 1), `TESTBENCH_FSYNC` (Task 2), `TESTBENCH_BUCKETS` (Tasks 3+4), `TESTBENCH_GRPC_PORT` (Task 4); `TESTBENCH_STORE`/`TESTBENCH_ROOT` already done in phase 4. ✅
- `TESTBENCH_GRPC_THREADS` default 32 replaces the hardcoded `_GRPC_SERVER_THREAD_COUNT = 2` (grpc_server.py:46→`_grpc_thread_count()` at :1395); goldens proven unmoved on BOTH backends after the change, not assumed. ✅
- `TESTBENCH_BUCKETS` idempotent startup seeding via `Database.seed_buckets`, superseding but not removing the `GOOGLE_CLOUD_CPP_STORAGE_TEST_BUCKET_NAME` auto-create (`insert_test_bucket` intact); empty-string and unset both seed nothing (empty-filter); idempotent against a persistent named volume on restart. ✅
- `TESTBENCH_GRPC_PORT` boot-start reuses the exact `grpc_server.run(port, db, ...)` the `/start_grpc` route uses, via the shared `_start_grpc` helper that sets the MODULE globals so a later `/start_grpc` is a no-op (the double-start hazard is mutation-checked); definition/anchor order is fixed explicitly. ✅
- `TESTBENCH_FSYNC` opt-in fsync of metadata (`write_bytes_atomic`) and media (`FileMedia.finalize`/`seal`) writes, OFF by default with zero extra syscalls (single load-bearing gate, mutation-checked on both paths), so B ≡ C and the memory digest are unaffected (fsync changes durability, not bytes). ✅
- Single-worker assertion for `TESTBENCH_STORE=file`: a cross-worker `O_CREAT|O_EXCL` lock with PID-liveness reclaim and fail-safe refuse on a nascent/unreadable marker (the load-bearing detector, since gunicorn exposes no worker count and the app-factory runs without `--preload`), plus the `--workers=1` gunicorn belt; a second worker is refused loudly end-to-end. ✅
- docker compose: two services from one image (`gcs-dev` persistent named volume + `gcs-test` ephemeral tmpfs), same code path, env-only difference; the loopback bind generalized just enough (`TESTBENCH_ALLOW_NONLOOPBACK`, a 7th var explicitly acknowledged and flagged for spec-owner blessing) for container port-mapping while publishing to host loopback (spec Security rule 6 preserved). ✅
- Every env var UNSET ⇒ byte-identical to today: every hook is `if os.environ.get(VAR)`-gated, the lock/seed/boot block is a no-op with the vars unset, `_bind_host`/the refusal default to today's values, and `_index_names`' directory filter is a no-op for real bucket dirs. The memory backend never imports `containment`, so `TESTBENCH_FSYNC` cannot reach it. ✅

**Deliberate design decisions (recorded in-plan):**
1. `TESTBENCH_FSYNC` is a single process-global in `containment` (the write choke point), NOT a `FileStore(root, fsync=)` param — fsync is a deployment concern, `FileStore` never fsyncs directly, and a param would risk moving the `FileStore(root)` call sites and add dead surface. `FileStore(root)` stays byte-identical (Task 2 decision block). The metadata fsync calls are UNCONDITIONAL with a single internal guard in `maybe_fsync`, so the guard is genuinely mutation-killable (no double-guard survivor) — Task 2 single-gate decision.
2. `seed_buckets` uses plain `Bucket.init` defaults, not `insert_test_bucket`'s `metageneration=4`/`versioning=True` (those are the cpp suite's expectations); per-bucket JSON overrides are out of scope (nice-to-have, dev-only) to avoid a new parsing surface (Task 3 decision block).
3. The single-worker guard is a runtime lock, not an env-count read — gunicorn gives a sync worker no worker count, and no-`--preload` guarantees a fresh per-worker import, so `O_EXCL` genuinely fires. It lives in `_init_db_from_env`'s file branch (earliest point the root is known) and NOT in `FileStore.__init__`, so in-process pytest never trips it. An unreadable/nascent marker fails safe (refuse) rather than reclaiming a possibly-live lock (Task 5).
4. `TESTBENCH_ALLOW_NONLOOPBACK` is the minimal generalization the spec's "phase 6 only generalizes the wiring" clause anticipates; without it the spec's own compose is unreachable. It is a **7th public env var beyond the spec's 6-var table** — explicitly acknowledged, default-off (so goldens/B≡C are unaffected), and recorded against the spec table in the handoff for the spec owner to bless. Compose publishes to host loopback so the service stays off external interfaces (Task 6 decision block).
5. The worker lock lives at `<root>/.gcs-worker.lock`, a sibling FILE of the bucket dirs. `rebuild_index` already skips it (isdir, :389), BUT `_index_names` (:61-62) had NO isdir filter and feeds `constrained_rmtree` in BOTH `bucket_deleted` and `cleared()` — the latter would raise on the marker (containment.py:152). Task 5 fixes the property at the source: `_index_names` filters to directories only (matching rebuild_index's discipline), regression-tested via `cleared()` with the marker present. This is stronger than relying on the unstated fact that `Database.clear()` has no wire route today; the marker never reaches `constrained_rmtree` and never surfaces as a bucket regardless of reachability (Task 5 decision block).
6. gRPC boot-at-import and the single-worker lock both depend on the app-factory running without `--preload` (confirmed: `testbench/__init__.py:27-37` → `rest_server.py:50`); this coupling is documented so a future `--preload` change is flagged.

**Sequencing:** each task lands with the memory harness EMPTY diff (digest `98fa2130…`) AND the file harness B ≡ C (one entry), because every new behaviour is env-gated OFF by default (or, for `_index_names`, a no-op for real bucket dirs). Task 1 is first (the only golden-risk change, 2→32) and is explicitly re-verified; Tasks 2–6 are no-ops when their vars are unset; Task 7 (compose) depends on the boot-start (Task 4), seeding (Task 3), and bind generalization (Task 6).

**Placeholder scan:** no `TBD`/`TODO`/"handle edge cases"; every code step shows real code and every run step a real command with an expected result. The gRPC executor introspection (`server._state.thread_pool._max_workers`), the fsync syscall counter, the `os.fork`-based dead-pid fixture, the nascent-marker fail-safe test, the `cleared()`-with-marker regression, the argv-capture `mock.patch`, and the compose smoke are all concrete.

**Type consistency:** `grpc_server._grpc_thread_count() -> int`; `grpc_server.run(port, database, echo_metadata=False) -> (int, grpc.Server)` unchanged; `containment.FSYNC: bool`, `containment.maybe_fsync(fd) -> None`, `containment.claim_worker_lock(root) -> str`; `FileStore._index_names(self) -> set[str]`; `Database.seed_buckets(names: Iterable[str]) -> None`; `rest_server._start_grpc(port, echo_metadata=False) -> None` (sets globals), `rest_server._bootstrap_from_env() -> None`; `grpc_server._bind_host() -> str`. `FakeRequest(args={}, data=json.dumps({"name": name}))` matches `insert_test_bucket:246-248`; `Bucket.init(request, None) -> (bucket, projection)` matches `insert_test_bucket:249`.

**Known risk carried into execution:** `server._state.thread_pool._max_workers` is a grpcio private attribute (pinned `==1.70.0` in setup.py:50); if a version bump moves it, Task 1 Step 1's `test_run_sizes_executor_from_env` must re-pin the observable (or pass an executor via a seam) — flagged with an in-test comment. The compose gate needs a Docker daemon; where CI lacks one it is a documented manual gate (Task 7 Steps 3-4), with `docker compose config` as the syntax check when the daemon is present. `atexit` release does not run on `SIGKILL`; a persistent-volume stale lock is handled by PID-liveness reclaim (readable dead pid) or fail-safe refuse (unreadable/nascent), and tmpfs wipes on container restart — all covered. The nascent-marker TOCTOU window is unreachable under `--workers=1` and is handled fail-safe (refuse) rather than by racing a reclaim.
