# File Backend for storage-testbench — Design

Date: 2026-07-27
Status: approved design, not yet implemented

## Goal

Add an opt-in, file-backed storage backend to `storage-testbench` so that a downstream
application can satisfy all of the following at once:

1. Tests, local development, and production exercise the same application code paths.
2. Tests get ephemeral GCS storage.
3. Local development gets persistent, local, file-backed GCS storage.
4. Production uses the real GCS API.
5. The application's Rust client uses the GCS v2 gRPC API, which alternatives such as
   `fake-gcs-server` do not support.

The testbench already implements a broad GCS v2 gRPC surface (`ReadObject`,
`BidiReadObject`, `WriteObject`, `BidiWriteObject`, `StartResumableWrite`,
`QueryWriteStatus`, `ComposeObject`, `RewriteObject`, `MoveObject`, folders,
`GetStorageLayout`), so requirement 5 is already met. This design adds requirement 3
without regressing anything, and makes requirement 2 use the same backend as
requirement 3 so that test and dev parity is maximised.

## Feasibility

Feasible. The repository is well shaped for this change:

- All mutable state lives in a single object, `Database` (`testbench/database.py:31`),
  holding plain dicts guarded by `RLock`s.
- Object bytes live in exactly one field, `gcs.object.Object.media`
  (`gcs/object.py:70`), and `Upload.media` (`gcs/upload.py:58`).
- The gRPC and REST servers share one process and one `Database` instance. gRPC is
  deliberately started inside the gunicorn worker for this reason
  (`testbench/rest_server.py:162-180`).

There is therefore exactly one seam for metadata persistence and one for object bytes.

The work is medium-sized rather than small because of the multi-GB object requirement,
not because of persistence. Several existing operations are whole-buffer operations
that are correct at test scale and unusable at GB scale. See "Large-object handling"
and "Risks".

### Inherent parity gap (out of scope, stated for the record)

The testbench performs no ACL or IAM enforcement and far fewer validations than real
GCS. Consequently, development and tests will not surface authorization or permission
bugs, regardless of this work. This is a pre-existing property of the testbench.

## Non-goals

- Multi-process or multi-container operation over a shared directory. One emulator
  process owns one root directory.
- Transactional durability. See "Locking and durability".
- Resumable-upload survival across an emulator restart. In-flight uploads are lost on
  restart. The on-disk layout leaves room to add this later.
- Adopting hand-dropped files that have no sidecar. Seeding goes through the API.
- Upstreaming to `googleapis/storage-testbench` as a blocking requirement. The design
  keeps the option open (opt-in seams, no new runtime dependencies, default behavior
  byte-identical) but does not wait on it.

## Architecture

Two independent, opt-in abstractions. Both default to today's exact behavior.

```
                    ┌─────────────────────────────────┐
  gRPC v2 ─────────►│ grpc_server.py / rest_server.py │  (unchanged)
  REST/XML ────────►└──────────────┬──────────────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │  Database  (index + locks)      │  ← seam A: persistence
                    │  _buckets/_objects/_live_gens   │
                    └──────────────┬──────────────────┘
                                   │ Store protocol
                    ┌──────────────▼──────────────────┐
                    │  NullStore    │    FileStore     │
                    └─────────────────────────────────┘

  gcs.object.Object.media ──► Media protocol   ← seam B: bytes
                              BytesMedia (default) │ FileMedia (lazy, mmap)
```

### Seam A: `Store`

`Database` keeps its dicts as the authoritative **in-memory index**, so listing,
generation tracking, precondition evaluation, and locking logic are untouched. Each
mutation additionally write-throughs to a `Store`.

- `NullStore` does nothing. Behavior is byte-identical to today.
- `FileStore` writes and reads sidecar metadata, and rebuilds the index by scanning the
  tree at startup.

Touch points are the existing mutating methods in `testbench/database.py`:
`insert_bucket`, `delete_bucket`, `insert_object`, `delete_object`,
`do_update_object`, `restore_object`, and the folder operations.

### Seam B: `Media`

`Object.media` and `Upload.media` change from raw `bytes` to a `Media` object with:

| Operation | Purpose |
|---|---|
| `__len__()` | object size |
| `__getitem__(slice)` → `bytes` | range reads |
| `chunks(begin, end, size)` | streaming reads and downloads |
| `append(data)` | resumable upload accumulation |
| `crc32c()`, `md5()` | incremental, cached checksums |
| `reader()` | file-like, for streaming gzip |
| `finalize(dest)` | promote staging file to its final path |

`BytesMedia` wraps `bytes` and preserves today's semantics; it is what `NullStore`
uses. `FileMedia` is backed by a real file, reads via `mmap`, appends via `O_APPEND`,
and maintains rolling checksums.

### Why two seams rather than one

Metadata is small, transactional, and needs atomic replace. Bytes are large,
append-only, and need streaming. Collapsing them would force the metadata path to
inherit the byte path's chunking complexity for no benefit.

## On-disk layout

GCS-shaped and browsable, with a single reserved prefix (`.gcs/`) per bucket, so
exactly one name pattern requires escaping.

```
/data/
  my-bucket/
    .gcs/
      bucket.json                          ← Bucket proto as JSON
      generations/audio%2Fclip.wav/1753…   ← non-live generations (media + .json)
      soft_deleted/…
      uploads/<upload_id>{,.json}          ← in-flight resumable staging
      folders/<escaped>.json               ← hierarchical-namespace folders
      overflow/<sha256>{,.json}            ← names the filesystem cannot represent
    audio/clip.wav                         ← LIVE object bytes, at its natural path
    audio/clip.wav.gcsmeta                 ← sidecar: Object proto as JSON
  other-bucket/
    …
```

### Rules

1. **The sidecar is the source of truth.** A media file with no sidecar is not a live
   object. Write order is: write media, `os.replace` into place, write sidecar to a
   temporary file, `os.replace` it. A crash before the sidecar lands leaves an
   invisible orphan rather than a corrupt object.
2. **No index file.** Startup walks the tree and reads sidecars to build the in-memory
   index. There is nothing to corrupt or to fall out of sync, and a bucket directory
   can be deleted or copied between machines with `rm -rf` and `cp -r`.
3. **Sidecar format is the proto as JSON**, via
   `json_format.MessageToJson(storage_pb2.Object)`, wrapped in a small envelope
   carrying a schema version and the true (unescaped) object name. This round-trips
   exactly and is identical to the internal representation, so there is no second data
   model to maintain.
4. **Only the live generation sits at its natural path.** Older generations and
   soft-deleted objects live under `.gcs/`, keeping the browsable view clean.

### Escaping

Escaping is minimal and reversible: percent-encode only the characters and patterns
that actually break, so ordinary object names appear pristine on disk.

Some legal GCS object names cannot be represented as filesystem paths at all:

- any path segment longer than 255 bytes (`NAME_MAX`), which a 1024-byte name without
  slashes will exceed;
- names with a trailing `/`, which GCS permits and uses for folder placeholders;
- `.` or `..` as a path segment, which the filesystem collapses;
- names beginning with the reserved `.gcs/` prefix;
- names ending in the reserved `.gcsmeta` suffix, which would otherwise shadow the
  sidecar of another object — an object named `audio/clip.wav.gcsmeta` collides with
  the sidecar of `audio/clip.wav`.

These go to `.gcs/overflow/<sha256-of-name>` with a sidecar carrying the real name. The
startup scan reads overflow entries like any other object, so nothing downstream
special-cases them.

Without the overflow store the emulator would reject names that real GCS accepts,
which breaks requirement 1.

### Collision detection

If two distinct object names map to the same path — the case that arises on a
case-insensitive macOS bind mount, where `Clip.wav` and `clip.wav` collide — the
startup scan **fails loudly** rather than silently losing data.

## Large-object handling

`FileMedia` is what makes multi-GB objects work, and it is mostly about not
materialising buffers.

- **Reads** are `mmap`-backed slices, so `ReadObject` and `BidiReadObject` range
  serving costs no extra memory.
- **REST downloads** currently emit the whole payload in a single
  `yield response_payload` (`gcs/object.py:515-517`). This becomes a chunked generator.
- **Uploads** append to a staging file under `.gcs/uploads/`, and finalize is an
  `os.replace`. A multi-GB upload never holds more than one chunk in memory, and
  finalizing is O(1) rather than a copy.
- **Rolling checksums.** `gcs/upload.py:590` recomputes `crc32c.crc32c(upload.media)`
  over the entire accumulated buffer on every BidiWrite flush, which is O(n²) in the
  upload size and would alone make multi-GB uploads unusable. `FileMedia` maintains
  incremental crc32c and incremental md5 via `hashlib`'s streaming interface. The same
  applies to `Object.init` (`gcs/object.py:114-115`), which currently hashes the whole
  buffer.
- **Copies** — `ComposeObject` (`testbench/grpc_server.py:530`) and `RewriteObject`
  (`testbench/grpc_server.py:1115`) — become chunked file-to-file streaming into a
  staging file.
- **Decompressive transcoding** (`gzip.decompress(self.media)`, `gcs/object.py:499`)
  becomes `gzip.GzipFile` over `media.reader()`, streamed.

### Dependency to verify first

The design assumes the `crc32c` package accepts a seed value, i.e. that
`crc32c.crc32c(b"world", crc32c.crc32c(b"hello")) == crc32c.crc32c(b"helloworld")`.
This is verified in phase 1. If it does not hold, incremental chaining is implemented
manually.

## Security: path handling

Bucket and object names are fully caller-controlled — via `CreateBucket(name)` and
`WriteObject`/`BidiWriteObject` over gRPC, and via REST routes such as
`@root.route("/<bucket_name>/<path:object_name>")` (`testbench/rest_server.py:101`).
Legal GCS object names contain `/`. Mapping them onto filesystem paths is therefore a
path-traversal surface, and the emulator has **no authentication of any kind**. The
escaping scheme above exists for representability; the following rules exist for
containment, and are independent of it.

1. **Containment check on every resolved path.** After building a path, resolve it with
   `os.path.realpath` and verify the result is still inside the bucket root before
   opening. This is the backstop that holds even if the escaping logic has a bug.
2. **Reject rather than sanitise** names that are absolute, contain a NUL byte, or
   contain `.`/`..` segments. `.`/`..` already route to the overflow store, which
   sidesteps traversal entirely because the on-disk name is a SHA-256 of the object
   name and contains no caller bytes.
3. **Do not follow symlinks.** Open the final path component with `O_NOFOLLOW`, and
   prefer `openat` against a directory descriptor for the bucket root so containment
   cannot be defeated by a symlink swapped in between the check and the open.
4. **Validate bucket names against GCS bucket naming rules yourself** (3–63
   characters, lowercase alphanumerics, hyphens, underscores, dots) *before* they
   become directory names -- do not assume the emulator already did this. It does
   not: `gcs/bucket.py`'s `__validate_json_bucket_name` branches on `if "." in
   bucket_name:`, and in that branch checks only length constraints, never the
   character-class regex the other branch enforces. A dotted, traversal-shaped name
   such as `"../../etc/passwd"` (which contains a `.`) is accepted today and reaches
   every `Store` notification unchanged -- confirmed against a live emulator and
   pinned as a test in `tests/test_store.py`. Because a notification's bucket name
   arrives as `projects/_/buckets/<name>`, the natural "strip the prefix" first step
   hands a handler `../../etc/passwd` directly. The file backend's own validation is
   therefore not a redundant belt-and-suspenders check; it is the *only* check.
5. **Constrain `shutil.rmtree`.** Only remove a path that is a direct child of the
   root, is a real directory rather than a symlink, and corresponds to a bucket present
   in the index.
6. **Do not publish the emulator beyond the compose network.** Both listeners bind
   `0.0.0.0` today (`testbench/grpc_server.py:1287`, and the Dockerfile's gunicorn
   bind) and neither authenticates. Ports should be published to loopback only, or not
   at all where a container can reach the service by service name.

Together with the collision check, these turn every hostile or malformed name into
either an overflow-store entry or a loud error, never a write outside the root.

## Locking and durability

The emulator serves many concurrent tests through one global `_resources_lock`, so
lock granularity matters.

- **Sidecar metadata writes stay inside the lock.** They are a few KB plus an
  `os.replace`, so sub-millisecond, and keeping them under the lock means the in-memory
  index and the disk never disagree.
- **Bulk media I/O happens outside the lock**, against immutable staging files.
  Otherwise a single test streaming 5 GB blocks every other test's `GetObject`.
- **No `fsync` by default.** Ordering via `os.replace` is sufficient to guarantee that
  a sidecar never references media that is not fully written. Durability is explicitly
  best-effort: a hard kill can lose the last write. An optional `TESTBENCH_FSYNC=1`
  enables fsync for anyone who needs it.

## Test isolation

Tests share one emulator container and create a **UUID-named bucket per test**, rather
than spawning a container per test, which has significant overhead.

```
one gcs-test container, TESTBENCH_STORE=file, tmpfs /data
   test A ──► CreateBucket("test-<uuid>")  ──► /data/test-<uuid>/
   test B ──► CreateBucket("test-<uuid>")  ──► /data/test-<uuid>/
   teardown ─► delete objects, DeleteBucket ─► rmtree(/data/test-<uuid>)
```

This needs no new API. `CreateBucket` (`testbench/grpc_server.py:275`) and
`DeleteBucket` (`testbench/grpc_server.py:257`) already exist over gRPC v2, and the
file store hooks bucket create and delete to directory create and `rmtree` through the
existing `insert_bucket` and `delete_bucket` methods (`testbench/database.py:107`,
`:191`).

UUID bucket names are filesystem-safe by construction and never reach the escaping
path.

tmpfs remains as the container-lifetime backstop, so nothing survives the container.
Note that tmpfs is RAM-backed: it suits the small-file suite but not multi-GB test
fixtures, which should use a normal volume or the container's writable layer.

### Consequences to design for

1. **`DeleteBucket` refuses non-empty buckets** (`testbench/database.py:194-202`),
   matching real GCS. Test teardown must delete objects first, which means the cleanup
   helper also works against real GCS. The file store additionally `rmtree`s
   defensively so a crashed test's stale directory does not leak into the next startup
   scan, and does not fail if the directory is already gone.
2. **State that is global rather than per-bucket**, and therefore shared across tests
   in one container: the retry-test registry, the monotonic generation counter
   (`gcs/object.py:38`), the auto-created env bucket, and project and HMAC state. Fault
   injection is keyed by `x-retry-test-id` and so is naturally per-test, but a test
   asserting on `ListBuckets` output must filter by prefix because it will see every
   other test's buckets.
3. **`TESTBENCH_BUCKETS` is development-only.** Tests create their own buckets.

## gRPC concurrency

`_GRPC_SERVER_THREAD_COUNT = 2` (`testbench/grpc_server.py:45`) is passed to the
server's `ThreadPoolExecutor` (`testbench/grpc_server.py:1279`). Two threads total, and
`ReadObject`, `BidiReadObject`, and `BidiWriteObject` are long-lived streaming RPCs
that each hold a thread for the duration of a transfer.

With a container per test this is invisible. With one shared container and a parallel
suite, two concurrent large uploads starve every other gRPC call, appearing as random
test timeouts. This becomes a `TESTBENCH_GRPC_THREADS` setting defaulting to 32.

## Configuration and deployment

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `TESTBENCH_STORE` | `memory` | `memory` (today's behavior) or `file` |
| `TESTBENCH_ROOT` | — | Root directory, required when `TESTBENCH_STORE=file` |
| `TESTBENCH_BUCKETS` | — | Comma-separated buckets created idempotently at startup |
| `TESTBENCH_GRPC_PORT` | — | Start the gRPC server at boot on this port |
| `TESTBENCH_GRPC_THREADS` | `32` | gRPC server thread-pool size |
| `TESTBENCH_FSYNC` | `0` | fsync metadata writes |

Two of these fix existing footguns:

- **`TESTBENCH_BUCKETS`** — today only one bucket can be auto-created, via
  `GOOGLE_CLOUD_CPP_STORAGE_TEST_BUCKET_NAME` (`testbench/database.py:216`). Optional
  per-bucket JSON overrides cover settings that change semantics, such as versioning
  and soft-delete policy.
- **`TESTBENCH_GRPC_PORT`** — today the gRPC server starts only when something issues
  `GET /start_grpc?port=N` (`testbench/rest_server.py:162`), which under compose means
  a fragile post-start `curl`.

### Single worker

gunicorn defaults to one worker, but N workers over one root directory means N
divergent in-memory indexes. The file store asserts a single worker at startup rather
than corrupting data quietly.

### docker compose

Two services from the same image, same code path, different backend configuration.

```yaml
services:
  gcs-dev:                          # requirement 3: persistent, file-backed
    environment:
      TESTBENCH_STORE: file
      TESTBENCH_ROOT: /data
      TESTBENCH_BUCKETS: audio,transcripts,models
      TESTBENCH_GRPC_PORT: 9001
    volumes: [ "gcs-data:/data" ]    # named volume, NOT a host bind mount
    ports: [ "9000:9000", "9001:9001" ]

  gcs-test:                         # requirement 2: ephemeral
    environment:
      TESTBENCH_STORE: file
      TESTBENCH_ROOT: /data
      TESTBENCH_BUCKETS: ""          # tests create UUID buckets themselves
      TESTBENCH_GRPC_PORT: 9001
    tmpfs: [ /data ]

volumes:
  gcs-data:
```

### Named volume, decided

The dev service uses a Docker **named volume**, not a host bind mount.

A named volume is case-sensitive and fast for large-file I/O. The cost is that the tree
is reachable through `docker compose exec gcs-dev ls /data` or `docker cp` rather than
directly in Finder. A host bind mount (`./.gcs-data:/data`) would be directly
browsable — which is what motivated the GCS-shaped layout — but on macOS it is
case-insensitive, so the startup collision check would reject two object names
differing only in case, and large-file I/O across the VirtioFS boundary is markedly
slower. Given that the dataset includes multi-GB files, both costs bite.

Correctness wins, and the layout stays browsable through `exec`. Switching to a bind
mount later is a one-line compose change, and because of the collision check the
failure mode would be a loud startup error rather than silent data loss.

## Nix flake

`flake.nix` provides a `devShell` containing:

- Python 3.12, matching the pinned Dockerfile base image;
- the pinned runtime dependencies from `setup.py` — `grpcio`, `grpcio-status`,
  `grpcio-tools`, `googleapis-common-protos`, `protobuf`, `flask`,
  `requests-toolbelt`, `scalpl`, `crc32c`, `gunicorn`, `waitress`, `Werkzeug`;
- `pytest`, `coverage` (for the Mechanism 3 gate), and `hypothesis` (for Mechanism 4) —
  development dependencies only, never in `setup.py`;
- `docker-client` and `docker-compose`. The daemon comes from Docker Desktop or
  Colima; nixpkgs' full `docker` package builds a daemon that is not useful on Darwin.

So `nix develop` yields a working `pytest` and `testbench_run.py` with no virtualenv.
The file backend itself adds **no new Python runtime dependencies** — stdlib `os`,
`mmap`, `hashlib`, `json`, plus the already-present `crc32c` and `protobuf` — which
preserves the upstreaming option.

## Verification plan

The central risk of this design is that it touches read and write paths shared by every
API in the emulator. "The existing suite passes" is necessary but not sufficient: the
suite was written to test GCS semantics, not to pin down every byte of external
behavior, and it may not reach the fault-injection and checksum edges that the `Media`
seam disturbs. This section defines what must be *demonstrated*, and by what mechanism.

### The invariant to prove

External, over-the-wire behavior is identical across three configurations:

```
  A: pre-change,  memory backend      (pristine main)
  B: post-change, memory backend      (NullStore + BytesMedia)
  C: post-change, file backend        (FileStore + FileMedia)

  required:  A ≡ B ≡ C
```

- **A ≡ B** proves the refactor is behavior-preserving. This is the gate for phases 2
  and 3, the invasive ones.
- **B ≡ C** proves the new backend is equivalent to the old. This is the gate for
  phases 4 and 5.

Equivalence means: identical status codes, identical response bodies, identical
response headers, identical gRPC messages, identical stream chunk boundaries where the
API pins them, and identical error taxonomies — after canonicalization of the
inherently non-deterministic fields listed below.

Deliberate, intended differences are permitted but must be enumerated in an explicit
allow-list with a justification per entry, reviewed as part of the change. An
unexplained diff is a defect.

### Mechanism 1: run the existing suite against both backends

Highest leverage, lowest cost. Both existing test styles funnel through
`Database.init()` — REST tests use the module-level singleton
(`testbench/rest_server.py:31`) and call `db.clear()` in `setUp`, while gRPC tests
construct their own (`tests/test_grpc_server.py:45`). So a single chokepoint controls
the backend for the whole suite.

Add a `conftest.py` (the repo currently has none) exposing a backend parameter driven by
`TESTBENCH_TEST_STORE=memory|file`. When `file`, `Database.init()` returns a
file-backed database rooted at a per-test temporary directory, and `Database.clear()`
also empties that directory. CI runs the full suite twice, once per value.

This gets all 30 existing test files executing against the file backend with **no test
rewrites**, and it is the primary evidence for B ≡ C on ordinary semantics. Its
weakness is that it cannot speak to A ≡ B, since pristine `main` has no file backend and
no `conftest.py`; that is what Mechanism 2 is for.

### Mechanism 2: black-box golden-master differential harness

This is the core of the pre/post proof.

A standalone harness, `tests/conformance/`, drives a **running emulator purely
over the wire** — HTTP/JSON, XML, and gRPC — and records every response to disk. It
**must not import `testbench` or `gcs` internals**, so that it is immune to the refactor
it is measuring and can run unchanged against pristine `main`.

The trace is a fixed, ordered script covering: bucket CRUD and IAM, object insert via
simple/multipart/resumable/XML upload, `WriteObject` and `BidiWriteObject`, ranged and
full reads over REST and `ReadObject`/`BidiReadObject`, decompressive transcoding,
compose, rewrite (including multi-call rewrite with continuation tokens), move,
generations and versioning, soft-delete and restore, folders, ACLs, CSEK, precondition
failures, and every fault-injection instruction in the README (`return-broken-stream`,
`return-corrupted-data`, `stall-*`, `return-503-after-256K`, `redirect-*`).

Workflow:

1. **Before phase 2**, run the harness against pristine `main` and commit the output as
   `tests/conformance/golden/`. This is configuration A, captured once, and it is the
   only artifact that cannot be regenerated later.
2. Every subsequent phase re-runs the harness and diffs against the goldens, in both
   memory and file configurations.
3. A `--regenerate` flag exists, but any commit that changes a golden file must justify
   the change in its message and in the allow-list. This is the review hook.

#### Canonicalization

The harness is worthless without careful handling of non-determinism, so this is
specified rather than left to implementation. Values are replaced through a **symbol
table**: the first occurrence of a non-deterministic value binds a stable placeholder
(`<GEN:1>`, `<UPLOAD:2>`), and every later occurrence of the *same* value reuses that
binding. This erases the value while preserving identity relationships, so aliasing
regressions — two objects wrongly sharing a generation, a rewrite token leaking across
requests — still show up as diffs.

| Field | Handling |
|---|---|
| `generation` (`gcs/object.py:38`, time-seeded) | symbol; assert positive int and monotonically increasing across the trace |
| `create_time`, `update_time`, `finalize_time`, `soft_delete_time`, `hard_delete_time` | symbol; assert RFC 3339 and correct relative ordering |
| `upload_id`, rewrite tokens, retry-test ids (`testbench/database.py:742`) | symbol |
| `self_link`, `media_link`, bucket `id` | symbol after substituting embedded generations |
| `x-goog-generation`, `x-goog-metageneration` headers | symbol, consistent with the body |
| `Date`, `Server`, `Content-Length` on chunked responses | dropped |
| `metageneration`, `etag` | **kept verbatim** — both are deterministic (etag is an md5 of metageneration, `gcs/object.py:91`), so they are real signal |

Streaming responses record chunk boundaries as well as concatenated bytes, because
chunking is externally visible to a client and the `Media` seam is precisely what could
change it.

### Mechanism 3: coverage-gated call-site audit

The `Media` seam audit spans roughly 40 call sites, and "we audited them" is not
verifiable. So the audit output becomes a committed, machine-readable checklist,
`tests/media_call_sites.txt`, listing every site as `path:line`.

A test then runs the conformance trace under `coverage.py` (already configured in this
repo, see `.codecov.yml`) and asserts that **every listed line was executed**. A new
`.media` use added without corresponding coverage fails the gate. This converts the
audit from a claim into a check, and it is what makes the highest-risk phase auditable.

### Mechanism 4: property-based name and layout round-trip

Escaping, the overflow store, and collision detection need adversarial input rather than
hand-picked examples. Using `hypothesis` (a **development** dependency only — it goes in
the flake, never in `setup.py`, preserving the zero-runtime-dependency property):

- Generate legal GCS object names — 1–1024 UTF-8 bytes, embedded `/`, Unicode,
  spaces, `%`, `#`, `?`, emoji, near-`NAME_MAX` segments, trailing `/`, `.`/`..`
  segments, `.gcs/` prefixes, `.gcsmeta` suffixes.
- Assert: write via the API, read back via the API, bytes and metadata identical; and
  `unescape(escape(name)) == name` for every name, including overflow entries.
- Assert the same names behave identically on the memory backend (B ≡ C at the name
  level).

Plus explicit, non-generated adversarial cases for the rules in "Security: path
handling" — `../../etc/passwd`, `a/../../../x`, `/etc/passwd`, embedded NUL, a symlink
planted in the root before startup — each asserting that nothing is written or read
outside the root, and that the failure is loud.

### Mechanism 5: large-object bounds

Two properties, neither of which needs a checked-in large fixture: the payload is a
deterministic PRNG stream, and reads are verified by re-deriving the same stream.

- **Bounded memory.** Upload and then download a 4 GB object, sampling process RSS
  throughout, and assert peak growth stays under a fixed cap (256 MiB above baseline).
  This is what prevents a regression back to memory-resident media.
- **Linear-time detector for the O(n²) checksum bug.** Time an upload at size N and at
  2N, and assert `t(2N)/t(N) < 3`. Linear behavior gives ~2, the current
  whole-buffer recompute at `gcs/upload.py:590` gives ~4. This is a cheap, robust
  detector that does not depend on absolute machine speed. CI uses N = 256 MiB; a
  nightly job uses multi-GB sizes.

### Mechanism 6: durability and crash behavior

File backend only, so these are B-less assertions — they have no memory-backend
counterpart and are therefore new behavior, not preserved behavior:

- Write objects, stop the emulator gracefully, restart, and assert every read-only
  assertion from the conformance trace still holds.
- `SIGKILL` mid-upload, restart, and assert the partial object is invisible (no sidecar)
  and the bucket is otherwise intact.
- Truncate or corrupt a sidecar, restart, and assert a loud failure rather than silent
  data loss.
- Plant two object names that collide case-insensitively and assert startup fails
  loudly.

### Mechanism 7: concurrency

Run N parallel streaming transfers plus interleaved metadata operations against one
emulator, asserting no request is starved and all responses are correct. This is the
test that would have caught `_GRPC_SERVER_THREAD_COUNT = 2`
(`testbench/grpc_server.py:45`), and it also exercises the claim that bulk media I/O
happens outside `_resources_lock`.

### Mechanism 8: cross-validation against real GCS (opt-in)

The premise of this work is dev/test/prod parity, and no amount of emulator self-testing
can confirm the emulator matches GCS. So the conformance trace is runnable against a
**real GCS bucket** with the same canonicalization, producing a divergence report.

This requires a project, credentials, and money, so it is a manually triggered job
rather than per-commit. Divergences are recorded as known gaps — the ACL/IAM
non-enforcement noted under "Feasibility" will appear here, as will signed-URL
verification. The value is that the parity gap becomes a reviewed document instead of an
assumption.

### Mechanism 9: the downstream client (outer loop)

None of the above exercises the application's Rust client, which is the actual
consumer. The final acceptance check is the application's own smoke suite run against
the emulator in both memory and file configurations. That lives in the application
repository, not here, but it is the last gate before adopting the file backend for
local development.

### Per-phase gates

| Phase | Must be green | New evidence produced |
|---|---|---|
| 1 — flake, harness | existing suite; harness runs on pristine `main` | `tests/conformance/golden/` (configuration A) |
| 2 — `Store` seam, `NullStore` | existing suite; harness diff clean | A ≡ B for metadata paths |
| 3 — `Media` seam, `BytesMedia` | existing suite; harness diff clean; coverage gate | A ≡ B for byte paths; `media_call_sites.txt` |
| 4 — `FileStore` | suite on both backends; harness diff clean on both | B ≡ C for metadata; property-based names; traversal cases |
| 5 — `FileMedia` | as above | B ≡ C for bytes; bounded memory; linear-time detector |
| 6 — bootstrap, compose | as above | single-worker assertion; boot-time gRPC and bucket seeding |
| 7 — full verification | everything above | durability and crash suite; concurrency suite; optional real-GCS divergence report |

Phases 2 and 3 are pure refactors: the harness diff must be **empty**, with no
allow-list entries. Any diff at those phases is a bug, which is what makes the risky
work safe to land incrementally.

### What this plan does not prove

Stated so the gaps are chosen rather than discovered:

- That the emulator matches real GCS, except to the extent Mechanism 8 is run.
- That signed URLs are correctly signed — the emulator performs no signature
  verification, so canonicalization bugs remain invisible.
- Anything about ACL or IAM enforcement, which the testbench does not implement.
- Behavior under multi-process operation, which is an explicit non-goal.
- Performance parity with real GCS; only internal scaling behavior is bounded.

## Risks

| Area | Risk | Mitigation |
|---|---|---|
| `Media` seam audit (~40 call sites) | Silent behavior change in a fault-injection or checksum path | Golden-master diff must be empty at phase 3 (Mechanism 2); coverage-gated call-site checklist (Mechanism 3); `BytesMedia` keeps the default path identical |
| Goldens captured from a moving target | Rebasing on upstream `main` invalidates the configuration-A baseline | Goldens are committed and versioned; re-derive and review the diff as part of any upstream rebase |
| `crc32c` incremental API | Assumption unverified; package not installed locally | Verify in phase 1; manual chaining fallback |
| macOS case-insensitivity | Silent data loss | Fail loudly on collision; recommend named volumes |
| Lock held across GB-scale copies | Emulator stalls under parallel tests | Immutable staging files; bulk copy outside the lock |
| gRPC thread starvation | Random test timeouts under a shared container | `TESTBENCH_GRPC_THREADS`, default 32 |
| Path traversal via caller-controlled object names | Writes outside the root on an unauthenticated service | `realpath` containment check, `O_NOFOLLOW`, overflow store for `.`/`..`, bucket-name validation, constrained `rmtree` |
| Upstream divergence | Painful rebases | Opt-in seams, no new dependencies, default behavior unchanged |

## Phasing

Phases 2 and 3 are pure refactors that leave the tree green, so work can stop after any
phase without a half-migrated codebase.

1. `flake.nix`; confirm the existing suite is green; verify the `crc32c` incremental
   API assumption; **build the black-box conformance harness and capture the
   configuration-A goldens against pristine `main`**. The goldens must be captured
   before any production code changes, so this phase is a hard prerequisite rather than
   setup.
2. `Store` seam plus `NullStore`. No behavior change; suite still green.
3. `Media` seam plus `BytesMedia`. The call-site audit; suite still green. Highest-risk
   phase, deliberately isolated.
4. `FileStore`: layout, escaping, overflow store, startup scan, collision detection,
   and the path-containment rules from "Security: path handling", with traversal tests
   written alongside. Metadata persistence for small objects.
5. `FileMedia`: mmap reads, append uploads, rolling checksums, streaming compose,
   rewrite, and gzip.
6. Bootstrap: `TESTBENCH_BUCKETS`, `TESTBENCH_GRPC_PORT`, `TESTBENCH_GRPC_THREADS`,
   single-worker assertion, compose files.
7. Durability, crash, concurrency, and large-object suites; optional real-GCS
   divergence report. See "Per-phase gates" for what must be green at each step.

## Decisions recorded

- Media residency: lazy, file-backed. The dev dataset mixes small files with several
  multi-GB files, so memory residency is not an option.
- Test isolation: shared container, UUID bucket per test, file backend on tmpfs.
- Disk layout: browsable and GCS-shaped, with sidecar metadata.
- Dev storage: Docker named volume, browsable via `docker compose exec`, not a host
  bind mount. Case-sensitivity and large-file throughput beat direct Finder access.
- Overflow store for unrepresentable object names: **kept**.
- Resumable-upload durability across restart: **dropped**.
- Startup adoption of hand-dropped orphan files: **dropped**.
- Fork now, keep the upstreaming option open.
