# Handoff: file-backend-foundations (Plan 1 of 4)

## Objective

`google/storage-testbench` is Google's Cloud Storage emulator (Python; Flask/Werkzeug for
the JSON and XML APIs, gRPC for the v2 API, all state in memory). A downstream team wants
to add a **file-backed storage backend** to it so that one application can exercise the
same code paths in three settings: ephemeral storage in tests, persistent local storage in
development, and real GCS in production. Their Rust client uses the GCS v2 gRPC API, which
alternatives like `fake-gcs-server` do not support — hence extending this emulator rather
than substituting another.

The work is split into four plans. **Plan 1, this branch, adds no file backend.** It builds
the machinery that makes adding one provably safe: a reproducible dev environment, a
black-box conformance harness that captures the emulator's current external behavior as
committed goldens, and the first no-op seam (`Store`) that a future `FileStore` will
implement. The point is that Plans 2–4 can then refactor storage and be *proven* not to
have changed observable behavior, because any change shows up as a golden diff.

Design spec: `docs/superpowers/specs/2026-07-27-file-backend-design.md`
Plan 1: `docs/superpowers/plans/2026-07-28-file-backend-foundations.md`

## What's been done

Plan 1 is **complete** — all seven tasks implemented, reviewed, fix-looped, and closed,
followed by a whole-branch review and its fix wave. 41 commits on branch
`file-backend-design`; `origin` is current except for the final docs commit (`0bec76d`).

- **Dev environment.** `flake.nix` provides a devShell (Python 3.12, docker client,
  `docker-compose`, `skopeo`, `gnumake`, `curl`, `jq`) plus a `.venv` provisioned from
  `setup.py` by its `shellHook`. Dependency versions come from pip rather than nixpkgs so
  they match exactly what CI and the Dockerfile install.
- **Verified the design's load-bearing assumption.** `tests/test_crc32c_assumptions.py`
  pins that `crc32c.crc32c(data, seed)` chains, so incremental checksums over streamed
  chunks are sound. It holds at `crc32c==2.7.1`; the spec's large-object plan needs no
  amendment.
- **Conformance harness** in `tests/conformance/`, each file with one responsibility:
  - `symbols.py` — `SymbolTable.bind(kind, value)` maps non-deterministic values to
    stable numbered placeholders, injectively, so aliasing bugs stay visible.
  - `canonicalize.py` — `Canonicalizer.body()`/`.headers()`/`.assert_invariants()`. Two
    whole-tree passes (`_bind_pass` then `_emit`) so nesting depth and field order do not
    affect the result. Substitution is **scoped** to link fields, header values, and error
    envelopes; arbitrary body strings are left alone. `_canonical_link` erases only a URL's
    `scheme://host:port` origin and keeps path and query.
  - `recorder.py` — `Recorder.record_http/record_grpc/record_stream/record_error/finish`.
    Bodies that are JSON are recorded structurally; other bodies as length + SHA-256.
  - `emulator.py` — `Emulator` context manager. Launches `testbench_run.py` (gunicorn on
    POSIX, waitress on Windows) with an explicit environment allowlist, polls readiness
    with a deadline, starts gRPC with a bounded retry, and reaps the process group on every
    failure path including `__enter__` raising.
  - `trace_rest.py`, `trace_grpc.py`, `trace_faults.py` — the domain knowledge.
  - `harness.py` — the only file with a CLI. `verify()` and `capture()`; `--regenerate`,
    `--trace`. Diff hunks are annotated with their enclosing interaction.
- **Committed baseline: 112 interactions** — `golden/rest.json` (47), `golden/grpc.json`
  (48), `golden/faults.json` (17) — covering the JSON API, XML API, gRPC v2, and fault
  injection. Verified green on **both macOS and Linux**.
- **`Store` seam.** `testbench/store.py` defines `Store` (eleven no-op notification
  methods) and `NullStore`. `testbench/database.py` accepts an optional `store`, defaults
  to `NullStore`, and notifies from every mutating method — each call the last statement
  inside the pre-existing `with` block, after the mutation. Production diff vs upstream is
  three files: `testbench/store.py` (new), `testbench/database.py` (+29/−2),
  `testbench/__init__.py` (adds `store` to the package imports).
- **The seam is a verified no-op**: the conformance gate is green with it in place.
- **CI.** `.github/workflows/build.yaml` gains a `Conformance baseline` job on
  `ubuntu-22.04`; `.github/sync-repo-settings.yaml` lists it as a required status check.
  `tests/test_conformance.py` skips on Windows and on any interpreter other than 3.12.
- **`make verify-linux`** runs the gate in a `python:3.12-slim` container so the Linux
  check is repeatable. Verified end-to-end from a cold cache.
- **Docs.** README gains a "Conformance harness" section, the placeholder vocabulary, and
  a "reading a golden diff" section. The spec gains the capture-platform hazard, the
  mutation-check habit, a full accounting of baseline coverage gaps, and the
  carried-forward defects.

**Done but untested:** the Windows branch of `Emulator._terminate` was reasoned from source
and never executed — no Windows machine was available. `test_traces.py` does exercise
`_terminate` on Windows CI (nine launches) even though `test_conformance.py` skips there,
so one green `windows-tests` run is genuinely informative.

## What's left

Ordered by priority.

1. **Push `0bec76d`** (the docs commit). Everything before it is already on `origin`.
2. **Confirm the `Conformance baseline` job is green on CI.** It failed once before, on
   the gzip issue below; that is fixed and verified on Linux locally, but CI runs
   `x86_64` while local verification was `aarch64`, so CI remains authoritative.
3. **Close the framing gap before Plan 2 writes any code.** `DROPPED_HEADERS` in
   `tests/conformance/canonicalize.py` drops both `content-length` and
   `transfer-encoding`, so response framing is invisible to the gate. Plan 2 is the
   `Media` seam — it refactors exactly that axis, so a switch to chunked encoding or a
   `Content-Length` inconsistent with the body would not move a golden. Suggested shape:
   record a derived `framing: content-length|chunked` field plus a
   body-length-consistency flag, so the axis is observable without pinning a value that
   legitimately varies. Regenerate goldens afterwards, and run `make verify-linux`.
4. **Re-establish A ≡ B against the final goldens** (~10 minutes, described under "Key
   decisions"). Restore `gcs/` and `testbench/` from `e8c8507` alongside the current
   `tests/`, run `PYTHONPATH=. python -m tests.conformance.harness`, expect green.
5. **Add the trace coverage Plan 3 needs, before it touches `Database`:**
   - gRPC `UpdateObject` and `UpdateBucket` — **zero interactions today**, and Plan 3's
     first task is routing bucket mutations through `Database`. Highest-value gap.
   - Soft-deleted reads and listings (`soft_deleted=True` on `GetObject`/`ListObjects`).
   - A handful of `call(...)` additions in `tests/conformance/trace_grpc.py`.
6. **Write Plan 2** (spec phase 3, the `Media` seam) with items 3 and 5 as its first
   tasks. Record the configuration-A hash and the A ≡ B result in its header.
7. **Then Plan 3** (spec phases 4–5, `FileStore`/`FileMedia`) and **Plan 4** (phases 6–7:
   large-object bounds, durability, concurrency, real-GCS cross-validation, the downstream
   Rust client).

Lower priority, all recorded in the spec's "Carried-forward defects and constraints":
notifications are guarded against deletion but not misplacement; `_STALL_LABELS` matches
exact labels while the emulator matches `stall-always` by prefix; `tests/test_store.py`
swaps a module global in `setUp` without `addCleanup`; `make verify-linux` resolves
unpinned deps at download time.

## Key decisions

**The harness measures external behavior only, and nothing in `tests/conformance/` may
import `testbench` or `gcs` internals** (`emulator.py` excepted, and only to name a script
to launch). This is what keeps it valid across the refactors it exists to police. Enforced
by the package docstring in `tests/conformance/__init__.py`.

**Canonicalization preserves identity while erasing value.** A scrubber mapping every
generation to `<GEN>` would hide a bug where two objects wrongly share a generation. The
symbol table binds each distinct value to a numbered placeholder on first sight.

**Substitution is scoped, not global.** An earlier version substituted every bound value
into every string, which corrupted unrelated fields — `{"generation": "1", "name":
"file-v1-final.txt"}` became `"file-v<GEN:1>-final.txt"`, and a GCS `id` field's text is a
substring of the `selfLink` path, so it fired on real payloads. A canonicalizer that
mangles data erases the regressions it exists to catch. Do not widen it back.

**Links are canonicalized structurally, not replaced wholesale.** Only the volatile
`scheme://host:port` origin becomes a placeholder; path and query survive so a change to
the emulator's URL scheme is still visible, and an embedded generation still aliases its
sibling field. Wholesale replacement was rejected because it would hide both.

**The golden allowlist was deleted rather than repaired.** It was specified but broken
three ways: an entry keyed by interaction label suppressed nothing, an entry keyed by a
field name suppressed that field across the whole trace, and suppressing every hunk still
left the build red. Its net effect was to hide evidence while keeping the failure, which
routes a developer to `--regenerate` — the one action that erases the baseline. Nothing
needs it yet; add it properly, interaction-scoped, when a first genuinely justified diff
arrives.

**Soft delete, hard delete, and purge each get their own notification.** Collapsing them
would make a `FileStore` destroy restorable copies on a soft delete, breaking `softDeleted`
reads and `restore_object` across a restart. `object_soft_deleted` carries
`hard_delete_time` because a persistent store needs it to expire the copy itself.

**`object_restored` was removed.** `restore_object` calls `insert_object` internally, which
already notifies, so the protocol emitted two notifications for one logical restore and a
`FileStore` would write the blob twice. A restore is an insert from a persistence
standpoint.

**`object_updated` fires unconditionally**, and its contract is "this generation may have
changed", not proof that it did. `Database` cannot inspect an arbitrary `update_fn`. An
earlier version fired nothing, justified by avoiding read-only callers — but all eleven
callers mutate, and two of them (`gcs/upload.py:606`, `:650`) assign `blob.media`, so a
`FileStore` would have missed every appended byte of an appendable object. A redundant
notification costs one idempotent rewrite; a missing one costs data.

**Bucket metadata mutations were deliberately left unobservable.** `bucket_update` and
`bucket_patch` mutate the `Bucket` in place and never call a `Database` mutator. Fixing
that requires changing REST handler control flow, which would forfeit this branch's
provable-no-op property. It is Plan 3's first task, not an oversight.

**The gzipped payload is a committed literal, not `gzip.compress(...)`.** `mtime=0` pins
the gzip timestamp but not byte 9, the OS field: `0xff` from Python's pure-Python writer,
`0x13` from zlib on Darwin, `0x03` on Linux. That byte changed a stored object's `crc32c`
and `md5Hash` and reddened CI on a green local tree. The literal's byte 9 is `0xff` and a
test pins it.

**`--regenerate` is never the way to turn a red build green.** It updates the baseline
after a reviewed, intentional change, explained in the commit message.

**Configuration A vs B, worth understanding before trusting the baseline.** The goldens
were regenerated *after* the `Store` seam landed, because the gzip-literal fix legitimately
changed a trace. So the committed goldens strictly describe configuration **B**
(post-seam). The seam was proven a no-op against the *intermediate* goldens, which is good
evidence but not the same claim. Item 4 under "What's left" closes it properly.

## Architecture notes

**Two habits this branch paid for, both now in the spec.** They are the most transferable
output of the work:

1. *Ask of every recorded value: does it depend on the interpreter, the OS, or a
   third-party library's internals?* Five separate defects here were that one class — the
   `crc32c` seed assumption, a `protobuf` keyword removed rather than deprecated, grpcio's
   private exception class names reaching a golden, transport-exception subclasses
   differing by platform, and the gzip OS byte. Only the last reached CI.
2. *Mutation-check every guard test.* Six tests in this branch passed while testing
   nothing, each found by deleting or reverting the thing under test and watching the suite
   stay green. The worst was the guard added *for* the gzip bug: it asserted the literal
   decompressed to the plaintext, true of any gzip stream, so reintroducing the exact
   regression left it passing.

**Notification placement discipline.** Every `Store` notification is the last statement
inside a pre-existing `with` block, after the mutation. Firing early lets a store observe
state that never became real; firing outside the lock lets concurrent requests deliver out
of order. `clear()` holds both `_resources_lock` and `_folders_lock` continuously through
its notification.

**Lock ordering.** `database.py`'s `clear()` is the only site acquiring both locks, in the
order `_resources_lock` → `_folders_lock`. A `Store` notification that re-enters `Database`
from a *folder* notification and touches bucket or object state would form the reverse
order and risk deadlock. Re-entrancy works at all only because the locks are
`threading.RLock`. Documented in `testbench/store.py`'s Consequences section.

**`bucket_name` in notifications is proto-form** (`projects/_/buckets/<name>`) and is *not*
round-trippable through `Database`'s own API: `__bucket_key` uses `context is not None` as
a proxy for "already proto form" and `bucket_name_to_proto` prepends unconditionally. A
re-entrant lookup needs a non-`None` `context`. Also: the value is unvalidated caller input,
and `gcs/bucket.py`'s validator has a bypass — it branches on `if "." in bucket_name:` and
in that branch checks only lengths, so `../../etc/passwd` is accepted. A `FileStore` must
validate names itself. A test pins the bypass as current behavior.

**Single gunicorn worker.** `testbench_run.py` passes `--threads=10` with no `--workers`,
so there is one process and one in-memory database — which is what makes responses
deterministic. It also passes `--reload`, so writing a `.py` file into the repo while an
emulator is live restarts the worker and wipes its state mid-trace.

## Current state

- **Build:** Python, no compile step. All new files parse under
  `ast.parse(feature_version=(3, 8))` — the floor is 3.8 and CI runs a 3.8–3.12 matrix.
- **Tests:** `493 passed, 13 subtests passed`. `tests/conformance/` + `tests/test_store.py`
  is `87 passed` with `-W error::ResourceWarning` clean.
- **Known local failure, pre-existing and not caused by this branch:**
  `tests/test_testbench_continue_after_fault_injection.py` **hangs** on macOS at its second
  test, `test_repeated_broken_stream_faults_by_header`, because that test issues
  `requests.get(..., stream=True)` with no `timeout=`. Reproduces at the base commit.
  Exclude it locally with `--ignore=tests/test_testbench_continue_after_fault_injection.py`.
  **Never add that exclusion to CI**, and do not edit the file.
  `tests/test_testbench_startup.py` also leaks gunicorn processes (`localhost:0` in `ps`) —
  pre-existing; `Emulator` always binds `127.0.0.1:<port>`, so the two are distinguishable.
- **Conformance gate:** green on macOS and on Linux.
- **Formatting:** `black==22.3.0` and `isort==5.12.0` are idempotent and clean. Run isort
  **then** black — a bare `isort --check-only` disagrees with the combination on one
  pre-existing import line, and the combination is what CI enforces.
- **No long-running processes.** Check for strays after any harness run.
- **Setup:** `nix develop` provisions everything. A Docker daemon is needed only for
  `make verify-linux` and comes from colima/Docker Desktop, not from Nix. On this machine
  colima is running an Ubuntu 24.04 aarch64 VM.

## Files touched

```
.github/sync-repo-settings.yaml   M  adds "Conformance baseline" to required status checks
.github/workflows/build.yaml      M  adds the ubuntu-22.04 Conformance baseline job
.gitignore                        M  adds .direnv/, result, result-*
Makefile                          A  make verify-linux / linux-image / linux-wheels / clean-linux-cache
README.md                         M  conformance harness, placeholder vocabulary, reading a golden diff, verify-linux
docs/superpowers/specs/2026-07-27-file-backend-design.md   A  design spec (amended during execution)
docs/superpowers/plans/2026-07-28-file-backend-foundations.md A plan 1 (amended 9x during execution)
flake.nix / flake.lock            A  devShell; provisioning steps &&-chained so failure aborts before stamping
testbench/store.py                A  Store protocol + NullStore, with the Consequences contract
testbench/database.py             M  optional store, store property, 11 notifications, do_update_object comment
testbench/__init__.py             M  adds store to the package import list
tests/conformance/__init__.py     A  package docstring stating the no-internals rule
tests/conformance/symbols.py      A  SymbolTable
tests/conformance/canonicalize.py A  Canonicalizer, NONDETERMINISTIC_FIELDS, DROPPED_HEADERS, invariants
tests/conformance/recorder.py     A  Recorder
tests/conformance/emulator.py     A  Emulator subprocess lifecycle
tests/conformance/harness.py      A  CLI: verify/capture/serialize, hunk annotation
tests/conformance/trace_rest.py   A  JSON + XML API trace, GZIPPED_PAYLOAD literal
tests/conformance/trace_grpc.py   A  gRPC v2 trace, incl. the documented RenameFolder bug pins
tests/conformance/trace_faults.py A  fault injection, split across both emulator mechanisms
tests/conformance/golden/*.json   A  the baseline: rest 47, grpc 48, faults 17 interactions
tests/conformance/test_*.py       A  unit tests for symbols, canonicalize, recorder, emulator, harness, traces
tests/test_conformance.py         A  pytest entry point; skips on Windows and non-3.12
tests/test_crc32c_assumptions.py  A  pins the incremental-checksum assumption
tests/test_store.py               A  RecordingStore; call sequences, ordering, inertness, name-bypass pin
```

## Blockers and risks

- **Nothing is blocked.** No external dependency, credential, or review is outstanding.
- **CI on `x86_64` is unverified by me.** Local Linux verification ran `aarch64` via
  colima. Content hashes and JSON serialization are architecture-independent, so the risk
  is low, but it is not literally CI's environment.
- **The `Conformance baseline` job has never been observed green.** Its one run failed on
  the gzip issue, now fixed. Treat the first green run as the real confirmation.
- **A red *first* CI run means the goldens need capturing on Linux, not `--regenerate`
  locally.** This is the trap the whole harness is vulnerable to.
- **Windows CI is unverified for the emulator teardown path** (see "Done but untested").
- **`colima` specifics that cost real time to diagnose**, all encoded in the Makefile and
  README: the VM often has no network egress even when the host shell does, so
  `make verify-linux` fetches its image with `skopeo` host-side and `docker load`s it, and
  downloads wheels host-side for `--no-index` install; colima mounts only `$HOME`, so the
  wheel cache cannot live in `/tmp`; and grpcio 1.70.0's aarch64 wheel is tagged only
  `manylinux_2_17_aarch64` with no `manylinux2014` alias, so passing just the latter makes
  pip report "no matching distribution" for a version that plainly exists.
- **The execution ledger at `.superpowers/sdd/2026-07-28-file-backend-foundations/` is
  git-ignored scratch.** Everything durable from it has been moved into the spec and the
  plan's handoff section, so it can be deleted. Read it first if you want the blow-by-blow.

## How to verify

```bash
# 1. Environment
nix develop                       # provisions .venv from setup.py on first entry

# 2. Full suite (the one hanging file is excluded; see Current state)
source .venv/bin/activate
PYTHONPATH="." pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
# expect: 493 passed, 13 subtests passed

# 3. The conformance gate — the property everything rests on
PYTHONPATH="." python -m tests.conformance.harness
# expect: OK faults / OK grpc / OK rest, exit 0
# NEVER run this with --regenerate to make a red result go away.

# 4. The same gate on Linux, which is what CI runs
nix develop --command make verify-linux
# expect: platform: 3.12.x <arch> Linux ... then OK faults / OK grpc / OK rest

# 5. Prove the gate actually detects a change (do this once, then revert)
#    Perturb any value in tests/conformance/golden/rest.json and re-run step 3.
#    Expect FAIL with a hunk annotated "interaction: '<label>'".

# 6. Confirm the production footprint is only the seam
git diff --name-only e8c8507..HEAD -- gcs/ testbench/ testbench_run.py
# expect exactly: testbench/__init__.py, testbench/database.py, testbench/store.py

# 7. Formatting, in CI's order
isort --quiet tests/ testbench/ && black --quiet tests/ testbench/ && git diff --exit-code

# 8. No stray processes after any harness run
ps ax | grep -E 'gunicorn|waitress|testbench' | grep -v grep
# Emulator binds 127.0.0.1:<port>; test_testbench_startup.py's localhost:0 leaks
# are pre-existing and not from this branch.
```

To re-prove the `Store` seam is a no-op against the *final* goldens (item 4 of "What's
left"): restore `gcs/` and `testbench/` from `e8c8507` into a scratch worktree, copy the
current `tests/` over it, and run step 3 there. Green means the committed baseline
describes untouched-upstream behavior.
