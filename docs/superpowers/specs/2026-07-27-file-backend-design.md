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
4. **Validate bucket names against GCS bucket naming rules** (3–63 characters,
   lowercase alphanumerics, hyphens, underscores, dots) *before* they become directory
   names. These rules are far stricter than object-name rules, so a validated bucket
   name is always a safe single path segment and needs no escaping.
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

### Named volume versus host bind mount, and its cost to browsability

There is a genuine tension here. A named volume is case-sensitive and fast for
large-file I/O, but the tree is only reachable through
`docker compose exec gcs-dev ls /data` or `docker cp` — not directly in Finder. A host
bind mount (`./.gcs-data:/data`) is directly browsable, which is what motivated the
GCS-shaped layout in the first place, but on macOS it is case-insensitive, so the
startup collision check will reject two object names differing only in case, and
large-file I/O through the VirtioFS boundary is markedly slower.

The design defaults to a named volume, because correctness beats convenience and the
layout stays browsable through `exec`. Switching to a bind mount is a one-line compose
change if direct Finder access proves more valuable in practice; the collision check
means the failure mode is a loud startup error, not silent data loss.

## Nix flake

`flake.nix` provides a `devShell` containing:

- Python 3.12, matching the pinned Dockerfile base image;
- the pinned runtime dependencies from `setup.py` — `grpcio`, `grpcio-status`,
  `grpcio-tools`, `googleapis-common-protos`, `protobuf`, `flask`,
  `requests-toolbelt`, `scalpl`, `crc32c`, `gunicorn`, `waitress`, `Werkzeug`;
- `pytest`;
- `docker-client` and `docker-compose`. The daemon comes from Docker Desktop or
  Colima; nixpkgs' full `docker` package builds a daemon that is not useful on Darwin.

So `nix develop` yields a working `pytest` and `testbench_run.py` with no virtualenv.
The file backend itself adds **no new Python runtime dependencies** — stdlib `os`,
`mmap`, `hashlib`, `json`, plus the already-present `crc32c` and `protobuf` — which
preserves the upstreaming option.

## Testing strategy

- **The existing suite must pass unmodified.** It is the regression net proving that
  `NullStore` and `BytesMedia` are behavior-identical, and it is the main guardrail for
  the invasive `Media` seam.
- **Backend-parametrized conformance suite** run against both stores, holding the file
  backend to the in-memory backend's behavior: round-trip, generations, versioning,
  soft-delete, compose, rewrite, move, resumable upload resume, range reads.
- **Layout tests:** escaping round-trip including every overflow trigger, case
  collision detection, crash recovery (media without sidecar is invisible), restart
  durability, orphan and `.gcs` handling.
- **Large-object tests:** a sparse multi-GB upload and download asserting bounded RSS.
  This is the only thing that keeps the O(n²) checksum class of bug fixed.
- **Concurrency test:** parallel streaming transfers against one emulator, which would
  have caught the two-thread gRPC pool.

## Risks

| Area | Risk | Mitigation |
|---|---|---|
| `Media` seam audit (~40 call sites) | Silent behavior change in a fault-injection or checksum path | Existing suite unmodified; grep-driven audit; `BytesMedia` keeps the default path identical |
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
   API assumption.
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
7. Conformance, layout, large-object, and concurrency tests.

## Decisions recorded

- Media residency: lazy, file-backed. The dev dataset mixes small files with several
  multi-GB files, so memory residency is not an option.
- Test isolation: shared container, UUID bucket per test, file backend on tmpfs.
- Disk layout: browsable and GCS-shaped, with sidecar metadata.
- Overflow store for unrepresentable object names: **kept**.
- Resumable-upload durability across restart: **dropped**.
- Startup adoption of hand-dropped orphan files: **dropped**.
- Fork now, keep the upstreaming option open.
