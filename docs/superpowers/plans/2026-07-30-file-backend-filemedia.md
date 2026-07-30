# FileMedia (`Media` → real-file streaming backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Back the phase-3 `Media` interface with a real file (`FileMedia`) so the FILE backend handles multi-GB objects without ever materialising a whole buffer: mmap/pread-backed reads, `O_APPEND` staging for uploads, incremental rolling `crc32c`/`md5`, streaming compose/rewrite/gzip, and `finalize(dest) = os.replace(staging → dest)` (with a `link_into`/`seal` variant for the appendable growth path) into the `FileStore`-owned destination. Every `.to_bytes()`/`gzip.decompress`/whole-buffer escape hatch the phase-3 handoff enumerates is replaced with true streaming, while the MEMORY backend stays byte-identical (`BytesMedia` untouched, golden digest unmoved) and the FILE backend stays B ≡ C (the conformance allow-list holds exactly one entry: `create-bucket-traversal`).

**Architecture:** One new file-backend-only unit, `testbench/filemedia.py` (`FileMedia(Media)`), backs the full `BytesMedia` surface with a real file — pread reads, an `O_APPEND` staging fd, rolling `crc32c` seed-chain + streaming `hashlib.md5`, `chunks()` whose boundaries and empty-slice/trailing-empty asymmetry are byte-identical to `BytesMedia`, `reader()` returning a fresh real file object, explicit fd ownership (`close()` + `__del__`), and two promotion primitives: `finalize((dst_dir_fd, name))` for one-shot producers (contained cross-dir `os.replace`, closes staging) and `link_into((dst_dir_fd, name))`/`seal()` for the appendable growth path (contained `os.link` that keeps the `O_APPEND` fd live, then a terminal unlink of the staging name). Backend selection lives in exactly one typed place: a **media factory on the `Store` base** (`new_upload_media`/`new_staging_media`) — `NullStore` returns `BytesMedia` (memory stays byte-identical), `FileStore` returns a `FileMedia` opened under the phase-4 `O_NOFOLLOW` containment walk. The two construction choke-points in `gcs/object.py` (`Object.__init__` :91 and `Object.init` :129) widen their `isinstance(media, BytesMedia)` guard to a shared `Media` base type so a handed-in `FileMedia` survives instead of being re-wrapped into bytes. Producers (upload/compose/rewrite/move) are migrated one family per task to build staging `FileMedia` via `db.store.new_*` and stream source→staging via `chunks()`; `FileStore.object_inserted` promotes the staging file (`finalize` for finalized objects, `link_into` for an unfinalized appendable insert) instead of `to_bytes()`+`write_bytes_atomic`, and `FileStore.object_updated` seals the appendable staging on the finalize checkpoint — so the object is written to disk exactly once and appendable growth flows to a single inode. `gcs/` stays store-agnostic (imports `Media`, never `FileStore`; import graph acyclic). Every wire-shape-affecting change (REST `Content-Length` framing, gRPC read chunk boundaries, the trailing-empty asymmetry) leads with the exact B ≡ C observable it could move.

**Tech Stack:** Python 3.8–3.12, stdlib only (`os`, `mmap`, `hashlib`, `io`, `gzip`, `contextlib`) plus the already-present `crc32c`; the phase-1 conformance harness with its phase-4 `--store {memory,file}` byte-exact masked overlay; `hypothesis` and `coverage` as dev-only deps (`flake.nix`); `tracemalloc` + `resource`/`os` RSS sampling for the Mechanism-5 bounded-memory detector, run **in-process** against `FileStore`/`gcs.upload`/`gcs.object` (never against `RUSAGE_SELF` while the bytes are streamed by an out-of-process gunicorn worker).

## Global Constraints

- **Zero new runtime dependencies.** `setup.py` stays as-is. `FileMedia` uses only stdlib (`os`/`mmap`/`hashlib`/`io`/`gzip`) + the already-present `crc32c`. `hypothesis`/`coverage` are dev deps in `flake.nix` only; do NOT add anything to `setup.py` (no `psutil` — the bounded-memory detector reads `/proc/<pid>/status` on Linux and falls back to in-process `tracemalloc`/`resource`). Use `.venv/bin/python -m pytest`, `.venv/bin/isort`, `.venv/bin/black`, `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness` as the toolchain — **nothing is on the bare `PATH`**.
- **Python floor is 3.8.** Every new/edited file must parse under `ast.parse(feature_version=(3, 8))`. All fd-based primitives use APIs present since 3.3 (`os.open(..., dir_fd=)`, `os.replace(..., src_dir_fd=, dst_dir_fd=)`, `os.link(..., src_dir_fd=, dst_dir_fd=)`, `os.O_NOFOLLOW`, `os.O_APPEND`, `os.pread`, `mmap.mmap`). CI runs a 3.8–3.12 matrix.
- **The MEMORY backend must stay byte-identical.** `sha256(rest.json + grpc.json + faults.json) = 98fa2130d213b04478474c5918a6ba36e3e52838823189f4093a9161f72987a7` and `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness` (memory) must print `OK` for all three traces with an **EMPTY diff**. `FileMedia` is FILE-backend-only; `BytesMedia`'s observable behaviour must not move, and the `NullStore` media factory MUST return `BytesMedia`. Any `FileMedia` leakage onto the memory path moves the golden. (Note: the phase-3 handoff cites the older digest `8eda6110…`; the live golden after phase 4 is `98fa2130…`, which this plan pins.)
- **The FILE backend must remain B ≡ C byte-identical.** `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file` must print `OK` for all three traces via the byte-exact masked comparison, diverging **only** on the single allow-listed `create-bucket-traversal` label. REST `Content-Length`/framing (`framing.mode == "content-length"`, `content_length_matches_body == true`) and gRPC `ReadObject`/`BidiReadObject` chunk boundaries + the **trailing-empty asymmetry** (grpc_server.py:687 replay and :815 replay) must NOT move. `tests/conformance/allowlist.json` must still hold **exactly one** entry (`stale_allowlist_labels` fails the gate if `create-bucket-traversal` ever stops diverging); no boundary change may add a second entry unless explicitly justified as a reviewed wire-shape change.
- **`--regenerate` NEVER turns a red gate green.** A golden diff without a reviewed, intended behaviour change is a defect — diagnose, don't regenerate.
- **Mutation-check every guard clause.** After a guard passes, reintroduce the specific defect it guards and confirm the named test FAILS, then revert. **Defense-in-depth / equivalent-mutant carve-out** (from the phase-4 plan): a clause provably subsumed by a stricter downstream clause is exempt from individual-killability PROVIDED (a) the load-bearing clause it defers to *is* mutation-killed by a test, and (b) the redundant clause is documented in code as intentional defense-in-depth. Deleting a security/correctness backstop to make a metric go green is the wrong trade; documenting the equivalence honestly is the right one.
- **Nothing in `tests/conformance/` may import `gcs`/`testbench` internals except `emulator.py`.** `FileMedia` and all its unit/parity/bounds tests live in `tests/` (top level) or `testbench/`, never under `tests/conformance/`.
- **Interpreter/OS/library-internals hazard.** `mmap` of a **zero-length** file raises `ValueError`; every `FileMedia` read path (`__getitem__`, `chunks`, `reader`) must special-case `len == 0`. `os.replace`/`os.link` require staging and destination on the **same filesystem** — `.gcs/uploads` and the natural/generations/overflow destinations are all under one bucket root, so this holds; a cross-device bucket root raises `OSError(EXDEV)` rather than silently degrading `finalize`/`link_into` to a copy (assert/document `st_dev` equality). Rolling `crc32c` seed-chaining (`crc32c.crc32c(data, seed)`) was pinned in Plan 1 (`tests/test_crc32c_assumptions.py`); `hashlib` is natively incremental. **`resource.getrusage(...).ru_maxrss` is bytes on macOS and KiB on Linux** — the bounded-memory detector must normalise the unit, AND must sample the process that actually streams the bytes (in-process driver, or the gunicorn worker pid via `/proc`), never `RUSAGE_SELF` while the worker is an out-of-process grandchild of the emulator subprocess. `make verify-linux` reproduces the Linux gate (gzip OS-byte / arch-sensitive crc32c); treat CI as authoritative.
- **Formatting is `isort` then `black`, in that order** (`isort==5.12.0`, `black==22.3.0`), run from `.venv/bin`.
- **Single gunicorn worker, `--reload` on.** Never edit a `.py` file while a harness run or emulator-backed test is in flight; it restarts the worker mid-trace and wipes state.
- **`TESTBENCH_FSYNC` stays off by default.** `finalize`/`link_into`/`seal`/`append` do not fsync; `os.replace`/`os.link` ordering is the durability guarantee. Keeping the no-fsync default preserves B ≡ C timing/behaviour.

### The golden digests (irreproducible baselines)

- MEMORY golden digest (unchanged by every task): `98fa2130d213b04478474c5918a6ba36e3e52838823189f4093a9161f72987a7`.
- FILE backend: B ≡ C via the byte-exact masked overlay, allow-list held at exactly one entry (`create-bucket-traversal`), on every task from Task 4 onward.

### The phase-5 exit gate (what "done" means)

Phase 5 is green when **all** hold:

1. `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness` (memory) prints `OK` for all three traces with an EMPTY diff (digest `98fa2130…`).
2. `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file` prints `OK` via the byte-exact masked comparison, diverging only on the one allow-listed `create-bucket-traversal` label.
3. `PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py` is green under both `TESTBENCH_TEST_STORE=memory` and `TESTBENCH_TEST_STORE=file`.
4. `PYTHONPATH=. .venv/bin/python -m pytest tests/test_media_call_sites.py` (subprocess-coverage call-site gate) is green; every new memory-reachable `.media` line is in `tests/media_call_sites.txt` and every FileMedia-only site is annotated `# UNCOVERED … <reason>`.
5. The `FileMedia` parity/adversarial/bounds suites pass: `tests/test_filemedia.py`, `tests/test_filemedia_staging.py`, `tests/test_filemedia_bounds.py`, `tests/test_appendable_filemedia.py`, `tests/test_filemedia_restart.py`; each new guard clause mutation-checked (with the documented carve-out).
6. Mechanism 5: bounded-memory (4GB up + 4GB down, peak RSS < baseline + 256 MiB) and the linear-time detector (`t(2N)/t(N) < 3`, `N = 256 MiB` in CI) are green, measured against the streaming process (in-process driver, or worker pid).
7. No large-object path retains a `.to_bytes()`/`gzip.decompress`/whole-buffer escape (Task 12 sweep); the only surviving materialisers are the small fault-injection-only paths (`corrupt_media`, `return-broken-stream`, `stall-*`, `return-corrupted-data`, `return-503-after-256K`), each documented.
8. `nix develop --command make verify-linux` is green on both legs; `setup.py` unchanged; nothing in `tests/conformance/` imports internals except `emulator.py`.

---

## File Structure

- **Create `testbench/filemedia.py`** — FILE-backend-only `FileMedia(Media)`: staging-fd-backed writes, pread reads, `O_APPEND` append with incremental rolling `crc32c`/`md5`, `chunks()`/`reader()` byte-identical to `BytesMedia` (zero-length special-cased), explicit fd ownership (`close()` idempotent + `__del__`), `is_finalized` property, `finalize((dst_dir_fd, name))` (one-shot promote via `containment.promote`), `link_into((dst_dir_fd, name))` + `seal()` (appendable hardlink-then-unlink via `containment.hardlink`), `from_existing(dir_fd, name, *, size, crc32c_value, md5_value)` for hydration, and the materialising escape hatches (`__add__`/`__radd__`/`__eq__`/`to_bytes`/`__getitem__`, `__hash__ = None`) kept working as explicit last-resort shims that streaming paths never route through. Imports only stdlib `os`/`mmap`/`hashlib`/`io` + `crc32c` + `testbench.containment` + `testbench.media` (for the `Media` base). Kept out of `media.py` so `os`/`mmap` machinery never leaks into the `BytesMedia` module.
- **Modify `testbench/media.py`** (:35-105) — add a thin `Media` base class (the shared duck-type anchor the two construction choke-points widen to); make `BytesMedia(Media)` with its observable behaviour UNCHANGED. This is the only edit that could move the memory golden, so it stays a pure type-widening no-op.
- **Modify `testbench/containment.py`** — add `promote(src_dir_fd, src_name, dst_dir_fd, dst_name)` (contained cross-dir `os.replace`, the one-shot `finalize` primitive), `hardlink(src_dir_fd, src_name, dst_dir_fd, dst_name)` (contained cross-dir `os.link`, the appendable `link_into` primitive), `unlink_at(dir_fd, name)` (contained single-component unlink for `seal`/staging cleanup), and `open_staging(dir_fd, name)` (`O_CREAT|O_RDWR|O_APPEND|O_NOFOLLOW` under containment). Fd-based only; no name/media logic.
- **Modify `testbench/store.py`** (base `Store` :95-169, `NullStore` :172-174) — add the media factory `new_upload_media(self, bucket_name, upload_id)`, `new_staging_media(self, bucket_name, token)` returning `BytesMedia` on the base (so `NullStore` keeps the memory backend byte-identical). Extend the trust-boundary docstring. (No `new_media`: every producer needs a bucket, so the two bucket-scoped factories are the whole surface — no dead method.)
- **Modify `testbench/filestore.py`** (`object_inserted` :134-148, `object_updated` :166-175, `_read_media` :347-351, `_hydrate_live`/`_hydrate_soft_deleted` :353-380) — override the media factory to return a `FileMedia` staged under `.gcs/uploads` via a contained dir_fd; branch `object_inserted` into three cases (unfinalized appendable `FileMedia` → `link_into`; finalized `FileMedia` → `finalize`; `BytesMedia` → `to_bytes()`+`write_bytes_atomic` fallback, no double-write); grow `object_updated` a `seal()` branch for the appendable finalize checkpoint; hydrate as read-only `FileMedia.from_existing` instead of whole-file reads; add `delete_upload`/`delete_rewrite`/cancel staging cleanup. Keep sidecar/collision/inode-detector logic as-is.
- **Modify `gcs/object.py`** — widen the two `isinstance(media, BytesMedia)` guards (`__init__` :91, `init` :129) to `isinstance(media, Media)` (import `Media`); rewrite `_download_range` to return `(begin, end, length)` arithmetically (no payload slicing); restructure `rest_media` (:518-692) so the normal + ranged download path streams via `_stream_media(self.media, max(0, begin), end)` with an arithmetic `Content-Length = end - max(0, begin)` and never materialises, while the fault-injection/instruction streamers materialise a small `response_payload` buffer via `to_bytes()` for **any** `Media` (widened guard); stream the decompressive transcode over `media.reader()` with a two-pass counted `Content-Length`.
- **Modify `gcs/upload.py`** (`Upload.init` :51-62, `_insert_empty_appendable_object` :299-310, bidi flush/alias :589-620, `finalize_blob` :656-670, non-appendable finalize :679-687) — import `Media`; accept a caller-supplied `Media`; replace `.to_bytes()` finalize args with pass-through; generalise the `isinstance(BytesMedia)` alias guards to `isinstance(_, Media)`; keep the classmethods store-agnostic (callers set `.media = db.store.new_upload_media(...)`).
- **Modify `gcs/rewrite.py`** (`init_rest` :71, `init_grpc` :124) — same: the caller supplies the staging `Media` via the store factory after `init`.
- **Modify `testbench/grpc_server.py`** (Compose :521/:551, MoveObject :1095-1096, WriteObject :1129, RewriteObject :1189-1219) — build staging via `db.store.new_*`; replace `.to_bytes()` appends with chunked source→staging streaming; MoveObject/copy default to a chunked copy (hardlink fast-path deferred).
- **Modify `testbench/rest_server.py`** (compose F1 :710-739, copy/move F1 :786-789, rewrite F1 :858-879, resumable append/finalize :1179-1185/:1292-1304) — migrate the `b"" += media` / slice idioms to explicit chunked streaming into a store-provided staging `FileMedia`; keep `Content-Length` set.
- **Create `tests/test_filemedia.py`** — read-core parity (len/getitem/chunks boundary parity incl. begin>0 mid-offset, empty-slice + trailing-empty, reader freshness, `to_bytes`/`__add__`/`__radd__`/`__eq__` compat, zero-length mmap, checksum equality, fd release) vs `BytesMedia`.
- **Create `tests/test_filemedia_staging.py`** — `O_APPEND` staging append, incremental rolling `crc32c`/`md5` == whole-buffer, `finalize` os.replace across dir_fds, `link_into`/`seal` hardlink lifecycle, `containment.promote`/`hardlink` symlink-swap rejection, staging cleanup, linear-time append.
- **Create `tests/test_appendable_filemedia.py`** — the trace-UNCOVERED F2 appendable round-trip (upload/flush across ≥2 checkpoints/finalize/read-back bytes+crc32c+md5), `object_updated` seal, and restart+hydrate.
- **Create `tests/test_filemedia_restart.py`** — hydration/restart round-trip proving startup does not materialise a large object (in-process, bounded RSS).
- **Create `tests/test_filemedia_bounds.py`** — Mechanism 5: 4GB up+down peak RSS ceiling and the linear-time detector, driven **in-process** against `FileStore`/`gcs.upload`/`gcs.object`.
- **Create `tests/test_filemedia_faults.py`** — dedicated tests for the B ≡ C-invisible media fault paths (broken-stream on a FileMedia read, `corrupt_media`/`inject-upload-data-error` over FileMedia, resumable `*/N` finalize, non-Content-Range simple-upload completion, `return-503` retry-success `flask.Response(bytes)`).
- **Create `tests/test_filemedia_download.py`** — REST download parity: forward-partial-overflow (`bytes=40-100` on a 43-byte object), suffix-overflow (`bytes=-N`, `N>length`), and ranged large-object bounded-RSS, asserting byte-identical `Content-Length` + `Content-Range` vs the pre-refactor slicing code (the trace is blind to overflow ranges).
- **Modify `tests/media_call_sites.txt`** — re-pin new active `path:line` sites per migration task; annotate FileMedia-only sites `# UNCOVERED … <reason>`.

Each task ends with the safety gate below. **Enabling/core tasks (1–3):** memory harness EMPTY diff; file harness B ≡ C (FileMedia not yet on any server path). **Migration tasks (4–12):** memory harness EMPTY diff AND file harness B ≡ C (allow-list at one entry) AND the media call-site coverage gate green.

---

### Task 1: `Media` base type + widen the construction choke-points + `Store` media factory (zero-diff, memory- and file-neutral)

**Files:**
- Modify: `testbench/media.py` (module head :35, `class BytesMedia` :35, `finalize` :99-102)
- Modify: `gcs/object.py` (`Object.__init__` :91, `Object.init` :129)
- Modify: `testbench/store.py` (base `Store` before `cleared` :168, `NullStore` :172-174)
- Test: `tests/test_media_base.py` (new), extend `tests/test_store.py`

**Interfaces:**
- Produces: `class Media` (empty base in `media.py`); `class BytesMedia(Media)` behaviourally unchanged. `Object.__init__`/`Object.init` widen `media if isinstance(media, BytesMedia) else BytesMedia(media)` → `media if isinstance(media, Media) else BytesMedia(media)`, so a pre-built `Media` (any backend) passes through by identity and only raw `bytes`/`bytearray` get wrapped. `Store.new_upload_media(self, bucket_name, upload_id) -> Media`, `Store.new_staging_media(self, bucket_name, token) -> Media`, both returning `BytesMedia(b"")` on the base (`NullStore` inherits).

- [ ] **Step 1: Write the failing base-type + factory test** — the RED must exercise the *behavioural* widening (a non-`BytesMedia` `Media` passing through by identity), not just the import.

```python
# tests/test_media_base.py
import unittest

import gcs.object
from testbench.media import BytesMedia, Media
from testbench.store import NullStore
# reuse _meta/_bucket helpers from tests/test_object.py (import or inline minimal protos)


class _OtherMedia(Media):
    """A non-BytesMedia Media so the identity-passthrough behaviour is driven by a
    failing test, not only by the Step-6 mutation check."""

    def to_bytes(self):
        return b""


class TestMediaBase(unittest.TestCase):
    def test_bytesmedia_is_a_media(self):
        self.assertIsInstance(BytesMedia(b"x"), Media)

    def test_init_keeps_a_prebuilt_non_bytes_media_by_identity(self):
        # The load-bearing behaviour: a Media that is NOT a BytesMedia must NOT be
        # re-wrapped (re-wrapping would materialise a FileMedia into bytes). This
        # test FAILS under the old `isinstance(media, BytesMedia)` guard.
        m = _OtherMedia()
        obj, _ = gcs.object.Object.init(_meta("o"), m, _bucket(), False, None)
        self.assertIs(obj.media, m)

    def test_init_wraps_raw_bytes(self):
        obj = gcs.object.Object(_meta("o"), b"hello", _bucket())
        self.assertIsInstance(obj.media, BytesMedia)


class TestStoreMediaFactory(unittest.TestCase):
    def test_nullstore_factory_returns_bytesmedia(self):
        s = NullStore()
        self.assertIsInstance(s.new_upload_media("projects/_/buckets/b", "u"), BytesMedia)
        self.assertIsInstance(s.new_staging_media("projects/_/buckets/b", "t"), BytesMedia)
```

(Match the real `Object.init` signature when wiring `_meta`/`_bucket`; the point is that `_OtherMedia()` reaches `init` and survives by identity.)

- [ ] **Step 2: Run to see it fail** — `PYTHONPATH=. .venv/bin/python -m pytest tests/test_media_base.py -q` → FAIL: `ImportError: cannot import name 'Media'`, then (after the base exists) `test_init_keeps_a_prebuilt_non_bytes_media_by_identity` FAILS on the un-widened guard.

- [ ] **Step 3: Add the base type, widen the guards, add the factory**

```python
# testbench/media.py -- above BytesMedia:
class Media:
    """Shared base for the two media backends. Exists only so the construction
    choke-points (gcs/object.py) can widen their isinstance guard without gcs/
    importing any file-backend code. BytesMedia is the memory impl; FileMedia
    (this plan) is the FILE-backend impl. Defines no behaviour: the interface is
    the duck surface (__len__/__getitem__/append/chunks/reader/crc32c/md5/
    finalize/to_bytes) both subclasses implement."""


class BytesMedia(Media):   # was: class BytesMedia:
    ...                    # body UNCHANGED
```

```python
# gcs/object.py -- __init__ (:91) and init (:129), identical change:
#   self.media = media if isinstance(media, BytesMedia) else BytesMedia(media)
# ->
        self.media = media if isinstance(media, Media) else BytesMedia(media)
# and in init:
        media = media if isinstance(media, Media) else BytesMedia(media)
```
Import `Media` alongside `BytesMedia` at the top of `gcs/object.py` (it already imports `from testbench.media import BytesMedia`).

```python
# testbench/store.py -- on Store, before cleared():
    def new_upload_media(self, bucket_name, upload_id):
        """Construct the Media an Upload accumulates into. bucket_name is
        proto-form. Base returns BytesMedia so the memory backend is
        byte-identical; FileStore opens an O_APPEND staging file under
        <bucket>/.gcs/uploads/<upload_id> through containment."""
        from testbench.media import BytesMedia

        return BytesMedia(b"")

    def new_staging_media(self, bucket_name, token):
        """Construct a staging Media for compose/rewrite/move destinations.
        Base returns BytesMedia; FileStore stages under .gcs/uploads/<token>."""
        from testbench.media import BytesMedia

        return BytesMedia(b"")
```
(Lazy `import` inside the methods keeps `store.py`'s import graph free of `media.py` at module load, matching the existing lazy-`FileStore` pattern in `database.py`.)

- [ ] **Step 4: Run to green** — `PYTHONPATH=. .venv/bin/python -m pytest tests/test_media_base.py tests/test_store.py tests/test_object.py -q` → PASS.

- [ ] **Step 5: Both harness legs (must be behaviour-preserving)**

```bash
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness              # memory: OK, EMPTY diff, digest 98fa2130…
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file # file: OK, one allow-list entry
```
Both pass: raw `bytes` still wrap into `BytesMedia` identically; the factory is unused by any caller yet; `NullStore` and `FileStore` still produce `BytesMedia` everywhere.

- [ ] **Step 6: Mutation-check the widened guard**
- In `Object.init`, revert the guard to `isinstance(media, BytesMedia)`. Expected: `test_init_keeps_a_prebuilt_non_bytes_media_by_identity` FAILS (the `_OtherMedia` gets re-wrapped into `BytesMedia`). Revert. (The `BytesMedia`-passes-through sub-case is an equivalent mutant under the old guard; the load-bearing behaviour — non-`BytesMedia` `Media` passes through — is killed by the `_OtherMedia` test.)
- In `NullStore.new_upload_media`, return `object()` instead of `BytesMedia(b"")`. Expected: `test_nullstore_factory_returns_bytesmedia` FAILS. Revert.

- [ ] **Step 7: 3.8 parse, format, commit**

```bash
PYTHONPATH=. .venv/bin/python -c "import ast; [ast.parse(open(f).read(), feature_version=(3,8)) for f in ('testbench/media.py','gcs/object.py','testbench/store.py')]"
.venv/bin/isort testbench/media.py gcs/object.py testbench/store.py tests/test_media_base.py && .venv/bin/black testbench/media.py gcs/object.py testbench/store.py tests/test_media_base.py
git add testbench/media.py gcs/object.py testbench/store.py tests/test_media_base.py tests/test_store.py
git commit -m "refactor(media): add Media base, widen construction guards, add Store media factory (zero-diff)"
```

**Safety gate:** memory harness EMPTY diff (digest `98fa2130…`); file harness B ≡ C (one allow-list entry); factory returns `BytesMedia` on both stores so nothing observable moves; the widened guard is mutation-checked with a genuine `_OtherMedia` RED and the documented `BytesMedia`-passthrough equivalent-mutant carve-out.

---

### Task 2: `FileMedia` read/measure core (pread) + fd ownership, proven parity vs `BytesMedia`, unwired

**Files:**
- Create: `testbench/filemedia.py` (read side)
- Test: `tests/test_filemedia.py`

**Interfaces:**
- Produces: `FileMedia(Media)` read-only surface: `__len__` (O(1) via cached size), `__getitem__` (int→int, slice→`bytes` via `os.pread`, zero-length special-cased), `chunks(begin, end, size)` (generator yielding `bytes`, **nothing for an empty `[begin, end)` slice**, boundaries stepped by `size` exactly like `BytesMedia.chunks`), `reader()` (a fresh real `io.BufferedReader`/`io.BytesIO(b"")` for empty), `crc32c()`/`md5()` (from rolling state), `to_bytes()`/`__eq__`/`__add__`/`__radd__` (materialising compat shims), `__hash__ = None`, `is_finalized` property (`self._staging is None`), explicit `close()` (idempotent) + `__del__`. A `from_existing(dir_fd, name, *, size, crc32c_value, md5_value)` classmethod (used by hydration in Task 11) trusts persisted checksums and never reads the whole file. Read-only instances hold an `O_RDONLY|O_NOFOLLOW` fd to the backing inode.

- [ ] **Step 1: Write failing parity tests (read side, incl. mid-offset begin>0, empty-slice, trailing-empty, zero-length, fd release)**

```python
# tests/test_filemedia.py
import gc
import io
import os
import tempfile
import unittest

from hypothesis import given, strategies as st

from testbench.filemedia import FileMedia
from testbench.media import BytesMedia


def _file_media(data):
    d = tempfile.mkdtemp()
    fd = os.open(d, os.O_RDONLY)
    with open(os.path.join(d, "m"), "wb") as fh:
        fh.write(data)
    fm = FileMedia.from_path(fd, "m")   # test-only convenience ctor over a dir_fd
    os.close(fd)
    return fm


class TestFileMediaReadParity(unittest.TestCase):
    @given(st.binary(max_size=4096), st.integers(1, 512))
    def test_chunks_boundaries_match_bytesmedia_from_zero(self, data, size):
        fm, bm = _file_media(data), BytesMedia(data)
        self.assertEqual(list(bm.chunks(0, len(data), size)),
                         list(fm.chunks(0, len(data), size)))

    @given(st.binary(min_size=1, max_size=4096), st.integers(1, 512),
           st.integers(0, 4096), st.integers(0, 4096))
    def test_chunks_boundaries_match_bytesmedia_midoffset(self, data, size, a, b):
        begin, end = min(a, b) % (len(data) + 1), max(a, b) % (len(data) + 1)
        fm, bm = _file_media(data), BytesMedia(data)
        self.assertEqual(list(bm.chunks(begin, end, size)),
                         list(fm.chunks(begin, end, size)))

    def test_midoffset_pinned_case(self):
        fm, bm = _file_media(b"0123456789abcdefghij" * 3), BytesMedia(b"0123456789abcdefghij" * 3)
        self.assertEqual(list(bm.chunks(10, 43, 16)), list(fm.chunks(10, 43, 16)))

    def test_empty_slice_yields_nothing(self):
        for data in (b"", b"abc"):
            fm = _file_media(data)
            self.assertEqual([], list(fm.chunks(0, 0, 4)))
            self.assertEqual([], list(fm.chunks(2, 2, 4)))

    def test_zero_length_reads_do_not_crash(self):
        fm = _file_media(b"")
        self.assertEqual(0, len(fm))
        self.assertEqual(b"", fm[0:0])
        self.assertEqual(b"", fm.reader().read())
        self.assertEqual([], list(fm.chunks(0, 0, 8)))

    def test_getitem_int_and_slice_match_bytes(self):
        fm = _file_media(b"hello world")
        self.assertEqual(ord("h"), fm[0])
        self.assertEqual(b"ello", fm[1:5])
        self.assertEqual(b"world", fm[-5:])

    def test_reader_is_fresh_each_call(self):
        fm = _file_media(b"abcdef")
        r1, r2 = fm.reader(), fm.reader()
        self.assertEqual(b"abc", r1.read(3))
        self.assertEqual(b"abcdef", r2.read())

    @given(st.binary(max_size=8192))
    def test_checksums_equal_whole_buffer(self, data):
        fm, bm = _file_media(data), BytesMedia(data)
        self.assertEqual(bm.crc32c(), fm.crc32c())
        self.assertEqual(bm.md5(), fm.md5())

    def test_eq_and_to_bytes_compat(self):
        fm = _file_media(b"xyz")
        self.assertEqual(b"xyz", fm.to_bytes())
        self.assertTrue(fm == b"xyz")
        self.assertEqual(b"pre" + b"xyz", b"pre" + fm)

    def test_close_is_idempotent_and_releases_fd(self):
        fm = _file_media(b"data")
        rfd = fm._read_fd
        fm.close()
        fm.close()  # idempotent
        with self.assertRaises(OSError):
            os.fstat(rfd)  # fd closed
```

- [ ] **Step 2: Run to see it fail** — `ModuleNotFoundError: No module named 'testbench.filemedia'`.

- [ ] **Step 3: Implement the read side of `testbench/filemedia.py`** (incl. lifecycle used by every later task)

```python
"""FileMedia: back the Media interface with a real file so the FILE backend
handles multi-GB objects without materialising a buffer. pread reads, O_APPEND
staging writes with incremental rolling crc32c/md5, and finalize/link_into via a
contained os.replace/os.link. The bytes-compat shims (to_bytes/__add__/__radd__/
__eq__/__getitem__) are kept working for the legacy call sites but the streaming
paths never route through them. FILE-backend-only: BytesMedia stays the memory
backend so the memory golden never moves."""

import hashlib
import io
import os

import crc32c

from testbench import containment
from testbench.media import Media

READ_CHUNK = 1024 * 1024  # streamed checksum/parity pass size (not golden-pinned)


class FileMedia(Media):
    def __init__(self, read_fd, size, crc, md5_digest):
        # read_fd: an O_RDONLY|O_NOFOLLOW fd to the backing inode (survives an
        # os.replace/os.link of its name). size/crc/md5_digest are rolling state.
        self._read_fd = read_fd
        self._size = size
        self._crc = crc                 # int, crc32c seed-chain accumulator
        self._md5 = md5_digest          # bytes (frozen digest) or a live hashlib.md5
        self._staging = None            # (staging_dir_fd_dup, name, append_fd) or None
        self._closed = False

    # --- lifecycle -------------------------------------------------------
    @property
    def is_finalized(self):
        # True once the staging file has been promoted (finalize) or sealed
        # (appendable). Read-only / hydrated instances are always "finalized".
        return self._staging is None

    def close(self):
        # Idempotent. Releases the read fd and any staging fds (append_fd + the
        # dup'd staging dir_fd). reader()'s dup'd fd is owned by its BufferedReader
        # (closefd=True) and is not touched here.
        if self._closed:
            return
        self._closed = True
        if self._staging is not None:
            sdir, _, afd = self._staging
            for fd in (afd, sdir):
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._staging = None
        try:
            os.close(self._read_fd)
        except OSError:
            pass
        self._read_fd = -1

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # --- measurement -----------------------------------------------------
    def __len__(self):
        return self._size

    # --- reads (pread; zero-length special-cased) ------------------------
    def _pread(self, begin, end):
        if end <= begin or self._size == 0:
            return b""
        length = min(end, self._size) - begin
        return os.pread(self._read_fd, length, begin)

    def __getitem__(self, key):
        if isinstance(key, slice):
            begin, end, step = key.indices(self._size)
            assert step == 1, "FileMedia slicing is contiguous only"
            return self._pread(begin, end)
        idx = key if key >= 0 else self._size + key
        return self._pread(idx, idx + 1)[0]

    def chunks(self, begin, end, size):
        pos = begin
        while pos < end:                      # empty slice -> zero iterations
            stop = min(pos + size, end)
            yield self._pread(pos, stop)
            pos = stop

    def reader(self):
        if self._size == 0:
            return io.BytesIO(b"")
        dup = os.dup(self._read_fd)
        os.lseek(dup, 0, os.SEEK_SET)
        return io.BufferedReader(io.FileIO(dup, "rb", closefd=True))

    # --- checksums (rolling) --------------------------------------------
    def crc32c(self):
        return self._crc

    def md5(self):
        return self._md5 if isinstance(self._md5, bytes) else self._md5.digest()

    # --- materialising compat shims (small/legacy callers only) ----------
    def to_bytes(self):
        return self._pread(0, self._size)

    def __add__(self, data):
        return self.to_bytes() + data

    def __radd__(self, data):
        return data + self.to_bytes()

    def __eq__(self, other):
        if isinstance(other, Media):
            return self.to_bytes() == other.to_bytes()
        if isinstance(other, (bytes, bytearray)):
            return self.to_bytes() == other
        return NotImplemented

    __hash__ = None

    # --- constructors ----------------------------------------------------
    @classmethod
    def _from_open_fd(cls, read_fd):
        size = os.fstat(read_fd).st_size
        crc, md5 = 0, hashlib.md5()
        off = 0
        while off < size:                 # single streamed pass -- bounded, linear
            buf = os.pread(read_fd, min(READ_CHUNK, size - off), off)
            crc = crc32c.crc32c(buf, crc)
            md5.update(buf)
            off += len(buf)
        return cls(read_fd, size, crc, md5.digest())

    @classmethod
    def from_existing(cls, dir_fd, name, *, size, crc32c_value, md5_value):
        # Hydration: trust the persisted sidecar checksums; do NOT re-read the
        # whole file at startup (bounded memory on a multi-GB tree).
        fd = containment.safe_open(dir_fd, name, os.O_RDONLY)
        return cls(fd, size, crc32c_value, md5_value)

    @classmethod
    def from_path(cls, dir_fd, name):
        # Test/convenience: open an existing file and compute checksums once.
        fd = containment.safe_open(dir_fd, name, os.O_RDONLY)
        return cls._from_open_fd(fd)
```

(`new_staging`/`append`/`finalize`/`link_into`/`seal` land in Task 3. `assert step == 1` guards a non-contiguous slice no call site produces; documented.)

- [ ] **Step 4: Run to green** — `PYTHONPATH=. .venv/bin/python -m pytest tests/test_filemedia.py -q` → PASS.

- [ ] **Step 5: Mutation-check the read guards**
- In `chunks`, change `while pos < end` to `while pos <= end`. Expected: `test_empty_slice_yields_nothing` FAILS (empty slice now yields one `b""`), and `test_chunks_boundaries_match_bytesmedia_midoffset`/`_from_zero` FAIL on exact-multiple sizes. Revert. **This is the load-bearing gRPC trailing-empty guard** (grpc_server.py:687, :815 replay the trailing empty response assuming `chunks()` yields nothing at `end`).
- In `_pread`, drop the `self._size == 0` clause. Expected: `test_zero_length_reads_do_not_crash` still passes (guarded by `end <= begin`), so this clause is an **equivalent mutant** — document it as defense-in-depth against a caller passing `end > 0` on an empty file. Keep it.
- In `_from_open_fd`, seed `crc = crc32c.crc32c(b"")` unconditionally per chunk instead of chaining. Expected: `test_checksums_equal_whole_buffer` FAILS on multi-chunk inputs. Revert.
- In `close`, make it non-idempotent (drop the `self._closed` early-return and double-close the read fd). Expected: `test_close_is_idempotent_and_releases_fd` FAILS (second `close()` raises on the already-closed fd). Revert.

- [ ] **Step 6: Harness legs unaffected (FileMedia unwired), 3.8 parse, format, commit**

```bash
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness              # memory EMPTY diff
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file # B==C, one entry
PYTHONPATH=. .venv/bin/python -c "import ast; ast.parse(open('testbench/filemedia.py').read(), feature_version=(3,8))"
.venv/bin/isort testbench/filemedia.py tests/test_filemedia.py && .venv/bin/black testbench/filemedia.py tests/test_filemedia.py
git add testbench/filemedia.py tests/test_filemedia.py
git commit -m "feat(filemedia): pread read core with BytesMedia parity (chunks/reader/checksums, zero-length, fd lifecycle)"
```

**Safety gate:** both harness legs unchanged (FileMedia on no server path yet). Read-core parity proven directly — `chunks()` boundary + mid-offset begin>0 + empty-slice + zero-length + checksum equality — so a boundary bug surfaces in a focused unit test, not as a moved gRPC golden. Fd ownership (`close`/`__del__`) defined and mutation-checked. Guards mutation-checked with the documented zero-length equivalent-mutant carve-out.

---

### Task 3: `FileMedia` staging/append/finalize + `link_into`/`seal` + `containment` primitives + linear-time detector

**Files:**
- Create: `testbench/filemedia.py` (write side — extend Task 2)
- Modify: `testbench/containment.py` (add `promote`, `hardlink`, `unlink_at`, `open_staging`)
- Test: `tests/test_filemedia_staging.py`, extend `tests/test_containment.py`

**Interfaces:**
- Produces: `FileMedia.new_staging(dir_fd, name)` classmethod — opens `name` under `dir_fd` with `containment.open_staging` and returns a writable `FileMedia` with `_size=0`, `_crc=0`, `_md5=hashlib.md5()`, `_staging=(os.dup(dir_fd), name, append_fd)`. **The dir_fd is dup'd here (once), in the method that defines the contract**, so the staging fd outlives the caller's `with … dfd` context; `close()`/`finalize`/`seal` release the dup. `append(data)` writes via the `O_APPEND` fd and rolls `_crc`/`_md5`/`_size` — never re-reads. `__iadd__` = `append`. `finalize((dst_dir_fd, dst_name))` (one-shot) promotes the staging file via `containment.promote` (contained `os.replace`), closes append_fd + staging dir_fd dup, freezes `_md5`, drops `_staging` (`is_finalized` → True); `_read_fd` stays valid (inode survives). `link_into((dst_dir_fd, dst_name))` (appendable) hardlinks staging→dest via `containment.hardlink` (contained `os.link`) and **keeps the append_fd/staging open** so subsequent appends flow to the shared inode now visible at the destination; records `_dest`. `seal()` (appendable terminal) closes append_fd, `containment.unlink_at`s the staging name (the destination hardlink and inode survive), freezes `_md5`, drops `_staging`. `containment.promote`/`hardlink` raise `ValueError` on a slash-bearing name (single-component guard) before touching the FS, and both are fully fd-relative so no symlink at any component redirects the operation.

- [ ] **Step 1: Write failing staging tests (append rolling parity, finalize, link_into/seal, containment guards split correctly, linear-time)**

```python
# tests/test_filemedia_staging.py
import os
import tempfile
import time
import unittest

from testbench import containment
from testbench.filemedia import FileMedia
from testbench.media import BytesMedia


class TestFileMediaStaging(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.dfd = os.open(self.root, os.O_RDONLY)
        self.addCleanup(os.close, self.dfd)
        os.mkdir(os.path.join(self.root, "dst"))
        self.dst_dfd = os.open(os.path.join(self.root, "dst"), os.O_RDONLY)
        self.addCleanup(os.close, self.dst_dfd)

    def test_append_rolls_checksums_incrementally(self):
        fm, bm = FileMedia.new_staging(self.dfd, "u"), BytesMedia(b"")
        for piece in (b"the ", b"quick ", b"brown ", b"fox"):
            fm.append(piece); bm.append(piece)
        self.assertEqual(len(bm), len(fm))
        self.assertEqual(bm.crc32c(), fm.crc32c())
        self.assertEqual(bm.md5(), fm.md5())
        self.assertEqual(bm.to_bytes(), fm.to_bytes())

    def test_finalize_replaces_into_destination_and_reads_survive(self):
        fm = FileMedia.new_staging(self.dfd, "u")
        fm.append(b"payload")
        fm.finalize((self.dst_dfd, "final"))
        self.assertTrue(fm.is_finalized)
        self.assertFalse(os.path.exists(os.path.join(self.root, "u")))     # staging gone
        self.assertEqual(b"payload", open(os.path.join(self.root, "dst", "final"), "rb").read())
        self.assertEqual(b"payload", fm.to_bytes())                        # read fd valid post-rename

    def test_link_into_then_append_then_seal(self):
        # Appendable lifecycle: link the (empty) staging into the destination,
        # keep appending -> the shared inode grows at the destination path, then
        # seal removes the staging name but leaves the destination hardlink.
        fm = FileMedia.new_staging(self.dfd, "u")
        fm.link_into((self.dst_dfd, "live"))
        self.assertFalse(fm.is_finalized)                                  # still staging
        fm.append(b"aaa"); fm.append(b"bbb")
        self.assertEqual(b"aaabbb", open(os.path.join(self.root, "dst", "live"), "rb").read())
        fm.seal()
        self.assertTrue(fm.is_finalized)
        self.assertFalse(os.path.exists(os.path.join(self.root, "u")))     # staging name gone
        self.assertEqual(b"aaabbb", open(os.path.join(self.root, "dst", "live"), "rb").read())
        self.assertEqual(b"aaabbb", fm.to_bytes())                         # read fd still valid

    def test_append_is_linear_time(self):
        def elapsed(n):
            fm = FileMedia.new_staging(self.dfd, "t%d" % n)
            chunk = b"a" * (1024 * 1024)
            t0 = time.perf_counter()
            for _ in range(n):
                fm.append(chunk)
            return time.perf_counter() - t0
        n = 64
        self.assertLess(elapsed(2 * n) / max(elapsed(n), 1e-6), 3.0)       # O(n), not O(n^2)
```

```python
# tests/test_containment.py -- add TWO tests (the single-component guard and the
# fd-containment property are separate concerns; do not conflate them under one
# assertRaises(OSError), because the guard raises ValueError, not OSError):
    def test_promote_rejects_slash_bearing_name(self):
        rfd = os.open(self.root, os.O_RDONLY)
        self.addCleanup(os.close, rfd)
        open(os.path.join(self.root, "src"), "wb").close()
        with self.assertRaises(ValueError):
            containment.promote(rfd, "src", rfd, "d/x")

    def test_promote_is_fd_contained_not_pathname(self):
        # Plant a symlink at a *single-component* destination name pointing
        # outside the dir. A pathname os.replace would clobber the target through
        # the symlink; the fd-relative os.replace replaces the symlink itself,
        # inside the dir, and never writes outside.
        outside = tempfile.mkdtemp()
        victim = os.path.join(outside, "victim")
        open(victim, "wb").write(b"KEEP")
        os.symlink(victim, os.path.join(self.root, "escape"))
        rfd = os.open(self.root, os.O_RDONLY)
        self.addCleanup(os.close, rfd)
        with open(os.path.join(self.root, "src"), "wb") as fh:
            fh.write(b"NEW")
        containment.promote(rfd, "src", rfd, "escape")     # single component, fd-relative
        self.assertEqual(b"KEEP", open(victim, "rb").read())    # symlink target untouched
        self.assertEqual(b"NEW", open(os.path.join(self.root, "escape"), "rb").read())  # symlink replaced in-dir
```

- [ ] **Step 2: Run to see it fail** — `AttributeError: type object 'FileMedia' has no attribute 'new_staging'` / `module 'testbench.containment' has no attribute 'promote'`.

- [ ] **Step 3: Implement the write side + containment primitives**

```python
# testbench/containment.py -- add:
def open_staging(dir_fd, name):
    """Open a single-component staging file O_CREAT|O_RDWR|O_APPEND|O_NOFOLLOW
    under dir_fd. O_NOFOLLOW refuses a symlink at the final component."""
    if "/" in name:
        raise ValueError("staging name %r is not a single component" % name)
    return os.open(name, os.O_CREAT | os.O_RDWR | os.O_APPEND | _O_NOFOLLOW, 0o644,
                   dir_fd=dir_fd)


def promote(src_dir_fd, src_name, dst_dir_fd, dst_name):
    """Contained cross-dir os.replace(staging -> dest). Both names are single
    components opened relative to their dir_fds, so a symlink at the destination
    name is replaced in-place inside the dir rather than followed (mirrors
    _move_quiet's fd discipline). Same-filesystem os.replace is O(1); a
    cross-device root raises OSError(EXDEV) rather than silently degrading."""
    if "/" in src_name or "/" in dst_name:
        raise ValueError("promote names must be single components")
    os.replace(src_name, dst_name, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)


def hardlink(src_dir_fd, src_name, dst_dir_fd, dst_name):
    """Contained cross-dir os.link(staging -> dest) for the appendable path: the
    destination becomes a second name for the staging inode, so O_APPEND writes
    to the staging fd are immediately visible at the destination. fd-relative and
    single-component, same containment guarantee as promote()."""
    if "/" in src_name or "/" in dst_name:
        raise ValueError("hardlink names must be single components")
    os.link(src_name, dst_name, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)


def unlink_at(dir_fd, name):
    """Contained single-component unlink (seal / staging cleanup)."""
    if "/" in name:
        raise ValueError("unlink name %r is not a single component" % name)
    os.unlink(name, dir_fd=dir_fd)
```

```python
# testbench/filemedia.py -- add to FileMedia:
    @classmethod
    def new_staging(cls, dir_fd, name):
        append_fd = containment.open_staging(dir_fd, name)
        read_fd = containment.safe_open(dir_fd, name, os.O_RDONLY)
        self = cls(read_fd, 0, 0, hashlib.md5())
        # dup the dir_fd so the staging tuple owns an fd that outlives the
        # caller's `with ... dfd` context. Released by finalize/seal/close.
        self._staging = (os.dup(dir_fd), name, append_fd)
        return self

    def append(self, data):
        if self._staging is None:
            raise RuntimeError("append on a non-staging FileMedia")
        _, _, append_fd = self._staging
        os.write(append_fd, data)              # O_APPEND -> always at EOF
        self._size += len(data)
        self._crc = crc32c.crc32c(data, self._crc)
        self._md5.update(data)

    def __iadd__(self, data):
        self.append(data)
        return self

    def finalize(self, dest):
        # One-shot promote: (dst_dir_fd, dst_name). Contained os.replace, then
        # release the staging fds. The inode survives, so _read_fd stays valid.
        if self._staging is None:
            return None                        # already finalized / read-only
        sdir, sname, append_fd = self._staging
        dst_dir_fd, dst_name = dest
        os.close(append_fd)
        containment.promote(sdir, sname, dst_dir_fd, dst_name)
        os.close(sdir)
        self._md5 = self._md5.digest()
        self._staging = None
        return None

    def link_into(self, dest):
        # Appendable: hardlink staging -> dest, KEEP staging open so appends
        # keep flowing to the shared inode (now visible at dest). Not finalized.
        if self._staging is None:
            raise RuntimeError("link_into on a non-staging FileMedia")
        sdir, sname, _ = self._staging
        dst_dir_fd, dst_name = dest
        containment.hardlink(sdir, sname, dst_dir_fd, dst_name)
        self._dest = dest
        return None

    def seal(self):
        # Appendable terminal: close the append fd, unlink the staging NAME (the
        # destination hardlink + inode survive), freeze md5, drop staging.
        if self._staging is None:
            return None
        sdir, sname, append_fd = self._staging
        os.close(append_fd)
        containment.unlink_at(sdir, sname)
        os.close(sdir)
        self._md5 = self._md5.digest()
        self._staging = None
        return None
```

Note the `Media` base still defines no `finalize`; `BytesMedia.finalize(dest)` stays the no-op at media.py:99-102. `object_inserted` branches on media type (Task 4) and only calls `finalize`/`link_into` on the `FileMedia` branch — the memory path never reaches them.

- [ ] **Step 4: Run to green** — `PYTHONPATH=. .venv/bin/python -m pytest tests/test_filemedia_staging.py tests/test_containment.py -q` → PASS.

- [ ] **Step 5: Mutation-check the write/containment guards**
- In `promote`, replace `os.replace(src_name, dst_name, src_dir_fd=, dst_dir_fd=)` with a raw-path `os.replace(os.path.join(root, src_name), os.path.join(root, dst_name))`. Expected: `test_promote_is_fd_contained_not_pathname` FAILS (a raw path follows the symlink and clobbers `victim`). Revert. **Load-bearing containment guard.**
- In `open_staging`, drop `| _O_NOFOLLOW`; plant a symlink at the staging name in a sub-test and confirm the write escapes → that sub-test FAILS. Revert.
- In `append`, change `crc32c.crc32c(data, self._crc)` to `crc32c.crc32c(data)` (drop the seed). Expected: `test_append_rolls_checksums_incrementally` FAILS. Revert.
- In `append`, recompute `self._crc` by re-reading the whole file each call. Expected: `test_append_is_linear_time` FAILS (ratio ≥ 3, O(n²)). Revert.
- In `link_into`, add `os.close(append_fd)` (break the "keep open" contract). Expected: `test_link_into_then_append_then_seal` FAILS on the post-link `append` (writes to a closed fd). Revert. This proves the appendable path keeps the staging fd live across `link_into`.

- [ ] **Step 6: Harness legs unaffected, 3.8 parse, format, commit**

```bash
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness && PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file
PYTHONPATH=. .venv/bin/python -c "import ast; [ast.parse(open(f).read(), feature_version=(3,8)) for f in ('testbench/filemedia.py','testbench/containment.py')]"
.venv/bin/isort testbench/filemedia.py testbench/containment.py tests/test_filemedia_staging.py tests/test_containment.py && .venv/bin/black testbench/filemedia.py testbench/containment.py tests/test_filemedia_staging.py tests/test_containment.py
git add testbench/filemedia.py testbench/containment.py tests/test_filemedia_staging.py tests/test_containment.py
git commit -m "feat(filemedia): O_APPEND staging, rolling checksums, contained finalize/link_into/seal"
```

**Safety gate:** both harness legs unchanged (FileMedia still unwired). Staging append rolls checksums incrementally (linear-time detector green); one-shot `finalize` promotes via a contained `os.replace` (mutation-checked against a raw-path regression); the appendable `link_into`→append→`seal` lifecycle keeps a single inode and a live append fd (mutation-checked). The risky pread/O_APPEND/os.replace/os.link/rolling-checksum core is fully proven in isolation before any server path routes onto it.

---

### Task 4: `FileStore` media factory + `object_inserted` finalize/link_into branches + staging cleanup (B ≡ C via fallback)

**Files:**
- Modify: `testbench/filestore.py` (`object_inserted` :134-148; add `new_upload_media`/`new_staging_media` overrides, `_uploads_dfd` helper, `delete_upload` cleanup)
- Test: `tests/test_filestore_filemedia.py` (new)

**Interfaces:**
- Produces: `FileStore.new_upload_media(bucket_name, upload_id)` / `new_staging_media(bucket_name, token)` → a `FileMedia.new_staging(uploads_dfd, id)` opened under `<bucket>/.gcs/uploads/` via the O_NOFOLLOW walk (the `FileMedia` dup's the dir_fd, so the `with` context closes cleanly). `object_inserted` branches THREE ways on `blob.media`:
  1. `FileMedia` and unfinalized appendable insert (`blob.upload is not None`) → `blob.media.link_into((dfd, base))` (destination is a hardlink to the still-growing staging inode);
  2. `FileMedia` (finalized/one-shot) → `blob.media.finalize((dfd, base))` (single O(1) promote);
  3. else (`BytesMedia`) → existing `to_bytes()`+`write_bytes_atomic` fallback.
  In all three, `_guard_collision` runs first and the sidecar is written after (media-then-sidecar order preserved). Staging files are unlinked on cancel/delete-upload.

  **Confirm-in-Step-1:** grep `_insert_empty_appendable_object` (upload.py:302-309 passes `upload=upload`) and the non-appendable finalize (`Object.init(... )` at :680, no `upload=`) to verify `blob.upload is not None` iff the insert is an in-progress appendable. If the codebase instead reliably distinguishes via `not blob.metadata.HasField("finalize_time")`, use that predicate — document the chosen discriminator in a code comment.

- [ ] **Step 1: Confirm the appendable discriminator, then write failing direct-drive tests**

```python
# tests/test_filestore_filemedia.py
import os
import tempfile
import unittest

from testbench.filestore import FileStore
from testbench.filemedia import FileMedia
# reuse _make_bucket/_make_object(_with_media) from tests/test_filestore.py


class TestFileStoreFinalize(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.fs = FileStore(self.root)
        self.fs.bucket_inserted(_make_bucket("bucket-name"))

    def test_new_upload_media_stages_under_gcs_uploads(self):
        m = self.fs.new_upload_media("projects/_/buckets/bucket-name", "up-1")
        self.assertIsInstance(m, FileMedia)
        self.assertTrue(os.path.exists(
            os.path.join(self.root, "bucket-name", ".gcs", "uploads", "up-1")))

    def test_object_inserted_promotes_finalized_filemedia_without_double_write(self):
        m = self.fs.new_upload_media("projects/_/buckets/bucket-name", "up-2")
        m.append(b"hello world")
        blob = _make_object_with_media("audio/clip.wav", m)   # finalized, blob.upload is None
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        media = os.path.join(self.root, "bucket-name", "audio", "clip.wav")
        self.assertEqual(b"hello world", open(media, "rb").read())
        self.assertTrue(os.path.exists(media + ".gcsmeta"))
        self.assertEqual([], os.listdir(                       # staging consumed by os.replace
            os.path.join(self.root, "bucket-name", ".gcs", "uploads")))

    def test_object_inserted_links_unfinalized_appendable(self):
        m = self.fs.new_upload_media("projects/_/buckets/bucket-name", "up-3")
        blob = _make_object_with_media("live.dat", m, appendable=True)  # blob.upload set, empty
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        dest = os.path.join(self.root, "bucket-name", "live.dat")
        self.assertTrue(os.path.exists(dest))                 # destination linked (0 bytes)
        m.append(b"grow")                                     # append AFTER link flows to dest
        self.assertEqual(b"grow", open(dest, "rb").read())    # shared inode
        self.assertFalse(m.is_finalized)

    def test_object_inserted_bytesmedia_still_uses_fallback(self):
        blob = _make_object(None, "b.txt", media=b"x")        # BytesMedia
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        self.assertEqual(b"x", open(
            os.path.join(self.root, "bucket-name", "b.txt"), "rb").read())

    def test_delete_upload_removes_staging(self):
        self.fs.new_upload_media("projects/_/buckets/bucket-name", "up-x")
        self.fs.delete_upload("projects/_/buckets/bucket-name", "up-x")
        self.assertEqual([], os.listdir(
            os.path.join(self.root, "bucket-name", ".gcs", "uploads")))
```

- [ ] **Step 2: Run to see it fail** — `AttributeError: 'FileStore' object has no attribute 'new_upload_media'`.

- [ ] **Step 3: Implement the factory overrides + finalize/link_into branches + cleanup**

```python
# testbench/filestore.py -- add near the object helpers:
    @contextlib.contextmanager
    def _uploads_dfd(self, bucket_name):
        short = self._bucket_name(bucket_name)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, [".gcs", "uploads"], create=True) as ufd:
                yield ufd

    def new_upload_media(self, bucket_name, upload_id):
        from testbench.filemedia import FileMedia
        with self._uploads_dfd(bucket_name) as ufd:
            # FileMedia.new_staging dup's ufd, so it survives this context close.
            return FileMedia.new_staging(ufd, upload_id)

    def new_staging_media(self, bucket_name, token):
        return self.new_upload_media(bucket_name, token)

    def delete_upload(self, bucket_name, upload_id):
        with self._uploads_dfd(bucket_name) as ufd:
            _unlink_quiet(ufd, upload_id)
```

```python
# testbench/filestore.py -- object_inserted (:134-148) becomes:
    def object_inserted(self, bucket_name, blob):
        from testbench.filemedia import FileMedia

        object_name = blob.metadata.name
        short = self._bucket_name(bucket_name)
        parts, base = self._dest_parts(object_name)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, parts, create=True) as dfd:
                self._guard_collision(dfd, base, object_name)   # write-time
                if isinstance(blob.media, FileMedia):
                    # blob.upload is set iff this is an in-progress (unfinalized)
                    # appendable insert (see _insert_empty_appendable_object,
                    # which passes upload=upload). Those keep growing, so hardlink
                    # the staging inode into the destination and leave it open;
                    # everything else is a one-shot promote. No to_bytes(), no
                    # double-write.
                    if getattr(blob, "upload", None) is not None:
                        blob.media.link_into((dfd, base))       # MEDIA CALL SITE (appendable)
                    else:
                        blob.media.finalize((dfd, base))        # MEDIA CALL SITE (one-shot)
                else:
                    data = blob.media.to_bytes()                # BytesMedia fallback
                    containment.write_bytes_atomic(dfd, base, data)
                sidecar.write_atomic(
                    dfd, base + ".gcsmeta", sidecar.dump(blob.metadata, object_name)
                )
```

Wire `delete_upload` to `Database.delete_upload`/`CancelResumableWrite` in Task 12 (staging-leak sweep); this task adds the method + its unit test only.

- [ ] **Step 4: Run to green** — `PYTHONPATH=. .venv/bin/python -m pytest tests/test_filestore_filemedia.py tests/test_filestore.py -q` → PASS.

- [ ] **Step 5: Both harness legs (B ≡ C via fallback)**

```bash
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness              # memory EMPTY diff
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file # B==C, one entry
```
Both pass: no producer creates a `FileMedia` yet (uploads still seed `BytesMedia(b"")` via `Upload.init`), so `object_inserted` takes the `BytesMedia` fallback exactly as in phase 4. The `FileMedia` branches are exercised only by the direct-drive unit tests.

- [ ] **Step 6: Mutation-check + call-site annotation**
- Collapse the branch to always `to_bytes()`+`write_bytes_atomic`. Expected: `test_object_inserted_promotes_finalized_filemedia_without_double_write` FAILS on the leftover-staging assertion. Revert.
- Swap the two `FileMedia` sub-branches (use `finalize` for the appendable case). Expected: `test_object_inserted_links_unfinalized_appendable` FAILS (the post-insert `append` hits a closed/cleared staging, and the destination does not grow). Revert. This proves the appendable insert must `link_into`, not `finalize`.
- Remove `_guard_collision(dfd, base, object_name)`. Expected: the existing phase-4 collision test in `tests/test_filestore.py` FAILS. Revert. (Unchanged from phase 4.)
- Update `tests/media_call_sites.txt`: replace the `# UNCOVERED testbench/filestore.py:138 …` annotation with the two new `# UNCOVERED testbench/filestore.py:<line> -- object_inserted promotes/links a FileMedia; only under TESTBENCH_STORE=file, coverage gate runs the memory-config trace (NullStore)` lines and keep the `BytesMedia` fallback line annotated.

- [ ] **Step 7: 3.8 parse, format, commit**

```bash
PYTHONPATH=. .venv/bin/python -c "import ast; [ast.parse(open(f).read(), feature_version=(3,8)) for f in ('testbench/filestore.py','testbench/filemedia.py')]"
.venv/bin/isort testbench/filestore.py testbench/filemedia.py tests/test_filestore_filemedia.py tests/media_call_sites.txt && .venv/bin/black testbench/filestore.py testbench/filemedia.py tests/test_filestore_filemedia.py
git add testbench/filestore.py testbench/filemedia.py tests/test_filestore_filemedia.py tests/media_call_sites.txt
git commit -m "feat(filestore): FileMedia upload staging factory + object_inserted finalize/link_into branches (no double-write)"
```

**Safety gate:** memory harness EMPTY diff; file harness B ≡ C via the `BytesMedia` fallback (no producer yet builds a `FileMedia`); the `FileMedia` promote/link branches proven by direct-drive tests and mutation-checked (no double-write; appendable links rather than replaces); staging opened under contained `.gcs/uploads`; `delete_upload` cleans staging.

---

### Task 5: Migrate uploads (REST resumable + gRPC `WriteObject`/`BidiWrite` non-appendable) onto staging `FileMedia`

**Files:**
- Modify: `testbench/rest_server.py` (resumable append `upload.media += data` :1292/:1297; finalize `upload.media.to_bytes()` :1185/:1304 — where `db` is in scope)
- Modify: `testbench/grpc_server.py` (`WriteObject` finalize `upload.media.to_bytes()` :1129)
- Modify: `gcs/upload.py` (import `Media`; bidi non-appendable finalize `upload.media.to_bytes()` :683)
- Test: `tests/test_filemedia_upload.py` (new; in-process readback + bounded RSS)

**Interfaces:**
- Consumes: `db.store.new_upload_media(bucket_name, upload_id)`; the `Object.init` pass-through (Task 1); the `object_inserted` finalize branch (Task 4).
- Produces: on the file backend, `Upload.media` is a `FileMedia` staged under `.gcs/uploads/<upload_id>`; appends via `O_APPEND` with rolling checksums; both finalize sites pass the `FileMedia` straight into `Object.init` (no `to_bytes()`); `FileStore.object_inserted` finalizes staging into the destination. On the memory backend `Upload.media` stays `BytesMedia` (factory returns `BytesMedia`), byte-identical.

**LEADING B ≡ C GATE:** gRPC `WriteObject` chunk boundaries and every readback's REST `Content-Length`/`framing` must stay byte-identical after routing uploads through `FileMedia` staging + finalize; the object metadata `crc32c`/`md5Hash` JSON fields (from the rolling checksums) must not move.

- [ ] **Step 1: Write the failing readback + bounded-memory test (concrete, in-process)**

```python
# tests/test_filemedia_upload.py  (top-level; may import internals)
import io
import os
import resource
import sys
import tempfile
import tracemalloc
import unittest

MiB = 1024 * 1024


def rss_bytes():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r if sys.platform == "darwin" else r * 1024   # macOS bytes, Linux KiB


class TestFileMediaUpload(unittest.TestCase):
    # Drive gcs.upload + FileStore IN-PROCESS so RUSAGE_SELF/tracemalloc observe
    # the process that actually streams the bytes (the gunicorn worker in the
    # subprocess Emulator is invisible to RUSAGE_SELF -- see emulator.py:219-221).
    def test_resumable_roundtrip_and_bounded_memory(self):
        # (a) stage a large upload chunk-by-chunk through new_upload_media +
        #     append; finalize via object_inserted; read back via chunks().
        # (b) assert crc32c/md5/bytes identical to a BytesMedia of the same
        #     stream, and peak RSS delta < 256 MiB for a 512 MiB object.
        ...  # full body: build FileStore(tmp), new_upload_media, append 512x1MiB
             # PRNG chunks, drive object_inserted, reopen via from_existing,
             # compare crc32c()/md5(); sample rss_bytes() before/after + tracemalloc
        self.assertLess(peak_delta, 256 * MiB)
        self.assertEqual(expected_crc, read_back.crc32c())
```

(Provide the full body when implementing — the scaffold above pins the RSS-sampling contract: in-process, unit-normalised, peak-delta ceiling.)

- [ ] **Step 2: Run to see it fail** — the bounded-RSS assertion FAILS today (`upload.media.to_bytes()` at :1304/:1129/:683 materialises the whole object).

- [ ] **Step 3: Thread the store factory and drop `to_bytes()`**

REST (`rest_server.py`): right after `Upload.init_resumable_rest(...)` (where `db` is in scope), set `upload.media = db.store.new_upload_media(upload.bucket.name, upload.upload_id)`. The append idioms `upload.media += data` (:1292/:1297) now route through `FileMedia.__iadd__` (O_APPEND). Replace both `Object.init(..., upload.media.to_bytes(), ...)` (:1185, :1304) with `Object.init(..., upload.media, ...)`.

gRPC (`grpc_server.py` `WriteObject` :1129) and `gcs/upload.py` bidi non-appendable finalize (:683): replace `upload.media.to_bytes()` with `upload.media`. Thread the factory in the servicer methods (`self.db`): set `upload.media = db.store.new_upload_media(upload.bucket.name, upload.upload_id)` immediately after `Upload.init...` returns, keeping `Upload.init` store-agnostic (no `db` param). Import `Media` in `gcs/upload.py` alongside `BytesMedia`.

Offset math: `len(upload.media)` (upload.py:238/243/261/551/556/574, rest resumable) is O(1) on `FileMedia`; `partial_media` resend slices `content` (a `bytes`), not the media, so nothing materialises.

- [ ] **Step 4: Run the leading gate + suite**

```bash
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file   # B==C: upload+readback framing/boundaries/checksums unmoved
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness                # memory EMPTY diff (factory -> BytesMedia)
PYTHONPATH=. .venv/bin/python -m pytest tests/test_filemedia_upload.py -q # readback + bounded RSS PASS
TESTBENCH_TEST_STORE=file PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
```

- [ ] **Step 5: Mutation-check**
- Revert the `upload.media = db.store.new_upload_media(...)` line (upload stays `BytesMedia` even on file). Expected: the bounded-RSS test FAILS (the fallback `to_bytes()` in `object_inserted` materialises), while B ≡ C still passes (bytes identical) — proving the streaming is what the RSS test guards. Revert.
- Re-introduce `upload.media.to_bytes()` at one finalize site. Expected: bounded-RSS test FAILS. Revert.

- [ ] **Step 6: Re-pin call sites, format, commit** — update `tests/media_call_sites.txt` (:1185/:1304/:1129/:683 now pass the media through; content-verify line numbers). `isort` then `black`; commit `feat(filemedia): stream resumable/gRPC uploads through O_APPEND staging + finalize`.

**Safety gate:** memory EMPTY diff; file B ≡ C on all upload/readback interactions (the leading gate); a mutation-checked in-process bounded-RSS test proves uploads no longer materialise; call-site gate green.

---

### Task 6: REST download normal + ranged true-streaming (drop the line-536 `to_bytes()` hot path, arithmetic Content-Length)

**Files:**
- Modify: `gcs/object.py` (`_download_range` :492-516 → arithmetic; `rest_media` :518-536/:544-658, `Content-Length` :676)
- Test: `tests/test_filemedia_download.py` (new; overflow-range parity + ranged bounded RSS)

**Interfaces:**
- Produces: `_download_range(request)` returns `(begin, end, length)` computed **arithmetically, without slicing** — reproducing today's clamping exactly: forward `bytes=B-E` → `begin=B`, `end=min(E+1, length)`; open `bytes=B-` → `begin=B`, `end=length`; suffix `bytes=-N` → `begin=length-N` (may be negative), `end=length`. `rest_media` (`instructions is None`, non-transcode) streams via the existing `_stream_media(self.media, max(0, begin), end)` helper (which already defaults `size=DOWNLOAD_CHUNK_SIZE`) and sets `headers["Content-Length"] = end - max(0, begin)` — **not** `end - begin` (the suffix-overflow `bytes=-N, N>length` case has a negative `begin`; `max(0, begin)` reproduces the whole-buffer length the pre-refactor slice produced). `content_range` is derived from the raw `begin` and clamped `end` exactly as today (`"bytes %d-%d/%d" % (begin, end - 1, length)`), so a negative start is preserved byte-for-byte. The 416 check stays `begin >= length`. `response_payload` is materialised via `to_bytes()` for **any `Media`** (widened guard, not just `BytesMedia`) ONLY inside the fault/instruction branches (`return-broken-stream`, `return-corrupted-data` → `corrupt_media`, `stall-*`, `return-503-after-256K` incl. the retry-success `flask.Response(response_payload)`), which are small trace-only objects — the normal/ranged path never materialises. The transcode path is Task 7.

**LEADING B ≡ C GATE:** `framing.mode` must stay `"content-length"` and `content_length_matches_body == true` across ALL ~50 REST download interactions, and the `Content-Length`/`Content-Range` VALUES must match the pre-refactor slicing code on every range axis. A generator of unknown length makes Werkzeug fall back to chunked transfer-encoding, flipping `framing.mode` — so the arithmetic `Content-Length` header MUST be set before returning the streamer. **The conformance trace only exercises `bytes=10-19`, `bytes=10-`, `bytes=-10`, and a `416` case — it is BLIND to overflow ranges**, so `tests/test_filemedia_download.py` is the required guard for those.

- [ ] **Step 1: Write the failing overflow-range parity + ranged bounded-memory test**

```python
# tests/test_filemedia_download.py
# For a 43-byte object served over BOTH backends, assert Content-Length and
# Content-Range byte-identical to the pre-refactor behaviour for:
#   bytes=40-100  (forward partial overflow -> CL=3, "bytes 40-42/43")
#   bytes=-100    (suffix overflow, N>length -> CL=43, "bytes -57-42/43")
#   bytes=10-19, bytes=10-, bytes=-10   (in-trace axes, regression guard)
# Plus a large-object ranged GET on the file backend asserting bounded RSS.
```

Fails today because :535-536 `to_bytes()` materialises the whole object for the unranged normal path, and (for the parity assertions) the pre-refactor `end - begin`/slice semantics differ from the new arithmetic if implemented naively.

- [ ] **Step 2: Restructure `_download_range` + `rest_media`**
- Rewrite `_download_range(request)` to return `(begin, end, length)` arithmetically (no payload slicing), matching the clamps above.
- In `rest_media`, drop the unconditional `if isinstance(response_payload, BytesMedia): response_payload = response_payload.to_bytes()` (:535-536). Keep `response_payload = self.media` for non-transcode. Compute `begin, end, length = self._download_range(request)`. Keep the 416 (`begin >= length`) and `content_range` exactly. The `instructions is None` non-transcode streamer already does `yield from _stream_media(self.media, max(0, begin), end)` — leave it.
- Immediately after `instructions = extract_instruction(...)`, materialise for the fault path: `if instructions is not None and not is_decompressive_transcode and isinstance(response_payload, Media): response_payload = response_payload.to_bytes()`. The fault branches then use `response_payload` bytes exactly as today (they run with no range → `begin=0/end=length`, so absolute indexing stays correct).
- Set `headers["Content-Length"] = end - max(0, begin)` for the non-transcode path (transcode keeps `len(response_payload)` until Task 7).

- [ ] **Step 3: Run the leading gate**

```bash
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file    # framing.mode stays content-length on every download
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness                 # memory EMPTY diff
PYTHONPATH=. .venv/bin/python -m pytest tests/test_filemedia_download.py -q # overflow-range parity + ranged bounded RSS PASS
TESTBENCH_TEST_STORE=file PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py  # faults.json objects are FileMedia now
```

- [ ] **Step 4: Mutation-check (two guards)**
- Remove `headers["Content-Length"] = end - max(0, begin)` (let Werkzeug infer). Expected: harness `--store file` AND memory FAIL because `framing.mode` flips to `chunked` on every download. Revert. Sharpest framing tripwire.
- Change `end - max(0, begin)` to `end - begin`. Expected: `tests/test_filemedia_download.py` suffix-overflow (`bytes=-100`) FAILS (`content_length_matches_body` false: header says `100`, body is `43`). Revert.

- [ ] **Step 5: Re-pin call sites (:535/:536 removed from the normal path; the fault-branch `to_bytes()` now guards on `Media`), format, commit** — `refactor(object): stream normal+ranged REST download, arithmetic clamped Content-Length`.

**Safety gate:** memory EMPTY diff; file B ≡ C with `framing.mode == content-length` on every download and byte-identical `Content-Length`/`Content-Range` including the two trace-blind overflow axes; faults.json green on `--store file` (fault branches materialise any `Media`); ranged download bounded-RSS test green; both mutations redden a gate.

---

### Task 7: Decompressive-transcode streaming (two-pass counted `Content-Length`)

**Files:**
- Modify: `gcs/object.py` (`rest_media` transcode branch :519-522, :548-551/:652-656, `Content-Length` :676)
- Test: extend `tests/test_filemedia_download.py` with a gzip-encoded object + `accept-encoding: identity` download

**Interfaces:**
- Produces: the transcode path wraps `self.media.reader()` in `gzip.GzipFile` and yields `gz.read(DOWNLOAD_CHUNK_SIZE)` chunks; the transcoded `Content-Length` is obtained by a **bounded counting pass** (a first `GzipFile(self.media.reader())` read in chunks, summing lengths), then the body is streamed from a **second** fresh `GzipFile(self.media.reader())`. No whole decompressed buffer is materialised.

**LEADING B ≡ C GATE:** the transcode interaction's framing must stay `mode == "content-length"`, `content_length_matches_body == true`, and the `Content-Length` value byte-identical (the transcoded length is not known a priori — the counting pass supplies it).

- [ ] **Step 1: Write the failing bounded-memory transcode test** — a gzip object downloaded with `accept-encoding: identity` on the file backend, asserting bounded RSS + identical body length. Fails today (`gz.read()` at :522 materialises the whole decompressed object).

- [ ] **Step 2: Implement the two-pass streaming transcode** — replace :521-522 `response_payload = gz.read()` with a counting pass computing `transcoded_len` (open `GzipFile(self.media.reader())`, read `DOWNLOAD_CHUNK_SIZE` chunks, sum lengths, close); set `headers["Content-Length"] = transcoded_len`; make the transcode streamers (`instructions is None` :548-551 and default `else` :652-656) yield from a fresh `GzipFile(self.media.reader())` in `DOWNLOAD_CHUNK_SIZE` chunks. Keep the fault-injection streamers on a materialised buffer (documented fault-only path).

- [ ] **Step 3: Run the leading gate**

```bash
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file    # transcode framing + Content-Length byte-identical
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness                 # memory EMPTY diff (BytesMedia.reader path identical)
PYTHONPATH=. .venv/bin/python -m pytest tests/test_filemedia_download.py -q # bounded RSS transcode PASS
nix develop --command make verify-linux                                    # gzip OS-byte / arch-sensitive crc32c
```

- [ ] **Step 4: Mutation-check** — replace the counting-pass `Content-Length` with `len(self.media)` (the compressed length). Expected: the transcode interaction's `content_length_matches_body` flips to `false` → `--store file` and memory FAIL. Revert.

- [ ] **Step 5: Re-pin call sites (:521/:524 transcode reader sites), format, commit** — `refactor(object): stream decompressive transcode with counted Content-Length`.

**Safety gate:** memory EMPTY diff; file B ≡ C with the transcode `Content-Length`/framing byte-identical (mutation-checked); bounded-RSS transcode test green; `make verify-linux` green (the gzip-OS-byte-sensitive leg).

---

### Task 8: Compose streaming (gRPC :521/:551 + REST F1 :710-739)

**Files:**
- Modify: `testbench/grpc_server.py` (`ComposeObject` `composed_media = BytesMedia()` :521, `composed_media.append(source_blob.media.to_bytes())` :551)
- Modify: `testbench/rest_server.py` (`composed_media = b""` :710-711, `composed_media += source_object.media` :739)
- Test: `tests/test_filemedia_compose.py` (new; multi-source compose bounded RSS + identical bytes/checksums)

**Interfaces:**
- Produces: `composed_media = db.store.new_staging_media(dest_bucket, token)` (`token = uuid4().hex`); per source, stream `for chunk in source.media.chunks(0, len(source.media), SIZE): composed_media.append(chunk)`; `Object.init(request, metadata, composed_media, ...)` → `object_inserted` finalizes staging into the destination. Removes the `.to_bytes()` append (gRPC) and the `b"" += media` `__radd__` idiom (REST F1). On the memory backend the factory returns `BytesMedia`, so `chunks`+`append` reproduce identical composed bytes.

**LEADING B ≡ C GATE:** the compose interaction's result bytes, `crc32c`/`md5Hash` JSON, and both gRPC + REST response metadata unmoved.

- [ ] **Step 1: Write the failing multi-source compose bounded-memory test** — N sources of ~64 MiB each composed on the file backend (in-process driver); assert bounded RSS + identical result crc32c. Fails today (`.to_bytes()` / `+=` materialise each source).

- [ ] **Step 2: Migrate both compose sites** to the staging-`FileMedia` + chunked-append idiom. For REST, the destination bucket is `bucket_name`; mint `uuid4().hex` for `new_staging_media`.

- [ ] **Step 3: Run the leading gate + suite**

```bash
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file   # compose interaction unmoved
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness                # memory EMPTY diff
PYTHONPATH=. .venv/bin/python -m pytest tests/test_filemedia_compose.py -q
```

- [ ] **Step 4: Mutation-check** — in the REST migration, revert to `composed_media += source_object.media`. Expected: on the file backend the compose bounded-RSS test FAILS (`__radd__` materialises the whole source); B ≡ C still passes (bytes identical). Revert.

- [ ] **Step 5: Re-pin call sites, format, commit** — `refactor(compose): stream gRPC+REST compose source->staging (drop to_bytes/__radd__)`.

**Safety gate:** memory EMPTY diff; file B ≡ C on the compose interaction (leading gate); the F1 `__radd__` materialiser removed and mutation-checked; bounded-RSS compose test green; call-site gate green.

---

### Task 9: Rewrite + Move/Copy streaming (gRPC :1096/:1194 + REST F1 :789/:865)

**Files:**
- Modify: `testbench/grpc_server.py` (MoveObject `dst_media = BytesMedia(); dst_media.append(src_object.media.to_bytes())` :1095-1096; RewriteObject `rewrite.media.append(src_object.media[slice])` :1194)
- Modify: `testbench/rest_server.py` (copy/move `dst_media = b""; dst_media += src_object.media` :786-789; rewrite `rewrite.media += src_object.media[slice]` :865)
- Modify: `gcs/rewrite.py` (`init_rest` :71, `init_grpc` :124 seed `BytesMedia(b"")` — the caller supplies the staging media)
- Test: `tests/test_filemedia_rewrite.py` (new; multi-call rewrite + move/copy bounded RSS + identical metadata)

**Interfaces:**
- Produces: `rewrite.media = db.store.new_staging_media(dst_bucket, rewrite.token)` (set by the caller after `Rewrite.init*`, keeping the classmethod store-agnostic); the per-call window append streams `src_object.media.chunks(offset, total, size)` into staging rather than slicing a `bytes` buffer; on `done`, `Object.init(..., rewrite.media, ...)` finalizes into the destination. MoveObject/copy build a staging `FileMedia` and stream `src.media.chunks(...)` into it (default). A `FileStore` hardlink/rename fast-path for store-internal move is an explicit **deferred optimization** (a shared inode would interact with the inode-collision detector at filestore.py:359-366; the default chunked copy avoids that).

**LEADING B ≡ C GATE:** rewrite `rewriteToken`, per-call `totalBytesRewritten`/`objectSize` windows, and final `crc32c`/`md5Hash` unmoved on both transports.

- [ ] **Step 1: Write the failing rewrite/move bounded-memory test** — a rewrite whose `maxBytesRewrittenPerCall` exceeds the bounded-memory budget, plus a move/copy of a large object, on the file backend (in-process driver); assert per-call windows + final checksums identical and RSS bounded. Fails today (the `src.media[slice]` / `+=` materialise each window / the whole source).

- [ ] **Step 2: Migrate the four sites** to staging `FileMedia` + `chunks(offset, total, size)` windowed streaming; thread `rewrite.media = db.store.new_staging_media(...)` at the gRPC `RewriteObject`/REST rewrite handlers (both hold `db`). Keep `len(rewrite.media)`/`len(src_object.media)` (O(1)) for the window math.

- [ ] **Step 3: Run the leading gate + suite**

```bash
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file   # rewrite/move/copy interactions unmoved
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness                # memory EMPTY diff
PYTHONPATH=. .venv/bin/python -m pytest tests/test_filemedia_rewrite.py -q
```

- [ ] **Step 4: Mutation-check** — revert the REST rewrite to `rewrite.media += src_object.media[slice]`. Expected: file-backend rewrite bounded-RSS test FAILS (`__getitem__` materialises the slice); B ≡ C still passes. Revert.

- [ ] **Step 5: Re-pin call sites (:865/:789/:1096/:1194 change shape), format, commit** — `refactor(rewrite): stream gRPC+REST rewrite/move/copy windows source->staging`.

**Safety gate:** memory EMPTY diff; file B ≡ C on rewrite/move/copy (leading gate); F1 slice/`+=` materialisers removed and mutation-checked; bounded-RSS rewrite test green; call-site gate green.

---

### Task 10: Appendable (F2) alias resolution + `object_updated` seal (trace-UNCOVERED, dedicated test)

**Files:**
- Modify: `gcs/upload.py` (import `Media`; `_insert_empty_appendable_object` `upload.media.to_bytes()` :305; `update_appendable_blob` alias :613-617; `finalize_blob` alias :659-663)
- Modify: `testbench/filestore.py` (`object_updated` :166-175 — add the appendable seal branch)
- Test: `tests/test_appendable_filemedia.py` (new; upload appendable, flush across ≥2 checkpoints, finalize, read back bytes+crc32c+md5; restart + hydrate)

**Interfaces:**
- Produces: on the file backend, the empty-insert (:305) passes the empty staging `FileMedia` (not `to_bytes()`); the `isinstance(BytesMedia)` alias guards at :613-617/:659-663 generalise to `isinstance(upload.media, Media)` so a `FileMedia` is not re-wrapped; `blob.media` **aliases** the single `O_APPEND` staging `FileMedia` across checkpoints (no defensive multi-GB copy). The lifecycle is realised entirely through the FileStore signals and the Task-3 primitives:
  - `object_inserted` for the empty appendable insert (Task 4): `link_into((dfd, base))` — the destination is a hardlink to the staging inode; the `O_APPEND` fd stays open, so every subsequent `upload.media += content` grows BOTH the staging name and the destination (one inode).
  - `object_updated` on each intermediate checkpoint (`blob.upload is not None`, `not blob.media.is_finalized`): **sidecar-only** — the media bytes are already live at the destination via the shared inode; the checkpoint only rewrites size/crc into the sidecar (O(1) from the rolling values).
  - `object_updated` on the finalize checkpoint (`finalize_blob` set `blob.upload = None`, media still `not is_finalized`): call `blob.media.seal()` (closes the append fd, unlinks the staging name, freezes md5, drops staging → `is_finalized` True; the destination hardlink + inode survive), then write the sidecar.

  This resolves the concrete failure the reviewer flagged: **no `os.replace` runs per checkpoint** (which would consume the staging name and close the fd), so the second and later `upload.media += content` calls keep succeeding against the same open fd; and `seal` runs **exactly once**, gated on the `finalize_blob`-only `blob.upload is None` signal, so "finalize once" is well-defined and implementable.

**Concrete `object_updated` (replaces filestore.py:166-175):**

```python
    def object_updated(self, bucket_name, blob):
        from testbench.filemedia import FileMedia

        short = self._bucket_name(bucket_name)
        parts, base = self._dest_parts(blob.metadata.name)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, parts, create=False) as dfd:
                # Appendable finalize checkpoint: finalize_blob cleared blob.upload
                # while the media is still an unsealed staging FileMedia. Seal it
                # (close append fd, unlink the staging NAME; the destination
                # hardlink established at object_inserted + its inode survive).
                # Intermediate checkpoints (blob.upload set) and PATCH/ACL updates
                # (BytesMedia, or an already-sealed FileMedia) fall through to the
                # sidecar-only write below, unchanged from phase 4.
                if (
                    isinstance(blob.media, FileMedia)
                    and not blob.media.is_finalized
                    and getattr(blob, "upload", None) is None
                ):
                    blob.media.seal()   # MEDIA CALL SITE (appendable finalize)
                sidecar.write_atomic(
                    dfd,
                    base + ".gcsmeta",
                    sidecar.dump(blob.metadata, blob.metadata.name),
                )
```

**Rationale (resolving the phase-3 deferred comment):** a single staging file, hardlinked into the live destination at insert and sealed once at finalize, is correct here because (a) there is no mid-upload read of `blob.media` other than through the aliased `FileMedia` itself (the bidi loop only writes and reports `persisted_size`), and reads that do occur go through the `FileMedia`'s own read fd, not the destination pathname; (b) the destination pathname stays byte-consistent with the sidecar throughout (empty → growing) via the shared inode, so a would-be hydration of an in-progress object never sees a sidecar-without-media; (c) a defensive snapshot of a multi-GB staging file would itself violate bounded memory. This path is **trace-UNCOVERED**, so B ≡ C cannot guard it — the dedicated test is the safety net. The shared-inode window does not confuse the startup inode-collision detector (filestore.py:359-366): the scan excludes `.gcs/uploads`, so it never sees two names for the one inode, and after `seal` only the destination link remains.

- [ ] **Step 1: Confirm the signals, then write the failing appendable round-trip test** — grep `finalize_blob` (:668 `blob.upload = None`) and `_insert_empty_appendable_object` (:302-309 `upload=upload`) to confirm `blob.upload is None` fires only at finalize and `blob.upload is not None` on intermediate checkpoints. Then `tests/test_appendable_filemedia.py`: create an empty appendable object, append across ≥2 checkpoints, finalize, read back and assert bytes + `crc32c` + `md5`; then restart the emulator (new `FileStore` over the same root, `rebuild_index`) and assert the finalized object hydrates and reads back identically.

- [ ] **Step 2: Run to see it fail** — the empty-insert `to_bytes()` (:305) and the `BytesMedia`-only alias guards mean the file backend never stages appendable growth to disk; readback/restart FAILS.

- [ ] **Step 3: Implement** — (a) :305 pass `upload.media` through; (b) import `Media` in `gcs/upload.py` and generalise the two alias guards to `isinstance(upload.media, Media)`; (c) add the `object_updated` seal branch above; (d) document the `blob.upload` routing decision in a code comment at both the FileStore branch and `finalize_blob`.

- [ ] **Step 4: Run the dedicated test + both harness legs**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_appendable_filemedia.py -q
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file   # still one allow-list entry (appendable not in the trace)
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness                # memory EMPTY diff
```

- [ ] **Step 5: Mutation-check**
- Remove the `object_updated` seal branch (sidecar-only, as phase 4). Expected: `tests/test_appendable_filemedia.py` FAILS — after finalize the staging name still exists / `blob.media.is_finalized` is False, and the restart-readback sees a leftover staging plus an unsealed md5. Revert.
- Change the seal gate from `getattr(blob, "upload", None) is None` to unconditional (seal on every checkpoint). Expected: the ≥2-checkpoint test FAILS — the first intermediate checkpoint seals (closes the append fd), and the next `upload.media += content` raises on the closed staging. Revert. This kills the "seal exactly once" mutant.
- Revert :305 to `to_bytes()`. Expected: the appendable test FAILS on the file backend (staging never links growth to disk). Revert.

- [ ] **Step 6: Re-pin call sites (upload.py :305/:613-617/:659-663 annotated `# UNCOVERED … no appendable upload in the trace`; filestore object_updated seal line), format, commit** — `feat(filemedia): appendable staging alias + object_updated seal (dedicated test)`.

**Safety gate:** memory EMPTY diff; file harness still exactly one allow-list entry (appendable is trace-UNCOVERED, no new golden); the link-once/seal-once lifecycle pinned by a ≥2-checkpoint round-trip + restart test, mutation-checked (seal-branch removal AND seal-every-checkpoint both redden the dedicated test); call-site annotations updated.

---

### Task 11: Startup hydration via `FileMedia.from_existing` (restart bounded-memory)

**Files:**
- Modify: `testbench/filestore.py` (`_read_media` :347-351, `_hydrate_live` :353-370, `_hydrate_soft_deleted` :372-380)
- Test: `tests/test_filemedia_restart.py` (new; large-object restart bounded RSS + readback)

**Interfaces:**
- Produces: `_hydrate_live`/`_hydrate_soft_deleted` construct a read-only `FileMedia.from_existing(dir_fd, name, size=obj_proto.size, crc32c_value=obj_proto.checksums.crc32c, md5_value=obj_proto.checksums.md5_hash)` pointed at the on-disk media path instead of `_read_media`'s whole-file `open(path).read()`. `Object.__init__` accepts the `Media` (Task 1) and does NOT re-hash at construction (only `Object.init` recomputes checksums, and hydration uses `Object(...)`, not `init`). The inode-collision detector (`st_dev`/`st_ino`, filestore.py:359-366) is unaffected — it stats the media path, not the media object.

**B ≡ C note:** hydration only runs at startup from an existing tree; the conformance emulator starts with an empty root, so this change is invisible to the harness. The in-process restart test is the safety net (as in the phase-4 plan's scan task).

- [ ] **Step 1: Write the failing restart bounded-memory test** — write a large object under the file backend (in-process), drop the `Database`, build a fresh `FileStore` over the same root, `rebuild_index`, and assert (a) the object reads back byte-identically and (b) peak RSS during hydration stays under baseline + 256 MiB (unit-normalised `ru_maxrss` as in Task 5). Fails today (`_read_media` reads the whole file into a `BytesMedia`).

- [ ] **Step 2: Implement** — replace the `self._read_media(media_path)` construction with `FileMedia.from_existing(...)` opened via the containment walk (thread the leaf `dir_fd` into `_hydrate_live`/`_hydrate_soft_deleted`, or open the media file under an O_NOFOLLOW walk). Keep the `seen`/inode collision detection unchanged. Retain `_read_media` only if a small fallback is still needed for soft-deleted `media` files; otherwise remove it and re-pin any reference.

- [ ] **Step 3: Run the restart test + both harness legs**

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_filemedia_restart.py -q
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file   # empty-root hydration is a no-op -> B==C unchanged
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness                # memory EMPTY diff
```

- [ ] **Step 4: Mutation-check** — revert `_hydrate_live` to `self._read_media(media_path)` (whole-file read into `BytesMedia`). Expected: `tests/test_filemedia_restart.py` bounded-RSS assertion FAILS; readback still passes. Revert. Also verify the collision detector still fires: reintroduce a case-insensitive collision on the target FS and confirm the existing phase-4 collision test in the scan suite still FAILS-on-collision (unchanged behaviour).

- [ ] **Step 5: Format, commit** — `refactor(filestore): hydrate FileMedia from disk instead of whole-file reads`.

**Safety gate:** memory EMPTY diff; file B ≡ C unchanged (empty-root hydration no-op); large-object restart reads back identically with bounded RSS (mutation-checked); inode-collision detector unaffected.

---

### Task 12: Mechanism 5 bounds, fault-path UNCOVERED tests, staging-leak sweep (uploads + rewrites), call-site finalization, exit gate

**Files:**
- Create: `tests/test_filemedia_bounds.py`, `tests/test_filemedia_faults.py`
- Modify: `testbench/filestore.py` / `testbench/database.py` (staging cleanup on `CancelResumableWrite`/`delete_upload`/abort AND abandoned rewrite tokens)
- Modify: `tests/media_call_sites.txt` (finalize active + `# UNCOVERED` annotations)

**Interfaces:**
- Produces: the Mechanism-5 detectors over the whole up+down path, driven **in-process** against `FileStore`/`gcs.upload`/`gcs.object` (4GB up + 4GB down, peak RSS < baseline + 256 MiB; linear-time `t(2N)/t(N) < 3` at `N = 256 MiB` in CI), with `ru_maxrss` unit-normalised (bytes on macOS, KiB on Linux) and the 4GB leg gated behind an env flag so laptops skip it; dedicated tests for the B ≡ C-invisible media fault paths; `.gcs/uploads/<id>` cleanup on all abort paths (uploads AND rewrites) so staging does not leak.

- [ ] **Step 1: Write the Mechanism-5 bounds tests** (`tests/test_filemedia_bounds.py`) — concrete bodies:

```python
import os
import resource
import sys
import tempfile
import time
import tracemalloc
import unittest

MiB = 1024 * 1024
N_DEFAULT = 256 * MiB
BIG = os.environ.get("TESTBENCH_BOUNDS_4GB") == "1"


def rss_bytes():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r if sys.platform == "darwin" else r * 1024


def _prng_chunks(total, size, seed=1234567):
    x = seed
    produced = 0
    while produced < total:
        n = min(size, total - produced)
        x = (1103515245 * x + 12345) & 0xFFFFFFFF
        yield (bytes((x >> (i % 24) & 0xFF) for i in range(min(n, 64))) * (n // 64 + 1))[:n]
        produced += n


class TestBoundedMemory(unittest.TestCase):
    def test_upload_then_download_is_bounded(self):
        total = 4 * 1024 * MiB if BIG else N_DEFAULT
        root = tempfile.mkdtemp()
        from testbench.filestore import FileStore
        fs = FileStore(root)
        fs.bucket_inserted(_make_bucket("b"))
        base = rss_bytes(); tracemalloc.start()
        media = fs.new_upload_media("projects/_/buckets/b", "u")
        for chunk in _prng_chunks(total, 8 * MiB):
            media.append(chunk)
        blob = _make_object_with_media("big.bin", media)
        fs.object_inserted("projects/_/buckets/b", blob)
        # download: stream via chunks(), discard
        hydrated = _reopen(fs, "b", "big.bin")
        seen = 0
        for c in hydrated.chunks(0, len(hydrated), 8 * MiB):
            seen += len(c)
        peak_py = tracemalloc.get_traced_memory()[1]; tracemalloc.stop()
        self.assertEqual(total, seen)
        self.assertLess(rss_bytes() - base, 256 * MiB)
        self.assertLess(peak_py, 256 * MiB)


class TestLinearTime(unittest.TestCase):
    def _elapsed(self, n):
        root = tempfile.mkdtemp()
        from testbench.filemedia import FileMedia
        dfd = os.open(root, os.O_RDONLY)
        try:
            m = FileMedia.new_staging(dfd, "t")
            chunk = b"a" * (4 * MiB)
            t0 = time.perf_counter()
            for _ in range(n):
                m.append(chunk)
            for _ in m.chunks(0, len(m), 4 * MiB):
                pass
            return time.perf_counter() - t0
        finally:
            os.close(dfd)

    def test_append_read_is_linear(self):
        n = 32
        self.assertLess(self._elapsed(2 * n) / max(self._elapsed(n), 1e-6), 3.0)
```

- [ ] **Step 2: Write the fault-path tests** (`tests/test_filemedia_faults.py`) — concrete assertions for the trace-UNCOVERED media fault paths, all over a `FileMedia` (file backend): (a) `return-broken-stream` on a gRPC/REST read — assert the partial byte prefix + abort; (b) `inject-upload-data-error` → `corrupt_media` over a `FileMedia` in `Object.init` — assert the fault path materialises a small buffer and aborts as before (the documented fault-only `to_bytes()`); (c) `return-503-after-256K` retry-success — assert `flask.Response(response_payload)` receives genuine `bytes` (materialised via the widened `Media` guard from Task 6) and returns 200; (d) resumable finalize on the `bytes */N` branch (rest_server.py:1185); (e) non-Content-Range simple-upload completion (rest_server.py:1297). B ≡ C is blind to all of these.

- [ ] **Step 3: Wire staging cleanup (uploads AND rewrites)** — `CancelResumableWrite`/`Database.delete_upload`/upload abort unlink `.gcs/uploads/<upload_id>` via `FileStore.delete_upload` (Task 4); **additionally** unlink `.gcs/uploads/<rewrite.token>` and drop the append fd when a multi-call rewrite is dropped/expired (Task 9 stages `rewrite.media` there and only finalizes on the terminal `done`; an abandoned rewrite otherwise leaks the staging file and an open fd). Add `FileStore.delete_rewrite(bucket_name, token)` (mirrors `delete_upload`) and call it from the rewrite drop/expiry path. Add two tests: a cancelled upload and an abandoned rewrite each leave no staging file.

- [ ] **Step 4: Final sweep** — grep for any remaining large-path materialiser and confirm each surviving one is fault-injection-only and small:

```bash
cd /Users/adrianborup/coding/storage-testbench
grep -rn "to_bytes()\|gzip.decompress\|\.media\[" gcs/ testbench/ | grep -v "# fault-only\|corrupt_media\|test"
```
Every hit must be a documented fault-injection path (`corrupt_media` object.py:128/:585, `return-broken-stream`, `stall-*`, `return-503-after-256K`) or the `BytesMedia` fallback in `object_inserted`. Annotate each.

- [ ] **Step 5: Finalize `tests/media_call_sites.txt`** — every new memory-reachable `.media` site added by Tasks 5–9 is listed active; every FileMedia-only site (`object_inserted` finalize/link_into branches, `object_updated` seal, appendable, staging) is annotated `# UNCOVERED … <reason>`. Run the gate:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_media_call_sites.py -q
```

- [ ] **Step 6: Full exit gate on both legs**

```bash
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness                                 # memory OK, EMPTY diff, digest 98fa2130…
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness --store file                    # B==C, one allow-list entry
TESTBENCH_TEST_STORE=memory PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
TESTBENCH_TEST_STORE=file   PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
PYTHONPATH=. .venv/bin/python -m pytest tests/test_filemedia_bounds.py tests/test_filemedia_faults.py -q
TESTBENCH_BOUNDS_4GB=1 PYTHONPATH=. .venv/bin/python -m pytest tests/test_filemedia_bounds.py -q   # CI/Linux 4GB leg
nix develop --command make verify-linux
.venv/bin/isort <tracked *.py excluding *_pb2*.py> && .venv/bin/black <same>
```

- [ ] **Step 7: Commit** — `feat(filemedia): Mechanism-5 bounds, fault-path tests, staging cleanup (uploads+rewrites), call-site finalization`.

**Safety gate (phase-5 exit):** memory harness byte-identical (digest `98fa2130…`); file harness byte-identical except the one mutation-checked allow-list entry; suite green on both `TESTBENCH_TEST_STORE` legs; Mechanism-5 bounded-memory (4GB up+down < baseline+256 MiB, measured in-process against the streaming code) and linear-time (`t(2N)/t(N) < 3`) green; no large-object materialiser survives outside documented fault-only paths; staging cleaned on abort for BOTH uploads and rewrites; media call-site gate green; `make verify-linux` green; `setup.py` unchanged; nothing in `tests/conformance/` imports internals except `emulator.py`.

---

## Self-Review

**Spec/handoff coverage (Seam B / Media table; Large-object handling; Mechanism 5; the enumerated escape hatches):**
- `FileMedia` backing the full `BytesMedia` interface (pread reads, O_APPEND staging, incremental crc32c/md5, chunks/reader, finalize/link_into/seal, fd lifecycle, to_bytes fallback) → Tasks 2, 3. ✅
- Construction/selection seam: `Media` base + widened `Object.__init__`/`init` guards + Store media factory (memory stays `BytesMedia`, digest `98fa2130…` unmoved) → Tasks 1, 4. ✅
- EVERY enumerated escape hatch migrated: resumable/gRPC/bidi upload finalize (`to_bytes` at upload.py:305/:683, rest:1185/1304, grpc:1129) → Tasks 5, 10; REST download normal+ranged (object.py:536) → Task 6; gzip transcode (object.py:522) → Task 7; gRPC compose (grpc:551) + REST F1 compose (rest:739) → Task 8; gRPC rewrite/MoveObject (grpc:1096/:1194) + REST F1 rewrite/copy/move (rest:789/:865) → Task 9; `object_inserted` (filestore:138) → Task 4; hydration (filestore:347-380) → Task 11; F2 appendable alias (upload.py:613/:659) → Task 10. ✅
- B ≡ C guardrails for wire-shape-affecting changes: gRPC read chunk-boundary + trailing-empty asymmetry pinned by the Task-2 `chunks()` parity test (incl. mid-offset begin>0) BEFORE any wiring; REST `Content-Length` framing led in Tasks 6/7, with the arithmetic Content-Length CLAMPED (`end - max(0, begin)`, `end = min(raw_end, length)`) and two mutations that redden a gate, plus a dedicated overflow-range parity test because the trace is blind to those axes; fault branches materialise ANY `Media` so faults.json stays B ≡ C on `--store file`; every migration task leads with the exact observable it could move. ✅
- Mechanism 5: 4GB bounded-memory RSS cap + O(n) vs O(n²) linear-time detector, driven IN-PROCESS (RUSAGE_SELF/tracemalloc valid; unit-normalised) so it measures the streaming code, not client buffering or an invisible worker grandchild — isolated in Task 3 and end-to-end in Task 12. ✅
- Trace-UNCOVERED media paths (appendable multi-checkpoint + restart, broken-stream, corrupt_media, 503 retry-success, resumable */N, non-Content-Range simple-upload) → dedicated tests in Tasks 10, 12. ✅
- `tests/media_call_sites.txt` re-pinned under `--store file` each migration + finalized in Task 12. ✅

**Deliberate design decisions (resolving the survey's open questions):**
1. **Two promotion primitives, not one.** One-shot producers use `finalize((dst_dir_fd, dst_name))` → `containment.promote` (contained `os.replace`, closes staging). The appendable growth path uses `link_into` (`containment.hardlink`, keeps the O_APPEND fd live so the destination and staging share one growing inode) + a single terminal `seal` (`containment.unlink_at` of the staging name). This is the fix for the multi-checkpoint break: no `os.replace` per checkpoint, the append fd is never closed mid-upload, and `seal` runs exactly once (gated on the finalize-only `blob.upload is None` signal). `BytesMedia.finalize(dest)` stays a no-op; `object_inserted`/`object_updated` only call the primitives on the `FileMedia` branch, so the memory path never reaches them.
2. **Staging lives under `<bucket>/.gcs/uploads/<id-or-token>`, owned by `FileStore`**, handed out via `new_upload_media`/`new_staging_media`; the staging fd is dup'd inside `new_staging` (defined once in Task 3, not mutated by Task 4) so it outlives the caller's dir_fd context. Same filesystem as the destination → O(1) `os.replace`/`os.link`; cross-device raises `EXDEV`.
3. **db.store threaded by the caller** (grpc/rest servicer methods hold `db`) setting `upload.media`/`rewrite.media = db.store.new_*` after `init`; `Upload.init`/`Rewrite.init` stay store-agnostic; `gcs/` imports `Media` only (never `FileStore`), import graph acyclic. `Media` imported into `gcs/object.py` (Task 1) and `gcs/upload.py` (Tasks 5/10).
4. **No `new_media`.** Every producer is bucket-scoped, so the factory surface is exactly `new_upload_media`/`new_staging_media` — no defined-but-unused method, no `FileStore.new_media` raise.
5. **Move/copy defaults to a chunked copy**, not a hardlink — a shared inode would interact with the inode-collision detector; the hardlink/rename fast-path is an explicit deferred optimization (Task 9). (The appendable path DOES hardlink, but into a `.gcs/uploads`-excluded staging inode, so the scan never double-counts it.)
6. **REST Content-Length is arithmetic and clamped**: `_download_range` returns `(begin, end, length)` with `end = min(raw_end, length)` (forward) / `end = length` (open+suffix), and `Content-Length = end - max(0, begin)` — byte-identical to the pre-refactor `len(slice)` on every axis including forward-partial-overflow and suffix-overflow (negative `begin`), guarded by a dedicated test since the trace is blind (Task 6).
7. **Fault-injection streamers materialise ANY `Media`** (widened guard, not `BytesMedia`-only) via a small `to_bytes()`, because on `--store file` `self.media` is a `FileMedia` that `corrupt_media`/`flask.Response` cannot consume — keeping faults.json B ≡ C (Task 6).
8. **gzip transcode Content-Length via a bounded counting pass**, then stream the body from a second `GzipFile` (Task 7).
9. **Hydration trusts the sidecar's persisted crc32c/md5** (`from_existing`) so restart is bounded-memory/linear-time; `_from_open_fd` remains for a future integrity check (Task 11).
10. **Explicit fd ownership**: `FileMedia.close()` (idempotent) + `__del__` release `_read_fd` and any staging fds; `reader()`'s dup is owned by its `BufferedReader(closefd=True)`; `finalize`/`seal` release the append fd + the dup'd staging dir_fd. No fd leak across many uploads/hydrations (Task 2/3).
11. **Staging cleanup covers uploads AND rewrites** (`delete_upload`/`delete_rewrite`), each with an abandoned-token test (Task 12).
12. **Zero-length pread ValueError/edge special-cased** in every read path, pinned by Task-2 unit tests.

**Placeholder scan:** no `TBD`/`TODO`/"handle edge cases". Every code step shows real code and every run step a real `.venv/bin` command with an expected result. The hardest tests carry concrete bodies or real-assertion scaffolds with the RSS-sampling/PRNG-streaming contract spelled out (unit-normalised `ru_maxrss`, in-process driver, `tracemalloc` peak): bounded-memory + linear-time (Task 12 Step 1 full code), upload (Task 5), download/overflow/transcode (Tasks 6/7), compose/rewrite (Tasks 8/9), restart (Task 11), faults (Task 12). `FileMedia` methods (`__len__`/`__getitem__`/`chunks`/`reader`/`crc32c`/`md5`/`to_bytes`/`append`/`__iadd__`/`finalize`/`link_into`/`seal`/`close`/`is_finalized`/`from_existing`/`new_staging`) carry exact bodies; `containment.promote`/`hardlink`/`unlink_at`/`open_staging` carry exact contracts; the `object_inserted` three-way branch and the `object_updated` seal branch are written out verbatim.

**Type consistency:** `Media` is the shared base; `BytesMedia(Media)` unchanged; `FileMedia(Media)`. `Store.new_upload_media(name, id)/new_staging_media(name, token) -> Media` (base returns `BytesMedia`). `Object.init`/`__init__` accept any `Media` by identity; only raw `bytes` wrap (driven by an `_OtherMedia(Media)` RED test, Task 1). `FileMedia.finalize((dst_dir_fd, dst_name))`/`link_into((dst_dir_fd, dst_name))` match `containment.promote`/`hardlink(src_dir_fd, src_name, dst_dir_fd, dst_name)` and the `FileStore` call sites. `is_finalized` is a real property (`self._staging is None`) referenced consistently — no `is_finalized` placeholder. `FileMedia.chunks(begin, end, size)` yields `bytes` with `BytesMedia`-identical boundaries (empty slice → nothing); Task 6 streams via the existing `_stream_media(self.media, max(0, begin), end)` helper (size defaulted), not a bare `chunks(begin, end)`. `len(media)`/`media.crc32c()`/`media.md5()` are O(1) on both backends, matching `gcs/object.py:135-137` and the upload/rewrite offset math.
