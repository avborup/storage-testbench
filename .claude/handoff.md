# Handoff: Phase 4 (FileStore) complete & CI-green — phase 5 (FileMedia) next

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
- ✅ **Phase 4 FileStore** — DONE, **CI-green at commit `5924408`** (Lint ✓,
  Docker ✓, Unit Tests ✓ on Python 3.8–3.12 + Windows 3.11, Conformance ✓ on
  BOTH the memory and `--store file` B≡C legs). See "Phase 4 detail" below.
- ⏳ **Phase 5 FileMedia** — plan being synthesized (see `docs/superpowers/plans/`
  once written: `2026-07-30-file-backend-filemedia.md`). NOT started in code.
- ⬜ **Phase 6 Bootstrap** — env vars, single-worker assert, compose files.
- ⬜ **Phase 7 Verification** — durability/crash, concurrency, 4GB bounded-memory.

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

## Phase 5 (FileMedia) — what's next
Back the `Media` interface with a real file: mmap reads, `O_APPEND` staging
uploads, incremental rolling crc32c/md5, streaming compose/rewrite/gzip, and
`finalize(dest)`=`os.replace(staging, dest)` into the FileStore-owned path.
Replace EVERY `.to_bytes()`/`gzip.decompress`/whole-buffer escape hatch with true
streaming (`gcs/object.py`, `gcs/upload.py`, `gcs/rewrite.py`,
`testbench/grpc_server.py`, `testbench/rest_server.py` F1 REST compose/rewrite,
`testbench/filestore.py` `object_inserted`), decide F2 (appendable-upload
`blob.media = upload.media` snapshot-vs-alias), re-pin `tests/media_call_sites.txt`
under `--store file`, and add Mechanism-5 (4GB bounded-memory RSS cap + O(n)-vs-O(n²)
linear-time detector). The memory backend AND the file B≡C invariant (framing +
gRPC chunk boundaries unchanged) must hold; allow-list stays at one entry.

## Environment / toolchain
Nothing is on the bare PATH. Use the venv directly (`nix develop` provisions it):
- `PYTHONPATH=. .venv/bin/python -m pytest …`
- `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store {memory,file}`
- `.venv/bin/isort --quiet <f> && .venv/bin/black --quiet <f>` (isort then black)
- `hypothesis<6.113` is installed in `.venv`.

## How to verify (phase 4)
```bash
git log --oneline 2a55caa..5924408          # phase-4 commits (HEAD = 5924408)
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store memory   # OK; digest 98fa2130…
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file     # OK; diverges only on create-bucket-traversal
TESTBENCH_TEST_STORE=memory PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
TESTBENCH_TEST_STORE=file   PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
gh run list --branch file-backend-design --limit 3        # Unit Test / Docker / Lint all success at 5924408
```
Local note: 3 `tests/test_testbench_startup.py` failures are a pre-existing env
gap (they Popen the system `python3`, which lacks `waitress`); CI's venv has it.
Skip `tests/test_testbench_continue_after_fault_injection.py` locally (macOS hang);
never add that ignore to CI.

## Blockers / decisions reserved for the human
- **PR #1 is a draft; merging to `main` is the maintainer's call — not an agent
  decision.** All phase work lands on `file-backend-design` and is taken to
  CI-green there; the merge is explicitly out of scope without instruction.
