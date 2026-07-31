# Handoff: Phase 5 (FileMedia) complete & CI-green — phase 6 (bootstrap) next

## Objective
`storage-testbench` is a fake Google Cloud Storage server for testing GCS clients.
The multi-phase goal is a **persistent, opt-in file backend** so object bytes and
metadata can live on disk, with **zero change to externally observable behavior**
on the default (memory) backend. Two seams: `Store` (metadata, phase 2/4) and
`Media` (bytes, phase 3/5). Everything is proven by a black-box conformance
harness (`tests/conformance/`) that must show an empty diff for the memory
backend and byte-identical B≡C behavior for the file backend.

## Status by phase (branch `file-backend-design`)
- ✅ **Phase 1** flake + conformance harness (golden master, "configuration A").
- ✅ **Phase 2** `Store` seam + `NullStore` (`testbench/store.py`).
- ✅ **Phase 3** `Media` seam + `BytesMedia` (`testbench/media.py`).
- ✅ **Phase 4 FileStore** — DONE, CI-green at `5924408`. See "Phase 4 detail".
- ✅ **Phase 5 FileMedia** — DONE, **CI-green at commit `aea1cc5`** (Lint ✓,
  Docker ✓, Unit Tests ✓ on Python 3.8–3.12 + Windows 3.11, Conformance ✓ on
  BOTH the memory and `--store file` B≡C legs). See "Phase 5 detail" below.
- ⏳ **Phase 6 Bootstrap** — plan being synthesized
  (`docs/superpowers/plans/2026-07-31-file-backend-bootstrap.md`). Env vars
  (TESTBENCH_BUCKETS/GRPC_PORT/GRPC_THREADS/FSYNC), single-worker assert, compose files.
- ⬜ **Phase 7 Verification** — durability/crash, concurrency, 4GB bounded-memory,
  optional real-GCS divergence report.

## Phase 4 detail (what landed, commits `2a55caa..5924408`)
New modules (stdlib-only + existing crc32c/protobuf; zero new runtime deps):
- `testbench/pathing.py` — pure name→path policy: `validate_bucket_name` (the
  file backend's SOLE bucket check; `_BUCKET_RE`+`goog` are load-bearing, the
  explicit `..`/slash/NUL/length clauses are documented defense-in-depth
  equivalent-mutants), reversible `escape`/`unescape`, `classify`→natural|overflow
  (`overflow_token` = sha256 hex, no caller bytes), `is_contained`.
- `testbench/containment.py` — fd-based `openat`/`O_NOFOLLOW` primitives +
  `realpath` `constrained_rmtree` (defense-in-depth backstop). POSIX-only.
- `testbench/sidecar.py` — versioned `MessageToJson` envelope, fd-based atomic write.
- `testbench/filestore.py` — `FileStore(Store)`: all 12 notifications mapped to
  CONTAINED fs ops over the `.gcs/` layout; write-time collision guard;
  `rebuild_index` startup tree-scan + inode-based (`st_dev`/`st_ino`) collision
  detection. Phase-4 persists SMALL object bytes via `blob.media.to_bytes()` +
  `write_bytes_atomic` (bytes are still `BytesMedia`; streaming is phase 5).
Wiring & gates:
- `Database.do_update_bucket`/`bucket_updated` route ALL bucket-metadata mutations
  (REST PUT/PATCH/ACL/DOACL/IAM/lockRetention + gRPC Update/Lock/SetIam) — zero-diff.
- `restore_object` fires `object_purged` for the original generation (single
  reconciliation mechanism; no `generation-1` cleanup in `object_inserted`).
- Env-driven store selection (`TESTBENCH_STORE=file`, `TESTBENCH_ROOT`); the file
  backend forces **loopback bind** (gRPC 127.0.0.1, REST refuses non-loopback).
- `conftest.py` = Mechanism-1 both-backend switch (`TESTBENCH_TEST_STORE=memory|file`),
  swapping the store on the live `rest_server.db` singleton (does NOT override
  `Database.init` — would break `test_default_store_is_a_null_store`). Windows:
  `collect_ignore` excludes the 6 POSIX-only file-backend test modules.
- B≡C harness: `tests/conformance/harness.py --store {memory,file}` + byte-exact
  masked overlay; `tests/conformance/allowlist.json` holds **exactly one** entry,
  `create-bucket-traversal` (file rejects `../../etc/passwd`, memory accepts —
  `gcs/bucket.py`'s validator is DELIBERATELY left unfixed to preserve the memory
  golden digest). `stale_allowlist_labels` is scoped to labels present in a trace.
- CI: `python-tests` runs a `TESTBENCH_TEST_STORE=file` leg; `conformance` runs
  `harness --store file`. `hypothesis<6.113` added to flake.nix + CI (Linux only).

## Phase 5 detail (what landed, commits `a9be352..aea1cc5`)
- `testbench/filemedia.py` — `FileMedia(Media)`: pread reads on an `O_RDONLY|O_NOFOLLOW`
  fd, `O_APPEND` staging writes with incremental rolling crc32c/md5, `chunks()`/`reader()`
  byte-identical to `BytesMedia` (zero-length special-cased), explicit fd ownership
  (`close()` idempotent + `__del__`), `is_finalized`, `finalize`/`link_into`/`seal`
  promotion, `from_existing` hydration, and materialising compat shims (never on the
  streaming path).
- Backend selection: a media factory on `Store` (`new_upload_media`/`new_staging_media`) —
  `NullStore`→`BytesMedia` (memory byte-identical), `FileStore`→`FileMedia` staged under
  `.gcs/uploads/` via the `O_NOFOLLOW` walk. `gcs/object.py` construction guards widened
  from `isinstance(BytesMedia)` to `isinstance(Media)`. `gcs/` stays store-agnostic.
- Every `.to_bytes()`/`gzip.decompress`/whole-buffer escape hatch migrated to streaming:
  uploads (resumable/gRPC/bidi), REST download (arithmetic clamped `Content-Length`,
  `end - max(0, begin)`), gzip transcode (two-pass counted `Content-Length`), compose,
  rewrite/move/copy, `FileStore.object_inserted` (finalize/link_into vs BytesMedia fallback),
  hydration. Surviving materialisers are fault-injection-only (documented) + the BytesMedia
  fallback. `testbench/containment.py` gained `promote`/`hardlink`/`unlink_at`/`open_staging`.
- Appendable F2 (trace-UNCOVERED, dedicated test): `object_inserted` `link_into`s the staging
  inode once (append fd stays open); intermediate `object_updated` checkpoints are
  sidecar-only; `seal()` runs exactly once at the finalize checkpoint (`blob.upload is None`).
- Mechanism 5: `tests/test_filemedia_bounds.py` — in-process 4GB (env-gated `TESTBENCH_BOUNDS_4GB=1`)
  peak-RSS < baseline+256 MiB + linear-time `t(2N)/t(N)<3`. Fault-path + parity + staging suites added.

## Key invariants (do NOT break)
- **Memory golden digest = `98fa2130d213b04478474c5918a6ba36e3e52838823189f4093a9161f72987a7`**
  (sha256 of `golden/{rest,grpc,faults}.json` concatenated). Task 2 changed it once
  from the phase-3 value `8eda6110…` by ADDITIVELY pinning `create-bucket-traversal`.
- **File harness must diverge on ONLY `create-bucket-traversal`.** Allow-list stays
  at one entry. NEVER `--regenerate` to turn a red gate green.
- Zero new RUNTIME deps (`setup.py` unchanged); Python floor 3.8; isort THEN black.
- Nothing in `tests/conformance/` imports `gcs`/`testbench` except `emulator.py`.
- The file backend is **POSIX-only / Windows-out-of-scope**.
- Mutation-check every guard clause (documented equivalent-mutant carve-out exists
  for genuinely-subsumed defense-in-depth clauses).

## Phase 6 (bootstrap) — what's next
Wire the deployment/config surface (spec "Configuration and deployment"):
`TESTBENCH_BUCKETS` (idempotent startup bucket seeding, superseding the single
`GOOGLE_CLOUD_CPP_STORAGE_TEST_BUCKET_NAME` auto-create), `TESTBENCH_GRPC_PORT`
(boot-start gRPC), `TESTBENCH_GRPC_THREADS` (default 32, replaces the hardcoded
`_GRPC_SERVER_THREAD_COUNT = 2`), `TESTBENCH_FSYNC` (opt-in fsync, OFF by default),
the single-worker startup assertion for `TESTBENCH_STORE=file`, and docker-compose
files (gcs-dev named volume + gcs-test tmpfs). **Every new env var UNSET must be
byte-identical to today** (memory digest `98fa2130…`, file B≡C one entry). Loopback
bind already landed in phase 4. Plan: `docs/superpowers/plans/2026-07-31-file-backend-bootstrap.md`.
Phase 7 after that: durability/crash, concurrency, 4GB bounded-memory, optional real-GCS report.

## Environment / toolchain
Nothing is on the bare PATH. Use the venv directly (`nix develop` provisions it):
- `PYTHONPATH=. .venv/bin/python -m pytest …`
- `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store {memory,file}`
- `.venv/bin/isort --quiet <f> && .venv/bin/black --quiet <f>` (isort then black)
- `hypothesis<6.113` is installed in `.venv`.

## How to verify (phases 4-5)
```bash
git log --oneline 2a55caa..aea1cc5          # phase 4+5 commits (HEAD = aea1cc5)
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store memory   # OK; digest 98fa2130…
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file     # OK; diverges only on create-bucket-traversal
TESTBENCH_TEST_STORE=memory PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
TESTBENCH_TEST_STORE=file   PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
gh run list --branch file-backend-design --limit 3        # Unit Test / Docker / Lint all success at aea1cc5
```
Local note: 3 `tests/test_testbench_startup.py` failures are a pre-existing env
gap (they Popen the system `python3`, which lacks `waitress`); CI's venv has it.
Skip `tests/test_testbench_continue_after_fault_injection.py` locally (macOS hang);
never add that ignore to CI.

## Known limitations
- **Abandoned multi-call rewrite leaks staging.** `Database.delete_rewrite` /
  `FileStore.delete_rewrite` exist and are unit-tested, but have NO server-invoked
  caller (GCS has no CancelRewrite RPC; the testbench has no rewrite-expiry sweep).
  An abandoned rewrite leaks its in-memory `_rewrites` entry on BOTH backends
  (pre-existing) and, on the file backend, its `.gcs/uploads/<token>` staging + fd.
  Completed rewrites do NOT leak (finalize consumes staging). Wiring a rewrite-lifecycle
  sweep (which would also GC `_rewrites`) is a phase-6/7 follow-up; a sweep must not
  add memory-backend-absent cleanup that breaks B≡C.

## Blockers / decisions reserved for the human
- **PR #1 is a draft; merging to `main` is the maintainer's call — not an agent
  decision.** All phase work lands on `file-backend-design` and is taken to
  CI-green there; the merge is explicitly out of scope without instruction.
