# FileStore (`Store` → on-disk layout) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `FileStore` implementation of the phase-2 `Store` seam that persists bucket/object/folder metadata (and, at phase 4, small-object bytes still carried by `BytesMedia`) to a GCS-shaped on-disk tree, proving the file backend produces byte-identical external behavior to the memory backend (B ≡ C) with exactly one reviewed, deliberate difference.

**Architecture:** Three pure/I/O-light units — `testbench/pathing.py` (untrusted-name → on-disk-name policy: bucket validation, reversible escape/unescape, overflow classification, pure containment predicate), `testbench/containment.py` (fd-based `openat`/`O_NOFOLLOW` primitives + realpath-verified constrained-`rmtree` backstop), `testbench/sidecar.py` (versioned `MessageToJson` envelope, atomic fd-based `os.replace`) — are proven adversarially in isolation before `testbench/filestore.py` composes them into a thin translator from each `Store` notification to one *contained* filesystem op over the `.gcs/` layout. Every write walks a per-bucket root dirfd with `O_NOFOLLOW` on each component, so a planted/swapped symlink cannot defeat containment even under concurrency. A startup tree-scan rebuilds the in-memory index from sidecars; `Database.init` selects `NullStore` vs `FileStore` from `TESTBENCH_STORE`/`TESTBENCH_ROOT`, and when the file backend is live **both listeners are forced to loopback** so the traversal-capable disk-writing backend is never network-exposed. Bytes remain `BytesMedia`; streaming is phase 5.

**Tech Stack:** Python 3.8–3.12, stdlib only for the new backend (`os`, `hashlib`, `json`, `pathlib`, `shutil`, `tempfile`) plus the already-present `crc32c` and `protobuf` (`json_format.MessageToJson`); `hypothesis` and `coverage` as dev-only deps in `flake.nix`; the phase-1 conformance harness (`tests/conformance/`).

## Global Constraints

- **Zero new runtime dependencies.** `setup.py` stays as-is. The file backend uses only stdlib + already-present `crc32c`/`protobuf`. `hypothesis`/`coverage` are dev deps in `flake.nix` only. **Provisioning already done** (env-prep commit before Task 1): `hypothesis<6.113` (last line supporting the Python-3.8 CI floor) added to `flake.nix`'s devShell venv install list, to the CI `python-tests` `pip install` step in `.github/workflows/build.yaml`, and to the local `.venv`. Do NOT add it to `setup.py`. Use `.venv/bin/python -m pytest`, `.venv/bin/isort`, `.venv/bin/black`, `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness` as the toolchain (nothing is on the bare `PATH`).
- **Python floor is 3.8.** Every new file must parse under `ast.parse(feature_version=(3, 8))`. CI runs a 3.8–3.12 matrix. All fd-based primitives use APIs present since 3.3 (`os.open(..., dir_fd=)`, `os.replace(..., src_dir_fd=, dst_dir_fd=)`, `os.O_NOFOLLOW`, `os.O_DIRECTORY`).
- **The harness measures external behavior only.** Nothing in `tests/conformance/` may import `testbench`/`gcs` internals (`emulator.py` excepted). `FileStore`/`pathing`/`containment`/`sidecar` live in `testbench/`, never in `tests/conformance/`.
- **`--regenerate` NEVER turns a red gate green.** For the memory backend the harness diff must stay **empty** and the golden digest `8eda6110f35c511b9afc7588bac771a5e18cc3b54b0dfa89eef960deba0c2fbb` unchanged, except for one reviewed `--regenerate` (Task 2) that *adds* new pinning interactions without altering the 112 existing ones. For the file backend, B ≡ C is enforced by a **byte-exact masked comparison** (Task 1): allow-listed interaction blocks are replaced by a canonical token in *both* golden and observed, then the full remaining text is compared byte-for-byte — strictly as strong as the memory leg's `expected == observed`, never a per-label subset. The single deliberate difference (file rejects `../../etc/passwd`, memory accepts it) is recorded in a committed, mutation-checked allow-list (`tests/conformance/allowlist.json`), never via `--regenerate`.
- **`gcs/bucket.py` `__validate_json_bucket_name` is DELIBERATELY LEFT UNCHANGED.** Fixing its `if "." in bucket_name:` bypass (bucket.py:62) would make the *memory* backend reject `../../etc/passwd`, breaking A ≡ B and moving the golden digest. The file backend's own validation (spec Security rule 4: "the only check") closes the gap for `FileStore`; the carried-forward defect stays scoped to the file backend by design.
- **The file backend never listens off-loopback.** The traversal-capable, disk-writing backend must not become network-reachable. When `TESTBENCH_STORE=file`, the gRPC listener binds `127.0.0.1` (not `0.0.0.0`, grpc_server.py:1348) and the REST bootstrap refuses a non-loopback `--bind` host. This is pulled **into phase 4** (an exit-gate item), not deferred to phase 6, because Task 9 is the moment the file backend first becomes reachable. Spec Security rule 6 is therefore satisfied for the file backend now; phase 6 only generalizes the wiring.
- **POSIX fd-based containment is required for the file backend.** `FileStore.__init__` asserts `os.open in os.supports_dir_fd` and `hasattr(os, "O_NOFOLLOW")`; it raises loudly rather than silently degrading to a symlink-following open. CI/target is Linux; the macOS capture host also satisfies this. Windows is out of scope for the file backend (memory backend unaffected).
- **Mutation-check every guard clause.** After a guard passes, reintroduce the specific defect it guards and confirm the test fails. Each *clause* of a multi-clause guard (`validate_bucket_name`, `_needs_overflow`, containment, collision) gets its own reintroduction, not a representative subset. A test that passes against the reintroduced bug is testing nothing.
- **Interpreter/OS/library-internals hazard.** Ask of every recorded or asserted value whether it depends on the OS, the interpreter, or a library's internals. Filesystem **case-sensitivity** and `NAME_MAX` differ between the macOS capture host and Linux CI. Collision detection therefore reflects the *target* filesystem's identity rule (`st_dev`/`st_ino` inode identity + true-name compare), never a string comparison that assumes one policy. Tests that depend on case-sensitivity probe the temp root and branch. Run `make verify-linux`; treat CI as authoritative.
- **Lock discipline.** Store notifications fire while `Database` holds the lock guarding the reported state (`_resources_lock` for bucket/object, `_folders_lock` for folders). `FileStore` folder handlers must touch only `.gcs/folders/` and never re-enter `Database` for resource state, or they invert `clear()`'s resources→folders order and can deadlock (store.py:60-74). Bulk media I/O opens/writes/renames outside the `Database` lock is fine; only the small metadata `os.replace` is quick and stays under it.
- **Formatting is `isort` then `black`, in that order** (`isort==5.12.0`, `black==22.3.0`). CI enforces the combination (`isort … && black … && git diff --exit-code`).
- **Single gunicorn worker, `--reload` on.** Never edit a `.py` file while a harness run or emulator-backed test is in flight; it restarts the worker mid-trace and wipes state.

### Configuration-A baseline (the irreproducible artifact)

- Configuration A (pristine upstream, no seam): commit `e8c8507`. A ≡ B re-proven on the current goldens; golden digest (sha256 of `rest.json`+`grpc.json`+`faults.json` concatenated): `8eda6110f35c511b9afc7588bac771a5e18cc3b54b0dfa89eef960deba0c2fbb`.
- Task 2 legitimately changes this digest **once** (adds pinning interactions). Record the new digest in the handoff; every subsequent memory-backend task must preserve *that* value.

### The phase-4 exit gate (what "done" means)

From the spec's per-phase gates (row 4), phase 4 is green when **all** hold:

1. `PYTHONPATH=. python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py` is green under **both** `TESTBENCH_TEST_STORE=memory` and `TESTBENCH_TEST_STORE=file` (Mechanism 1), and under the file leg a representative REST endpoint operation **provably wrote to the on-disk tree** (the disk-touch guard test passes and is mutation-checked).
2. `PYTHONPATH=. python -m tests.conformance.harness --store memory` prints `OK` for all three traces with an **empty diff** (digest = the Task-2 pinned value).
3. `PYTHONPATH=. python -m tests.conformance.harness --store file` prints `OK` for all three traces via the **byte-exact masked comparison**, diverging **only** on the one allow-listed `create-bucket-traversal` label.
4. When `TESTBENCH_STORE=file`, **no listener binds `0.0.0.0`**: the gRPC bind host is loopback and the REST bootstrap refuses a non-loopback bind. Adversarial test proves it and is mutation-checked.
5. `nix develop --command make verify-linux` is green on both legs.
6. The property/adversarial suites (`tests/test_pathing.py`, `tests/test_containment.py`, `tests/test_sidecar.py`, `tests/test_filestore.py`, `tests/test_filestore_scan.py`) pass, each guard **clause** mutation-checked, including the planted-symlink write-path test and the restore-reconciliation soft-delete test.
7. `tests/media_call_sites.txt` covers `FileStore`'s `blob.media` read; the coverage gate passes.
8. Nothing in `tests/conformance/` imports `gcs`/`testbench` except `emulator.py`; `setup.py` unchanged.

---

## File Structure

- **Create `testbench/pathing.py`** — pure, no I/O: `validate_bucket_name(name)` (the file backend's SOLE bucket check, raises `ValueError`), reversible `escape(object_name)`/`unescape(escaped)`, `classify(object_name)` → `("natural", relpath)` or `("overflow", sha256hex)`, and `is_contained(path, root)`. One responsibility: decide the reversible on-disk name for any caller name.
- **Create `testbench/containment.py`** — fd-based filesystem containment primitives: `assert_posix_support()`, `assert_within(path, root)` (realpath), `open_dir_nofollow(dir_fd, name)`, `open_bucket_root_fd(root_fd, short)`, `walk_dirs(dir_fd, parts, create)`, `safe_open(dir_fd, name, flags, mode)` (`openat`+`O_NOFOLLOW`), `write_bytes_atomic(dir_fd, name, data)`, `constrained_rmtree(path, root, index_names)` (realpath parent check, rejects any symlink component). One responsibility: keep every resolved path provably in-root even if `pathing` has a bug or a symlink is swapped in mid-op.
- **Create `testbench/sidecar.py`** — versioned envelope `{schema_version, kind, name, proto}` over `json_format.MessageToJson(Object|Bucket)`; `dump(proto, true_name)`/`load(text)`; atomic **fd-based** write `write_atomic(dir_fd, filename, text)` via temp (O_EXCL|O_NOFOLLOW) + `os.replace(..., src_dir_fd, dst_dir_fd)`; loud failure on truncated/corrupt JSON. One responsibility: persist/restore one proto atomically and inside containment.
- **Create `testbench/filestore.py`** — `FileStore(Store)`: maps all 12 notifications (11 existing + new `bucket_updated`) plus the `validate_bucket_name(name, context)` pre-commit hook to *contained* filesystem ops over the `.gcs/` layout, composing the three units; write-time collision guard; `rebuild_index(database)` drives the startup tree-scan + inode-based collision detection. No name/path/security logic of its own.
- **Create `conftest.py`** (repo root) — Mechanism-1 backend switch keyed on `TESTBENCH_TEST_STORE=memory|file`; in file mode (a) overrides `Database.init` to inject `FileStore(root)` **only when the caller passed no store**, and (b) swaps the store on the live `testbench.rest_server.db` singleton so Flask-client endpoint tests actually hit disk; exposes the per-test root.
- **Create `tests/conformance/allowlist.json`** — reviewed label→justification of deliberate B≠C divergences; phase 4 holds exactly one entry.
- **Create `tests/test_pathing.py` / `test_containment.py` / `test_sidecar.py` / `test_filestore.py` / `test_filestore_scan.py`** — property (hypothesis) + adversarial + direct-drive suites.
- **Create `tests/conformance/test_harness_overlay.py`** — unit test that the byte-exact masked overlay tolerates a listed label, fails any unlisted diff (inside or outside a block) and any duplicate/None label, and flags a stale entry. Inputs are REAL serialized interaction blocks built via `harness.serialize`.
- **Modify `testbench/store.py`** — add `bucket_updated(bucket)` and `validate_bucket_name(name, context=None)` as no-ops on `Store` base (`NullStore` inherits unchanged); extend the trust-boundary docstring.
- **Modify `testbench/database.py`** — add `do_update_bucket(bucket_name, *, update_fn, context, preconditions)` firing `bucket_updated`; call `self._store.validate_bucket_name(bucket.metadata.name, context)` pre-commit in `insert_bucket`; fire `object_purged` for the ORIGINAL restored generation in `restore_object`; construct `FileStore` and hydrate when `TESTBENCH_STORE=file`.
- **Modify `testbench/rest_server.py`** — select the store from env at the module `db` singleton; route `bucket_update`, `bucket_patch`, bucket ACL insert/update/patch/delete, `defaultObjectAcl` insert/update/patch/delete, `setIamPolicy`, `lockRetentionPolicy` through `do_update_bucket`.
- **Modify `testbench/grpc_server.py`** — route `UpdateBucket`, `LockBucketRetentionPolicy`, `SetIamPolicy` through `do_update_bucket`; derive the bind host from `_bind_host()` (loopback under file backend) instead of the hardcoded `0.0.0.0`.
- **Modify `testbench_run.py`** — refuse a non-loopback `--bind` host when `TESTBENCH_STORE=file`.
- **Modify `tests/conformance/harness.py`** — add `--store {memory,file}`; thread `store="memory"` default through `capture`/`verify`; in file mode compare via the byte-exact masked overlay.
- **Modify `tests/conformance/emulator.py`** — `Emulator(store="memory", root=None)`; make `_child_env` an instance method and update its call site; set `TESTBENCH_STORE`/`TESTBENCH_ROOT` in the child env in file mode; per-run temp root created before launch and `rmtree`d on teardown.
- **Modify `tests/conformance/trace_rest.py` + `trace_grpc.py` + `golden/*.json`** — add the dotted/traversal `create-bucket` pin (Task 2).
- **Modify `tests/test_store.py`** — extend `RecordingStore` with `bucket_updated`/`validate_bucket_name`; keep the memory-accepts pin, add the file-backend-rejects assertion.
- **Modify `tests/media_call_sites.txt`** — add `FileStore`'s `blob.media` read site.
- **Modify `.github/workflows/build.yaml`** — add the file-backend suite leg and the file-config harness leg.

Each task ends with the safety gate below. **Pure-refactor tasks (1–3):** memory harness diff EMPTY. **New-backend tasks (4–10):** unit/property suites green, and from Task 9 on, B ≡ C on the file leg with the single allow-list entry.

---

### Task 1: B ≡ C verification scaffolding (harness `--store`, byte-exact masked overlay, file-config `Emulator`)

**Files:**
- Modify: `tests/conformance/harness.py` (`capture` :65-67, `verify` :70-94, `main` :188-212)
- Modify: `tests/conformance/emulator.py` (`_child_env` :115-140 → instance method; call site :189; `__init__` :157-179; `_terminate` :244-288)
- Create: `tests/conformance/allowlist.json`, `tests/conformance/test_harness_overlay.py`

**Interfaces:**
- Produces: `Emulator(store="memory"|"file", root=None)`; in file mode sets `TESTBENCH_STORE=file` + `TESTBENCH_ROOT=<per-run tmpdir>` in the child env and `rmtree`s an owned root on teardown. `capture(name, store="memory")` and `verify(name, store="memory")` carry the store; `main` passes `args.store` to both non-regenerate call sites, while the regenerate branch keeps the `"memory"` default. In file mode `verify` uses `diff_with_allowlist`: allow-listed interaction blocks are masked to a canonical token in **both** golden and observed and the full remaining text is compared byte-for-byte; a listed label that did not diverge is reported by `stale_allowlist_labels`; a duplicate or unlabeled interaction block is a hard error.

- [ ] **Step 1: Write the failing overlay unit test (REAL serialized blocks)**

The inputs MUST match the golden framing (`_INTERACTION_START = ^    \{$`, `_LABEL_LINE`, `_INTERACTION_END = ^    \},?$`), so build them with `harness.serialize` on a `{"interactions": [...]}` record — the exact shape and indentation of the real goldens — not hand-typed pseudo-lines.

```python
# tests/conformance/test_harness_overlay.py
import json
import os
import unittest

from tests.conformance import harness

HERE = os.path.dirname(os.path.abspath(__file__))


def _trace(interactions):
    # Same serializer the goldens use -> 4-space-indented "{...}" blocks with a
    # 6-space "label" line, exactly what _INTERACTION_START/_LABEL_LINE match.
    return harness.serialize({"interactions": interactions})


class TestAllowListOverlay(unittest.TestCase):
    def test_allowlist_file_exists_and_is_a_flat_mapping(self):
        with open(os.path.join(HERE, "allowlist.json")) as fh:
            data = json.load(fh)
        self.assertIsInstance(data, dict)
        for label, justification in data.items():
            self.assertIsInstance(label, str)
            self.assertIsInstance(justification, str)
            self.assertTrue(justification.strip(), "empty justification for %r" % label)

    def test_masked_overlay_tolerates_only_listed_labels(self):
        golden = _trace([{"label": "create-bucket", "status": 200},
                         {"label": "get-bucket", "status": 200}])
        observed = _trace([{"label": "create-bucket", "status": 400},
                           {"label": "get-bucket", "status": 200}])
        # listed -> masked equal -> empty residual
        self.assertEqual("", harness.diff_with_allowlist(golden, observed,
                                                         {"create-bucket": "x"}))
        # unlisted -> byte-exact masked compare fails
        self.assertNotEqual("", harness.diff_with_allowlist(golden, observed, {}))

    def test_divergence_outside_any_interaction_block_still_fails(self):
        # A change in the JSON wrapper (outside every "{...}" block) must fail
        # even when the differing labels are allow-listed -- the memory leg's
        # byte-for-byte guarantee, preserved.
        golden = _trace([{"label": "create-bucket", "status": 200}])
        observed = golden.replace('"interactions"', '"INTERACTIONS"')
        self.assertNotEqual("", harness.diff_with_allowlist(golden, observed,
                                                           {"create-bucket": "x"}))

    def test_duplicate_or_unlabeled_block_is_a_hard_error(self):
        dup = _trace([{"label": "x", "status": 1}, {"label": "x", "status": 2}])
        with self.assertRaises(ValueError):
            harness.diff_with_allowlist(dup, dup, {})
        nolabel = _trace([{"status": 1}])
        with self.assertRaises(ValueError):
            harness.diff_with_allowlist(nolabel, nolabel, {})

    def test_stale_allowlist_entry_is_reported(self):
        same = _trace([{"label": "create-bucket", "status": 200}])
        self.assertIn("create-bucket",
                      harness.stale_allowlist_labels(same, same, {"create-bucket": "x"}))
```

- [ ] **Step 2: Run to see it fail**

Run: `PYTHONPATH=. python -m pytest tests/conformance/test_harness_overlay.py -q`
Expected: FAIL — `AttributeError: module 'tests.conformance.harness' has no attribute 'diff_with_allowlist'` (and the `allowlist.json` open raises `FileNotFoundError`).

- [ ] **Step 3: Implement the byte-exact masked overlay in `harness.py` and create an empty allow-list**

Create `tests/conformance/allowlist.json` with `{}`. Then add to `harness.py`:

```python
# tests/conformance/harness.py -- byte-exact masked overlay. `verify` uses these
# in file mode; the memory path keeps its existing `expected == observed`.

_MASK = '    {"__MASKED_ALLOWLISTED_INTERACTION__": %r},\n'


def _load_allowlist():
    with open(os.path.join(_HERE, "allowlist.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _segments(text):
    """Split `text` into ('lit', str) and ('int', label, block) segments such
    that concatenating the raw pieces reproduces `text` byte-for-byte. Only a
    top-level interaction block (4-space "{...}") is an 'int'."""
    segs, lit, cur, label = [], [], None, None
    for line in text.splitlines(True):
        if cur is None and _INTERACTION_START.match(line):
            if lit:
                segs.append(("lit", "".join(lit)))
                lit = []
            cur, label = [line], None
        elif cur is not None:
            cur.append(line)
            m = _LABEL_LINE.match(line)
            if m:
                label = m.group("label")
            if _INTERACTION_END.match(line):
                segs.append(("int", label, "".join(cur)))
                cur = None
        else:
            lit.append(line)
    if cur is not None:  # unterminated block: keep bytes, surfaces as a diff
        lit.extend(cur)
    if lit:
        segs.append(("lit", "".join(lit)))
    return segs


def _check_labels_unique(text):
    labels = [s[1] for s in _segments(text) if s[0] == "int"]
    if None in labels:
        raise ValueError("interaction block with no label in trace")
    dupes = sorted({l for l in labels if labels.count(l) > 1})
    if dupes:
        raise ValueError("duplicate interaction label(s): %r" % dupes)


def _mask(text, allowlist):
    out = []
    for seg in _segments(text):
        if seg[0] == "lit":
            out.append(seg[1])
        else:
            _, label, block = seg
            out.append(_MASK % label if label in allowlist else block)
    return "".join(out)


def diff_with_allowlist(expected, observed, allowlist):
    """Empty string iff, after masking allow-listed interaction blocks in BOTH
    sides, the full texts are byte-identical. Strictly as strong as the memory
    leg's equality on every byte outside an allow-listed block."""
    _check_labels_unique(expected)
    _check_labels_unique(observed)
    me, mo = _mask(expected, allowlist), _mask(observed, allowlist)
    if me == mo:
        return ""
    return "".join(
        difflib.unified_diff(
            me.splitlines(True), mo.splitlines(True),
            fromfile="golden (masked)", tofile="observed (masked)",
        )
    )


def _blocks(text):
    return {s[1]: s[2] for s in _segments(text) if s[0] == "int"}


def stale_allowlist_labels(expected, observed, allowlist):
    """Allow-listed labels whose block did NOT diverge -- a stale entry would
    silently absorb a future regression, so verify() fails on any."""
    exp, obs = _blocks(expected), _blocks(observed)
    return sorted(l for l in allowlist if exp.get(l) == obs.get(l))
```

Thread the store through the run functions (defaults keep the regenerate branch working):

```python
def capture(name, store="memory"):
    with Emulator(store=store) as emulator:
        return TRACES[name](emulator)


def verify(name, store="memory"):
    path = golden_path(name)
    if not os.path.exists(path):
        return "missing golden %s; run with --regenerate" % path
    with open(path, "r", encoding="utf-8") as handle:
        expected = handle.read()
    observed = serialize(capture(name, store))
    if store == "memory":
        if expected == observed:
            return ""
        # ... existing unified-diff + _annotate_hunks_with_labels path, unchanged ...
    allowlist = _load_allowlist()
    residual = diff_with_allowlist(expected, observed, allowlist)
    stale = stale_allowlist_labels(expected, observed, allowlist)
    if stale:
        residual += "\nstale allow-list entries (did not diverge): %r\n" % stale
    return residual
```

In `main`, add `parser.add_argument("--store", choices=("memory", "file"), default="memory")`; the regenerate branch keeps `capture(name)` (memory-only); the verify loop calls `verify(name, args.store)`.

- [ ] **Step 4: Extend `Emulator` for the file config (instance `_child_env`, volatile-root-safe)**

Two concrete edits — do BOTH:
1. Move the module-level `def _child_env():` (emulator.py:115) into the `Emulator` class as `def _child_env(self):`, reading `self._store`/`self._root`.
2. Change its only call site in `__enter__` (emulator.py:189, currently `env = _child_env()`) to `env = self._child_env()`.

```python
# __init__ gains store/root:
def __init__(self, rest_port=None, grpc_port=None, store="memory", root=None):
    ...
    self._store = store
    self._own_root = root is None and store == "file"
    self._root = root if root is not None else (
        tempfile.mkdtemp(prefix="testbench-conf-") if store == "file" else None
    )

# inside self._child_env(), after env.update(_PINNED_ENV):
    if self._store == "file":
        env["TESTBENCH_STORE"] = "file"
        env["TESTBENCH_ROOT"] = self._root

# _terminate(): after the process is reaped and logs snapshotted:
    if self._own_root and self._root is not None:
        import shutil
        shutil.rmtree(self._root, ignore_errors=True)
```

- [ ] **Step 5: Guard the goldens against carrying a volatile-root token (regression guard, not a leak proof)**

This test guards only that the committed goldens carry no root token — a regression sink if a future capture leaks one. The *actual* proof that the file backend does not leak its temp root into an observed response is the file-leg harness diff (a leaked absolute path reddens some label); that observed-capture grep is added in Task 9 once `FileStore` exists and file mode can be captured.

```python
    def test_committed_goldens_carry_no_volatile_root_token(self):
        for name in ("rest", "grpc", "faults"):
            with open(harness.golden_path(name), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("TESTBENCH_ROOT", text)
            self.assertNotIn("testbench-conf-", text)
```

- [ ] **Step 6: Run the gate green and mutation-check the overlay**

Run:
```bash
PYTHONPATH=. python -m pytest tests/conformance/test_harness_overlay.py -q
PYTHONPATH=. python -m tests.conformance.harness --store memory   # OK all three, empty diff, digest unchanged
```
Mutation-checks (each reverted after):
- In `_mask`, drop the `if label in allowlist` branch (never mask). Expected: `test_masked_overlay_tolerates_only_listed_labels` FAILS (listed case no longer empty).
- Make `_check_labels_unique` `return` immediately. Expected: `test_duplicate_or_unlabeled_block_is_a_hard_error` FAILS.
- Make `stale_allowlist_labels` `return []`. Expected: `test_stale_allowlist_entry_is_reported` FAILS.
- Change `_mask` to skip the `'lit'` segments (compare only blocks). Expected: `test_divergence_outside_any_interaction_block_still_fails` FAILS.

- [ ] **Step 7: Format and commit**

```bash
PYTHONPATH=. python -c "import ast; [ast.parse(open(f).read(), feature_version=(3,8)) for f in ('tests/conformance/harness.py','tests/conformance/emulator.py')]"
isort --quiet tests/conformance/harness.py tests/conformance/emulator.py tests/conformance/test_harness_overlay.py && black --quiet tests/conformance/harness.py tests/conformance/emulator.py tests/conformance/test_harness_overlay.py
git add tests/conformance/harness.py tests/conformance/emulator.py tests/conformance/allowlist.json tests/conformance/test_harness_overlay.py
git commit -m "test(conformance): add --store file leg and byte-exact masked B-vs-C overlay"
```

**Safety gate:** pure scaffolding — memory harness empty diff, digest `8eda6110…` unchanged; the file leg is wired but not yet exercised (no `FileStore`). Overlay is byte-exact and mutation-checked on all four failure modes.

---

### Task 2: Pin the dotted/traversal bucket name as currently-ACCEPTED

**Files:**
- Modify: `tests/conformance/trace_rest.py` (bucket section, after `create-versioned-bucket` at :110-116), `tests/conformance/trace_grpc.py` (bucket-create section)
- Modify: `tests/conformance/golden/rest.json`, `golden/grpc.json` (via reviewed `--regenerate`)

**Interfaces:**
- Consumes: the `api(label, method, path, **kwargs)` helper (trace_rest.py:75) and the gRPC `CreateBucket` trace pattern.
- Produces: one new interaction per transport pinning that the **memory** backend ACCEPTS `projects/_/buckets/../../etc/passwd`. This is the surface phase 4's file-backend validation will make diverge — baselined before it can be recorded honestly.

- [ ] **Step 1: Add the REST pin interaction**

```python
# tests/conformance/trace_rest.py, immediately after the create-versioned-bucket call:
# Pins the live bucket-name validation bypass (gcs/bucket.py:62: the `"." in
# name` branch checks only length, never the char-class regex) as CURRENT,
# ACCEPTED memory-backend behaviour. Phase 4's FileStore.validate_bucket_name
# makes the *file* backend reject this; that divergence is the single reviewed
# entry in tests/conformance/allowlist.json. Do NOT "fix" the validator -- that
# would move A and redden A == B.
api(
    "create-bucket-traversal",
    "POST",
    "/storage/v1/b",
    params={"project": "test-project"},
    json={"name": "../../etc/passwd"},
)
```

- [ ] **Step 2: Add the gRPC pin interaction**

In `trace_grpc.py`, mirror the existing `CreateBucket` call with `bucket_id="../../etc/passwd"` (parent `projects/_`), recorded under label `create-bucket-traversal`. Match the surrounding record helper exactly so only a new interaction is added. (Phase 4 will make this a clean `INVALID_ARGUMENT`, not an uncaught `UNKNOWN` — see Task 3 context threading.)

- [ ] **Step 3: Confirm the memory backend accepts it, then regenerate (reviewed)**

```bash
PYTHONPATH=. python -m tests.conformance.harness --store memory --trace rest   # FAIL: unknown interaction create-bucket-traversal
PYTHONPATH=. python -m tests.conformance.harness --regenerate --trace rest --trace grpc
git --no-pager diff --stat tests/conformance/golden/
git --no-pager diff tests/conformance/golden/rest.json | grep -E '^-' | grep -v '^---'   # expect empty (additions only)
```

- [ ] **Step 4: Re-prove A ≡ B against pristine main and record the new digest**

The config-A worktree at `e8c8507` predates the `--store` flag Task 1 added, so it must run its OWN harness with **no** `--store` argument (its `main()` would reject `--store`). Copy only the traces + goldens into it.

```bash
git stash --include-untracked
git worktree add /tmp/configA e8c8507
cp -r tests/conformance/trace_rest.py tests/conformance/trace_grpc.py tests/conformance/golden /tmp/configA/tests/conformance/
( cd /tmp/configA && PYTHONPATH=. python -m tests.conformance.harness )   # NO --store; expect OK all three
git worktree remove /tmp/configA --force
git stash pop
cat tests/conformance/golden/rest.json tests/conformance/golden/grpc.json tests/conformance/golden/faults.json | shasum -a 256
```
Confirm the worktree run printed `OK` for all three traces before recording the new digest in the handoff (it supersedes `8eda6110` for later memory-backend tasks).

- [ ] **Step 5: Commit (standalone, reviewable)**

```bash
git add tests/conformance/trace_rest.py tests/conformance/trace_grpc.py tests/conformance/golden/
git commit -m "test(conformance): pin dotted/traversal bucket name as currently-accepted (memory)

Adds create-bucket-traversal to the REST and gRPC traces. The memory backend
accepts projects/_/buckets/../../etc/passwd today (gcs/bucket.py:62 bypass);
this baselines it so phase 4's file-backend rejection can be recorded via the
allow-list. Additions only; all 112 pre-existing interactions byte-identical.
New golden digest: <paste from Step 4>."
```

**Safety gate:** reviewed additive `--regenerate` only; existing interactions byte-identical; A ≡ B re-proven via the config-A worktree's own flag-less harness; new digest recorded.

---

### Task 3: Extend the `Store` seam (zero-diff memory refactor): `bucket_updated`, `validate_bucket_name`, restore purge signal

**Files:**
- Modify: `testbench/store.py` (base `Store` :95-156, `NullStore` :158-160)
- Modify: `testbench/database.py` (`insert_bucket` :120-128, `restore_object` :600-643; add `do_update_bucket` after `do_update_object` :598)
- Modify: `testbench/rest_server.py` (`bucket_update` :249-261, `bucket_patch` :264-276, bucket ACL :308-354, `defaultObjectAcl` :372-426, `bucket_set_iam_policy` :468-476, `bucket_lock_retention_policy` :488-500)
- Modify: `testbench/grpc_server.py` (`LockBucketRetentionPolicy` :353-381, `SetIamPolicy` :389-392, `UpdateBucket` :404-479)
- Modify: `tests/test_store.py` (`RecordingStore` :42-110)

**Interfaces:**
- Produces: `Store.bucket_updated(self, bucket)` and `Store.validate_bucket_name(self, name, context=None)` (both no-op on base/`NullStore`); `Database.do_update_bucket(self, bucket_name, *, update_fn, context=None, preconditions=[])` — under `_resources_lock`: `get_bucket(...)`, `update_fn(bucket)`, `self._store.bucket_updated(bucket)`, return bucket. `insert_bucket` calls `self._store.validate_bucket_name(bucket.metadata.name, context)` before the `in self._buckets` check (so a rejection is a clean 4xx on REST and a clean `INVALID_ARGUMENT` on gRPC, not a post-commit 500 or an uncaught `UNKNOWN`). `restore_object` fires `object_purged` for the ORIGINAL soft-deleted generation (the function's `generation` argument).

- [ ] **Step 1: Write the failing `RecordingStore` seam test**

```python
# tests/test_store.py -- add to RecordingStore:
    def bucket_updated(self, bucket):
        self.calls.append(("bucket_updated", bucket.metadata.name))

    def validate_bucket_name(self, name, context=None):
        self.calls.append(("validate_bucket_name", name))

# new test class:
class TestBucketUpdatedSeam(unittest.TestCase):
    def _routes_bucket_updated(self, mutate):
        store = RecordingStore()
        db = testbench.database.Database.init(store=store)
        bucket = _make_bucket("bucket-name")
        db.insert_bucket(bucket, None)
        store.calls.clear()
        db.do_update_bucket(
            "projects/_/buckets/bucket-name", update_fn=mutate, context=None
        )
        return [c for c in store.calls if c[0] == "bucket_updated"]

    def test_do_update_bucket_notifies_exactly_once(self):
        seen = self._routes_bucket_updated(
            lambda b: setattr(b.metadata, "metageneration", 7))
        self.assertEqual(
            [("bucket_updated", "projects/_/buckets/bucket-name")], seen)

    def test_insert_bucket_validates_name_before_commit(self):
        store = RecordingStore()
        db = testbench.database.Database.init(store=store)
        db.insert_bucket(_make_bucket("bucket-name"), None)
        self.assertEqual(store.calls[0],
                         ("validate_bucket_name", "projects/_/buckets/bucket-name"))
        self.assertEqual(store.calls[1][0], "bucket_inserted")
```

- [ ] **Step 2: Run to see it fail**

Run: `PYTHONPATH=. python -m pytest tests/test_store.py -q -k "BucketUpdatedSeam"`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'do_update_bucket'`.

- [ ] **Step 3: Add the base-class methods and the Database mutator/hooks**

```python
# testbench/store.py -- on Store (before `cleared`):
    def bucket_updated(self, bucket):
        """Bucket metadata changed in place (PUT/PATCH, ACL, defaultObjectAcl,
        setIamPolicy, lockRetentionPolicy, gRPC UpdateBucket). A persistent
        store re-serializes bucket.json. Idempotent: routing that reaches here
        without a net change is acceptable."""

    def validate_bucket_name(self, name, context=None):
        """Pre-commit hook: reject a bucket name BEFORE Database commits it, so
        a rejection is a clean 4xx (REST) / INVALID_ARGUMENT (gRPC) rather than
        a post-commit 500 that leaves index and disk divergent. `name` is
        proto-form; `context` is the gRPC servicer context (None on REST) so
        the rejection aborts with the correct status on both transports. No-op
        here; FileStore enforces spec Security rules 2/4."""
```

```python
# testbench/database.py -- insert_bucket, add as the FIRST line under the lock:
        with self._resources_lock:
            self._store.validate_bucket_name(bucket.metadata.name, context)
            if bucket.metadata.name in self._buckets:
                ...

# after do_update_object:
    def do_update_bucket(
        self, bucket_name, *, update_fn, context=None, preconditions=[]
    ):
        # Mirrors do_update_object: notify unconditionally after update_fn.
        # Every bucket-metadata mutator (REST bucket PUT/PATCH, bucket ACL,
        # defaultObjectAcl, setIamPolicy, lockRetentionPolicy; gRPC
        # UpdateBucket/LockBucketRetentionPolicy/SetIamPolicy) funnels here so
        # a Store observes bucket changes -- previously they bypassed Database
        # entirely (spec carried-forward defect).
        with self._resources_lock:
            bucket = self.get_bucket(bucket_name, context, preconditions)
            if bucket is None:
                return None
            update_fn(bucket)
            self._store.bucket_updated(bucket)
            return bucket

# restore_object -- the `generation` PARAMETER is the ORIGINAL soft-deleted
# generation (blob.metadata.generation is incremented to generation+1 before
# insert_object). Fire the purge for that original before dropping the stale
# on-disk copy. Insert the two lines immediately before
# __remove_restored_soft_deleted_object (:640):
                self.insert_object(bucket_name, blob, context, preconditions)
                # The restored generation left a stale copy in the
                # soft-deleted set on disk; Database drops it in memory, and now
                # also signals object_purged for the ORIGINAL generation so a
                # FileStore removes .gcs/soft_deleted/<generation>. This is the
                # SINGLE reconciliation mechanism for restore (see Task 7: no
                # duplicate cleanup in object_inserted). No-op on NullStore ->
                # zero memory diff.
                self._store.object_purged(
                    self.__bucket_key(bucket_name, context), object_name, generation
                )
                self.__remove_restored_soft_deleted_object(
                    bucket_name, object_name, generation, context
                )
```

- [ ] **Step 4: Route the REST handlers through `do_update_bucket`**

Wrap each in-place mutation in a closure; handlers that must return a value produced by the mutation capture it via a mutable holder cell. Worked example for `bucket_update`:

```python
@gcs.route("/b/<bucket_name>", methods=["PUT"])
@retry_test(method="storage.buckets.update")
def bucket_update(bucket_name):
    db.insert_test_bucket()
    request = flask.request
    bucket = db.do_update_bucket(
        bucket_name,
        update_fn=lambda b: b.update(request, None),
        context=None,
        preconditions=testbench.common.make_json_bucket_preconditions(request),
    )
    projection = testbench.common.extract_projection(flask.request, "full", None)
    fields = flask.request.args.get("fields", None)
    return testbench.common.filter_response_rest(bucket.rest(), projection, fields)
```

Worked example for a return-carrying handler (`default_object_acl_insert`; apply the same holder-cell shape to the other three DOACL routes, the four bucket-ACL routes, and any ACL route whose response is the ACL object rather than the bucket):

```python
@gcs.route("/b/<bucket_name>/defaultObjectAcl", methods=["POST"])
@retry_test(method="storage.default_object_acl.insert")
def default_object_acl_insert(bucket_name):
    db.insert_test_bucket()
    request = flask.request
    holder = {}
    db.do_update_bucket(
        bucket_name,
        update_fn=lambda b: holder.__setitem__("acl", b.insert_default_object_acl(request, None)),
        context=None,
    )
    return holder["acl"]
```

Apply the same shape to `bucket_patch` (`lambda b: b.patch(request, None)`), `bucket_set_iam_policy` (`lambda b: b.set_iam_policy(request, None)`), and `bucket_lock_retention_policy` (`lambda b: (setattr(b.metadata.retention_policy, "is_locked", True), b.metadata.retention_policy.effective_time.FromDatetime(datetime.datetime.now()))`). Preserve each handler's existing preconditions by passing them to `do_update_bucket`; drop the now-redundant direct `db.get_bucket` call.

- [ ] **Step 5: Route the gRPC handlers through `do_update_bucket` (write out the `_apply` closure)**

`UpdateBucket` (grpc_server.py:404-479): keep the immutable-field validation (:405-424) and the mask/label pre-computation (:426-451) OUTSIDE the mutator — they run before the lock and can error early. Move the mutation body (:457-478) verbatim into an `_apply(bucket)` closure, **binding `now` inside `_apply`** so the update timestamp is taken at mutation time under the lock:

```python
        # mask, updated_labels, removed_labels, request already computed above.
        def _apply(bucket):
            now = datetime.datetime.now(datetime.timezone.utc)
            mask.MergeMessage(request.bucket, bucket.metadata, replace_message_field=True,
                              replace_repeated_field=True)
            # ... acl / default_object_acl replacement, label add/remove,
            # soft_delete_policy validation -- the existing :457-478 body,
            # moved verbatim, using `now` and the closed-over precomputed values ...
            bucket.metadata.update_time.FromDatetime(now)

        bucket = self.db.do_update_bucket(
            request.bucket.name,
            update_fn=_apply,
            context=context,
            preconditions=testbench.common.make_grpc_bucket_preconditions(request),
        )
        return bucket.metadata
```

`LockBucketRetentionPolicy` (:353-381): move the `bucket.metadata.retention_policy.*` writes (:377-380) into `update_fn`, pass the custom `precondition` (:363-372) as `preconditions=[precondition]`; return `bucket.metadata`. `SetIamPolicy` (:389-392): `update_fn=lambda b: b.set_iam_policy(request, context)`, return `bucket.iam_policy`.

- [ ] **Step 6: Run the suite and both harness legs**

```bash
PYTHONPATH=. python -m pytest tests/test_store.py tests/test_grpc_server.py -q
PYTHONPATH=. python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
PYTHONPATH=. python -m tests.conformance.harness --store memory   # EMPTY diff; digest = Task-2 value
```
Expected: suite green; memory harness empty diff. Routing is behavior-preserving on `NullStore` (only no-op `bucket_updated`/`validate_bucket_name`/`object_purged` were added).

- [ ] **Step 7: Mutation-check the seam AND response preservation**

The conformance traces exercise only `bucket_patch` and gRPC `UpdateBucket`; the bucket PUT, four bucket-ACL, four defaultObjectAcl, `setIamPolicy`, and `lockRetentionPolicy` reroutes are **not** in the traces, so the **existing endpoint suite (`tests/test_testbench_bucket.py`, `tests/test_acl.py`, etc., which assert response bodies) is the response-preservation gate** for them. State this explicitly in the commit body. Mutation-checks:
- Un-route `bucket_patch` (call `bucket.patch` directly). Re-run `tests/test_store.py -k BucketUpdatedSeam` + a new `RecordingStore`-backed REST PATCH test. Expected: FAIL. Revert.
- Break the `default_object_acl_insert` holder cell (return `None` instead of `holder["acl"]`). Re-run the existing DOACL-insert endpoint test. Expected: that test FAILS on the RESPONSE body — proving response preservation is gated, not merely notification firing. Revert.
- Delete the `validate_bucket_name` call in `insert_bucket`. Expected: `test_insert_bucket_validates_name_before_commit` FAILS. Revert.

- [ ] **Step 8: Format and commit**

```bash
isort --quiet testbench/store.py testbench/database.py testbench/rest_server.py testbench/grpc_server.py tests/test_store.py && black --quiet testbench/store.py testbench/database.py testbench/rest_server.py testbench/grpc_server.py tests/test_store.py
git add testbench/store.py testbench/database.py testbench/rest_server.py testbench/grpc_server.py tests/test_store.py
git commit -m "refactor(store): route bucket mutations through Database.do_update_bucket + bucket_updated (zero-diff)"
```

**Safety gate:** memory harness EMPTY diff (digest = Task-2 value); full suite green; seam mutation-checked including one response-body preservation check on a rerouted ACL handler.

---

### Task 4: `testbench/pathing.py` — pure name→path policy

**Files:**
- Create: `testbench/pathing.py`
- Test: `tests/test_pathing.py`

**Interfaces:**
- Produces: `validate_bucket_name(name) -> None` (raises `ValueError` on reject; `context`-aware abort is the Store boundary's job, not this pure module's); `escape(object_name) -> str`; `unescape(escaped) -> str`; `classify(object_name) -> ("natural", relpath) | ("overflow", sha256hex)`; `is_contained(candidate_abspath, root_abspath) -> bool` (pure `os.path` logic, no realpath/no I/O — the I/O backstop is Task 5).

- [ ] **Step 1: Write failing tests (property + adversarial, every overflow clause + every bucket clause exercised)**

```python
# tests/test_pathing.py
import hashlib
import unittest

from hypothesis import given, strategies as st

from testbench import pathing

_LEGAL_NAME = st.text(
    alphabet=st.characters(min_codepoint=1, max_codepoint=0x10FFFF),
    min_size=1, max_size=64,
).filter(lambda s: "\x00" not in s)


class TestEscapeRoundTrip(unittest.TestCase):
    @given(_LEGAL_NAME)
    def test_unescape_inverts_escape(self, name):
        kind, target = pathing.classify(name)
        if kind == "natural":
            self.assertEqual(name, pathing.unescape(pathing.escape(name)))

    def test_overflow_cases_route_to_sha256(self):
        # One case per _needs_overflow clause: trailing slash, reserved prefix,
        # reserved suffix, NUL, leading slash, '.'/'..' segment, NAME_MAX oversize.
        for name in ("folder/", ".gcs/x", "audio/clip.wav.gcsmeta",
                     "a\x00b", "/etc/passwd", "a/../b", "a/./b",
                     ".", "..", "a" * 300):
            kind, target = pathing.classify(name)
            self.assertEqual("overflow", kind, name)
            self.assertEqual(hashlib.sha256(name.encode()).hexdigest(), target)

    def test_ordinary_names_stay_natural_and_pristine(self):
        for name in ("audio/clip.wav", "05-dir/nested.txt", "01-simple.txt"):
            kind, target = pathing.classify(name)
            self.assertEqual("natural", kind)
            self.assertEqual(name, pathing.unescape(target))


class TestValidateBucketName(unittest.TestCase):
    def test_rejects_traversal_and_illegal(self):
        for bad in ("../../etc/passwd", ".", "..", "a/b", "/etc", "a\x00b",
                    "foo/../bar", "a" * 64, ".lead", "trail.", "Upper", "goog-x"):
            with self.assertRaises(ValueError, msg=bad):
                pathing.validate_bucket_name(bad)

    def test_accepts_legal(self):
        for ok in ("my-bucket", "a1x", "abc.def.ghi", "test-uuid-123", "a_b1"):
            pathing.validate_bucket_name(ok)   # must not raise


class TestContainment(unittest.TestCase):
    def test_rejects_escape(self):
        self.assertFalse(pathing.is_contained("/data/../etc/passwd", "/data/b"))
        self.assertTrue(pathing.is_contained("/data/b/audio/clip.wav", "/data/b"))
```

- [ ] **Step 2: Run to see it fail**

Run: `PYTHONPATH=. python -m pytest tests/test_pathing.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'testbench.pathing'`.

- [ ] **Step 3: Implement `testbench/pathing.py`**

```python
"""Pure name -> on-disk-name policy for the file backend. No filesystem I/O.

The file backend's bucket-name check is the ONLY check (spec Security rule 4);
gcs/bucket.py's validator has a live bypass and is deliberately left unchanged.
Object names route unrepresentable/hostile cases to a SHA-256 overflow name
that contains no caller bytes, so path traversal is sidestepped by
construction (spec Security rule 2)."""

import hashlib
import os
import re

_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]{1,61}[a-z0-9]$")
NAME_MAX = 255
RESERVED_PREFIX = ".gcs/"
RESERVED_SUFFIX = ".gcsmeta"


def validate_bucket_name(name):
    if "\x00" in name or name.startswith("/") or name in (".", ".."):
        raise ValueError("illegal bucket name %r" % name)
    for part in name.split("/"):
        if part in (".", ".."):
            raise ValueError("traversal segment in bucket name %r" % name)
    if "/" in name:
        raise ValueError("slash in bucket name %r" % name)
    if not (3 <= len(name) <= 63) and "." not in name:
        raise ValueError("bucket name length %r" % name)
    if _BUCKET_RE.match(name) is None:
        raise ValueError("bucket name char-class %r" % name)
    if name.startswith("goog") or re.search("g[0o][0o]g[1l][e3]", name):
        raise ValueError("reserved goog bucket name %r" % name)


def _needs_overflow(object_name):
    if object_name.endswith("/"):
        return True
    if object_name.startswith(RESERVED_PREFIX) or object_name.endswith(RESERVED_SUFFIX):
        return True
    if "\x00" in object_name or object_name.startswith("/"):
        return True
    for seg in object_name.split("/"):
        if seg in (".", "..") or len(seg.encode("utf-8")) > NAME_MAX:
            return True
    return False


def escape(object_name):
    # Minimal, reversible: percent-encode only "%" (the escape char) so a
    # natural name survives round-trip; ordinary names appear pristine.
    return object_name.replace("%", "%25")


def unescape(escaped):
    return escaped.replace("%25", "%")


def classify(object_name):
    if _needs_overflow(object_name):
        return "overflow", hashlib.sha256(object_name.encode("utf-8")).hexdigest()
    return "natural", escape(object_name)


def is_contained(candidate_abspath, root_abspath):
    root = os.path.normpath(root_abspath)
    cand = os.path.normpath(candidate_abspath)
    return cand == root or cand.startswith(root + os.sep)
```

- [ ] **Step 4: Run to green**

Run: `PYTHONPATH=. python -m pytest tests/test_pathing.py -q`
Expected: PASS (hypothesis explores the round-trip).

- [ ] **Step 5: Mutation-check EVERY guard clause (one reintroduction per clause, each reverted)**

`_needs_overflow` — delete each clause in turn and confirm the matching input in `test_overflow_cases_route_to_sha256` FAILS:
- trailing-slash clause → `"folder/"` fails
- reserved-prefix clause → `".gcs/x"` fails
- reserved-suffix clause → `"audio/clip.wav.gcsmeta"` fails
- NUL clause → `"a\x00b"` fails
- leading-slash clause → `"/etc/passwd"` fails
- `'.'/'..'` segment clause → `"a/../b"`, `"."`, `".."` fail
- `NAME_MAX` clause → `"a"*300` fails

`validate_bucket_name` — delete each clause in turn and confirm `test_rejects_traversal_and_illegal` FAILS on the matching input:
- traversal loop → `"../../etc/passwd"`, `"foo/../bar"`
- slash clause → `"a/b"`
- length clause → `"a"*64`
- char-class regex → `".lead"`, `"trail."`, `"Upper"`
- goog-reserved clause → `"goog-x"`

- [ ] **Step 6: 3.8 parse, format, commit**

```bash
PYTHONPATH=. python -c "import ast; ast.parse(open('testbench/pathing.py').read(), feature_version=(3,8))"
isort --quiet testbench/pathing.py tests/test_pathing.py && black --quiet testbench/pathing.py tests/test_pathing.py
git add testbench/pathing.py tests/test_pathing.py
git commit -m "feat(filestore): pure name->path policy (bucket validation, escape, overflow, containment)"
```

**Safety gate:** memory harness untouched (no wiring). New property/adversarial suite green, every guard clause mutation-checked, NUL/leading-slash/'.'/'..' object names proven to route to overflow.

---

### Task 5: `testbench/containment.py` — fd-based containment backstop

**Files:**
- Create: `testbench/containment.py`
- Test: `tests/test_containment.py`

**Interfaces:**
- Produces: `assert_posix_support() -> None` (raises `RuntimeError` if `O_NOFOLLOW`/`dir_fd` unsupported — no silent degradation); `assert_within(path, root) -> None` (raises `PermissionError` if `os.path.realpath(path)` escapes `root`); `open_dir_nofollow(dir_fd, name) -> int` (`openat` a single dir component with `O_RDONLY|O_DIRECTORY|O_NOFOLLOW`); `open_bucket_root_fd(root_fd, short) -> int` (single component; bucket names carry no slash); `walk_dirs(dir_fd, parts, create) -> int` (walk/optionally `mkdirat`+open each component with `O_NOFOLLOW`, returning a NEW dirfd for the leaf — caller closes it if it differs from `dir_fd`); `safe_open(dir_fd, name, flags, mode=0o644) -> int` (`openat` a single FILE component with `O_NOFOLLOW`); `write_bytes_atomic(dir_fd, name, data) -> None` (temp via `safe_open(O_WRONLY|O_CREAT|O_EXCL)` + `os.replace(..., src_dir_fd=dir_fd, dst_dir_fd=dir_fd)`); `constrained_rmtree(path, root, index_names) -> None` (removes only a real, non-symlink direct child of `root` — verified by **realpath**, not string normpath — whose basename is in `index_names`; no-op if already gone).

- [ ] **Step 1: Write failing tests (tmpdir + symlink swap at intermediate component)**

```python
# tests/test_containment.py
import os
import tempfile
import unittest

from testbench import containment


class TestContainment(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_posix_support_asserted(self):
        containment.assert_posix_support()  # must not raise on Linux/macOS

    def test_assert_within_rejects_escape(self):
        outside = os.path.join(self.root, "..", "escapee")
        with self.assertRaises(PermissionError):
            containment.assert_within(outside, self.root)
        containment.assert_within(os.path.join(self.root, "a", "b"), self.root)

    def test_safe_open_refuses_symlink_final_component(self):
        target = os.path.join(self.root, "real")
        open(target, "wb").close()
        os.symlink(target, os.path.join(self.root, "link"))
        rfd = os.open(self.root, os.O_RDONLY)
        try:
            with self.assertRaises(OSError):
                containment.safe_open(rfd, "link", os.O_RDONLY)
        finally:
            os.close(rfd)

    def test_walk_dirs_refuses_symlinked_intermediate_component(self):
        # Plant <root>/audio -> /tmp (an intermediate dir component); walking
        # into it with create=False must be refused by O_NOFOLLOW.
        outside = tempfile.mkdtemp()
        os.symlink(outside, os.path.join(self.root, "audio"))
        rfd = os.open(self.root, os.O_RDONLY)
        try:
            with self.assertRaises(OSError):
                containment.walk_dirs(rfd, ["audio"], create=False)
        finally:
            os.close(rfd)

    def test_write_bytes_atomic_lands_in_dir(self):
        rfd = os.open(self.root, os.O_RDONLY)
        try:
            containment.write_bytes_atomic(rfd, "m", b"hello")
        finally:
            os.close(rfd)
        self.assertEqual(b"hello", open(os.path.join(self.root, "m"), "rb").read())

    def test_constrained_rmtree(self):
        b = os.path.join(self.root, "bucket-x")
        os.makedirs(os.path.join(b, "audio"))
        containment.constrained_rmtree(b, self.root, {"bucket-x"})
        self.assertFalse(os.path.exists(b))
        containment.constrained_rmtree(b, self.root, {"bucket-x"})  # already gone

    def test_constrained_rmtree_refuses_symlink_non_child_unindexed(self):
        outside = tempfile.mkdtemp()
        os.symlink(outside, os.path.join(self.root, "evil"))
        with self.assertRaises(PermissionError):
            containment.constrained_rmtree(os.path.join(self.root, "evil"),
                                           self.root, {"evil"})
        with self.assertRaises(PermissionError):
            containment.constrained_rmtree(outside, self.root,
                                           {os.path.basename(outside)})
        real = os.path.join(self.root, "bucket-y")
        os.makedirs(real)
        with self.assertRaises(PermissionError):
            containment.constrained_rmtree(real, self.root, set())
```

- [ ] **Step 2: Run to see it fail** — `ModuleNotFoundError: No module named 'testbench.containment'`.

- [ ] **Step 3: Implement `testbench/containment.py`**

```python
"""Fd-based filesystem containment: the backstop that holds even if pathing.py
has a bug or a symlink is swapped in between a check and an open (spec Security
rules 1, 3, 5). Every path component is opened via openat with O_NOFOLLOW, so
no symlink at any component can redirect a write outside the bucket root."""

import os
import shutil

from testbench import pathing

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def assert_posix_support():
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError(
            "file backend requires POSIX openat/O_NOFOLLOW (dir_fd) support")


def assert_within(path, root):
    if not pathing.is_contained(os.path.realpath(path), os.path.realpath(root)):
        raise PermissionError("path %r escapes root %r" % (path, root))


def open_dir_nofollow(dir_fd, name):
    return os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=dir_fd)


def open_bucket_root_fd(root_fd, short):
    # `short` is a validated bucket name: no slash, no '.'/'..' -> one component.
    return open_dir_nofollow(root_fd, short)


def walk_dirs(dir_fd, parts, create):
    """Return a dirfd for the leaf of `parts` relative to `dir_fd`, opening each
    component with O_NOFOLLOW (optionally mkdirat-ing it first). Returns
    `dir_fd` itself when `parts` is empty; otherwise a NEW fd the caller closes."""
    cur = dir_fd
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=cur)
                except FileExistsError:
                    pass
            nxt = open_dir_nofollow(cur, part)
            if cur != dir_fd:
                os.close(cur)
            cur = nxt
        return cur
    except BaseException:
        if cur != dir_fd:
            os.close(cur)
        raise


def safe_open(dir_fd, name, flags, mode=0o644):
    return os.open(name, flags | _O_NOFOLLOW, mode, dir_fd=dir_fd)


def write_bytes_atomic(dir_fd, name, data):
    tmp = ".tmp-%d-%s" % (os.getpid(), name)
    fd = safe_open(dir_fd, tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise


def constrained_rmtree(path, root, index_names):
    if not os.path.lexists(path):
        return  # defensive teardown: already gone (spec Test-isolation)
    # realpath (not string normpath) so a symlinked component cannot make an
    # out-of-root target look like a direct child of root.
    real = os.path.realpath(path)
    if os.path.dirname(real) != os.path.realpath(root):
        raise PermissionError("rmtree target %r is not a direct child of %r" % (path, root))
    if os.path.islink(path) or not os.path.isdir(path):
        raise PermissionError("rmtree target %r is not a real directory" % path)
    if os.path.basename(real) not in index_names:
        raise PermissionError("rmtree target %r not present in index" % path)
    shutil.rmtree(real)
```

- [ ] **Step 4: Run to green** — `PYTHONPATH=. python -m pytest tests/test_containment.py -q` → PASS. (POSIX-gated: skip the symlink/openat tests under `os.name == "nt"`; the file backend is POSIX-only per Global Constraints.)

- [ ] **Step 5: Mutation-check every containment guard (each reverted)**
- `assert_within` → make it `pass`; `test_assert_within_rejects_escape` FAILS.
- `safe_open` → drop `| _O_NOFOLLOW`; `test_safe_open_refuses_symlink_final_component` FAILS.
- `walk_dirs` → open with plain `os.O_RDONLY` (no `O_NOFOLLOW`); `test_walk_dirs_refuses_symlinked_intermediate_component` FAILS.
- `constrained_rmtree` → replace realpath parent check with string `os.path.normpath`; `test_constrained_rmtree_refuses_symlink_non_child_unindexed` (non-child-via-symlink) FAILS. Remove the `index_names` clause; the unindexed sub-case FAILS. Remove `assert_posix_support` from the module and confirm `test_posix_support_asserted` FAILS on import/attribute.

- [ ] **Step 6: 3.8 parse, format, commit**

```bash
PYTHONPATH=. python -c "import ast; ast.parse(open('testbench/containment.py').read(), feature_version=(3,8))"
isort --quiet testbench/containment.py tests/test_containment.py && black --quiet testbench/containment.py tests/test_containment.py
git add testbench/containment.py tests/test_containment.py
git commit -m "feat(filestore): fd-based openat/O_NOFOLLOW + realpath constrained-rmtree containment backstop"
```

**Safety gate:** memory harness untouched. Containment suite green; O_NOFOLLOW enforced on every component; realpath used for the rmtree child check; every guard mutation-checked.

---

### Task 6: `testbench/sidecar.py` — versioned metadata envelope (fd-based atomic write)

**Files:**
- Create: `testbench/sidecar.py`
- Test: `tests/test_sidecar.py`

**Interfaces:**
- Consumes: `google.storage.v2.storage_pb2` (`Object`, `Bucket`), `google.protobuf.json_format`, `testbench.containment`.
- Produces: `dump(proto, true_name) -> str` (JSON envelope `{"schema_version": 1, "kind", "name": true_name, "proto": <MessageToJson dict>}`); `load(text) -> (kind, true_name, proto)`; `write_atomic(dir_fd, filename, text) -> None` (fd-based temp + `os.replace(..., src_dir_fd, dst_dir_fd)` via `containment.write_bytes_atomic`, so sidecar writes go through the same O_NOFOLLOW backstop as media); `read(path) -> (kind, true_name, proto)` raising `ValueError` on corrupt/truncated JSON. **Note the fd-based signature** — every `FileStore` caller in Task 7 has a bucket/leaf dirfd, never a bare directory path.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_sidecar.py
import os
import tempfile
import unittest

from google.storage.v2 import storage_pb2

from testbench import sidecar


class TestSidecar(unittest.TestCase):
    def test_object_round_trip_preserves_true_name(self):
        obj = storage_pb2.Object(name="audio/clip.wav.gcsmeta", size=42, generation=17)
        text = sidecar.dump(obj, true_name="audio/clip.wav.gcsmeta")
        kind, name, restored = sidecar.load(text)
        self.assertEqual("Object", kind)
        self.assertEqual("audio/clip.wav.gcsmeta", name)
        self.assertEqual(42, restored.size)
        self.assertEqual(17, restored.generation)

    def test_bucket_round_trip(self):
        b = storage_pb2.Bucket(name="projects/_/buckets/my-bucket", metageneration=4)
        _, name, restored = sidecar.load(sidecar.dump(b, true_name="my-bucket"))
        self.assertEqual("my-bucket", name)
        self.assertEqual(4, restored.metageneration)

    def test_write_atomic_is_fd_based(self):
        d = tempfile.mkdtemp()
        fd = os.open(d, os.O_RDONLY)
        try:
            sidecar.write_atomic(fd, "bucket.json",
                                 sidecar.dump(storage_pb2.Bucket(name="x"), "x"))
        finally:
            os.close(fd)
        _, name, _ = sidecar.read(os.path.join(d, "bucket.json"))
        self.assertEqual("x", name)

    def test_corrupt_sidecar_raises_loudly(self):
        with self.assertRaises(ValueError):
            sidecar.load('{"schema_version": 1, "name": "x", "proto"')  # truncated
```

- [ ] **Step 2: Run to see it fail** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `testbench/sidecar.py`**

```python
"""On-disk sidecar format: the proto as JSON in a small versioned envelope
carrying the true (unescaped) name. Round-trips exactly; no second data model
(spec On-disk-layout rule 3). Writes go through containment so they inherit the
O_NOFOLLOW backstop."""

import json

from google.protobuf import json_format
from google.storage.v2 import storage_pb2

from testbench import containment

SCHEMA_VERSION = 1


def dump(proto, true_name):
    kind = type(proto).__name__  # "Object" or "Bucket"
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": kind,
            "name": true_name,
            "proto": json.loads(json_format.MessageToJson(proto)),
        },
        sort_keys=True,
    )


def load(text):
    try:
        env = json.loads(text)
        kind = env["kind"]
        proto = {"Object": storage_pb2.Object, "Bucket": storage_pb2.Bucket}[kind]()
        json_format.ParseDict(env["proto"], proto)
        return kind, env["name"], proto
    except (ValueError, KeyError, json_format.ParseError) as exc:
        raise ValueError("corrupt sidecar: %s" % exc)


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return load(handle.read())


def write_atomic(dir_fd, filename, text):
    containment.write_bytes_atomic(dir_fd, filename, text.encode("utf-8"))
```

- [ ] **Step 4: Run to green** — `PYTHONPATH=. python -m pytest tests/test_sidecar.py -q` → PASS.

- [ ] **Step 5: Mutation-check** — in `dump`, drop the `"name"` key; expected `test_object_round_trip_preserves_true_name` FAILS (KeyError in `load`). Revert. In `load`, swallow the exception (`return None, None, None`); expected `test_corrupt_sidecar_raises_loudly` FAILS. Revert.

- [ ] **Step 6: 3.8 parse, format, commit**

```bash
PYTHONPATH=. python -c "import ast; ast.parse(open('testbench/sidecar.py').read(), feature_version=(3,8))"
isort --quiet testbench/sidecar.py tests/test_sidecar.py && black --quiet testbench/sidecar.py tests/test_sidecar.py
git add testbench/sidecar.py tests/test_sidecar.py
git commit -m "feat(filestore): versioned sidecar envelope over MessageToJson (fd-based atomic os.replace)"
```

**Safety gate:** memory harness untouched. Sidecar suite green, mutation-checked. Persistence core proven in isolation and routed through the containment backstop before any disk-writing feature composes it.

---

### Task 7: `testbench/filestore.py` — the notification handlers (contained writes, write-time collision guard)

**Files:**
- Create: `testbench/filestore.py`
- Test: `tests/test_filestore.py`
- Consumes the notification surface in `testbench/database.py`: `insert_bucket` :120-128, `delete_bucket` :205-221, `insert_object` :493-516, `delete_object`/`__soft_delete_object` :303-316/:518-555, `__remove_expired_objects_from_soft_delete` :318-335, `do_update_object` :557-598, `restore_object` :600-643, folder ops :866-920, `clear` :77-101.

**Interfaces:**
- Produces: `class FileStore(Store)` constructed as `FileStore(root)`; asserts POSIX support in `__init__`. Implements `validate_bucket_name(name, context)`, `bucket_inserted`, `bucket_deleted`, `bucket_updated`, `object_inserted`, `object_deleted`, `object_soft_deleted`, `object_purged`, `object_updated`, `folder_inserted`, `folder_deleted`, `folder_renamed`, `cleared`. Every write walks a per-bucket root dirfd via `containment` (O_NOFOLLOW on each component). Layout: bucket root `<root>/<name>/`, reserved subtree `<root>/<name>/.gcs/{generations,soft_deleted,uploads,folders,overflow}/`, `.gcs/bucket.json`, live media at natural path + `.gcsmeta` sidecar, older gens under `.gcs/generations/`. **Restore reconciliation is Task 3's `object_purged` alone** — `object_inserted` performs NO soft-deleted cleanup (no `generation - 1` magic).

- [ ] **Step 1: Write failing direct-drive tests (temp root, incl. planted-symlink)**

```python
# tests/test_filestore.py -- representative slice; one test per handler.
import os
import tempfile
import unittest

from testbench.filestore import FileStore
from testbench import sidecar
# reuse tests/test_store.py's _make_bucket/_make_object helpers via import


class TestFileStore(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.fs = FileStore(self.root)

    def _bucket_dir(self, name="bucket-name"):
        return os.path.join(self.root, name)

    def test_bucket_inserted_builds_tree_and_bucket_json(self):
        self.fs.bucket_inserted(_make_bucket("bucket-name"))
        d = self._bucket_dir()
        for sub in ("generations", "soft_deleted", "uploads", "folders", "overflow"):
            self.assertTrue(os.path.isdir(os.path.join(d, ".gcs", sub)))
        kind, name, _ = sidecar.read(os.path.join(d, ".gcs", "bucket.json"))
        self.assertEqual(("Bucket", "bucket-name"), (kind, name))

    def test_validate_bucket_name_rejects_traversal(self):
        with self.assertRaises(Exception):
            self.fs.validate_bucket_name(
                "projects/_/buckets/../../etc/passwd", None)

    def test_object_inserted_writes_media_and_sidecar_at_natural_path(self):
        self.fs.bucket_inserted(_make_bucket("bucket-name"))
        blob = _make_object(None, "audio/clip.wav", media=b"hello")
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        media = os.path.join(self._bucket_dir(), "audio", "clip.wav")
        self.assertEqual(b"hello", open(media, "rb").read())
        self.assertTrue(os.path.exists(media + ".gcsmeta"))

    def test_object_inserted_refuses_symlinked_intermediate_component(self):
        # Plant <bucket>/audio -> /tmp/outside BEFORE the write; the O_NOFOLLOW
        # walk must refuse to follow it, and nothing may be written outside root.
        self.fs.bucket_inserted(_make_bucket("bucket-name"))
        outside = tempfile.mkdtemp()
        os.symlink(outside, os.path.join(self._bucket_dir(), "audio"))
        blob = _make_object(None, "audio/clip.wav", media=b"pwn")
        with self.assertRaises(OSError):
            self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        self.assertEqual([], os.listdir(outside))  # nothing escaped

    def test_soft_delete_moves_to_gcs_soft_deleted_not_removed(self):
        self.fs.bucket_inserted(_make_bucket("bucket-name"))
        blob = _make_object(None, "clip.wav", media=b"x")
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        self.fs.object_soft_deleted("projects/_/buckets/bucket-name", blob,
                                    blob.metadata.hard_delete_time)
        self.assertFalse(os.path.exists(os.path.join(self._bucket_dir(), "clip.wav")))
        self.assertTrue(os.listdir(os.path.join(self._bucket_dir(), ".gcs", "soft_deleted")))

    def test_object_updated_is_idempotent(self):
        self.fs.bucket_inserted(_make_bucket("bucket-name"))
        blob = _make_object(None, "clip.wav", media=b"x")
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        p = os.path.join(self._bucket_dir(), "clip.wav.gcsmeta")
        before = open(p).read()
        self.fs.object_updated("projects/_/buckets/bucket-name", blob)
        self.fs.object_updated("projects/_/buckets/bucket-name", blob)
        self.assertEqual(before, open(p).read())

    def test_overflow_name_lands_under_overflow_with_true_name(self):
        self.fs.bucket_inserted(_make_bucket("bucket-name"))
        blob = _make_object(None, "clip.wav.gcsmeta", media=b"x")
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        overflow = os.path.join(self._bucket_dir(), ".gcs", "overflow")
        entries = [f for f in os.listdir(overflow) if f.endswith(".gcsmeta")]
        _, true_name, _ = sidecar.read(os.path.join(overflow, entries[0]))
        self.assertEqual("clip.wav.gcsmeta", true_name)

    def test_cleared_and_bucket_deleted_tolerate_missing_dir(self):
        self.fs.bucket_inserted(_make_bucket("bucket-name"))
        self.fs.bucket_deleted("projects/_/buckets/bucket-name")
        self.fs.bucket_deleted("projects/_/buckets/bucket-name")  # already gone
        self.fs.cleared()

    def test_folder_handlers_do_not_touch_resource_state(self):
        self.fs.bucket_inserted(_make_bucket("bucket-name"))
        self.fs.folder_inserted("bucket-name/logs/", object())
        self.assertTrue(os.listdir(os.path.join(self._bucket_dir(), ".gcs", "folders")))
```

- [ ] **Step 2: Run to see it fail** — `ModuleNotFoundError: No module named 'testbench.filestore'`.

- [ ] **Step 3: Implement `testbench/filestore.py`**

Compose `pathing`+`containment`+`sidecar`. Every write acquires a per-bucket root dirfd and walks components with `O_NOFOLLOW`; media goes through `containment.write_bytes_atomic`, sidecars through `sidecar.write_atomic(dir_fd, …)`. Load-bearing shapes:

```python
"""FileStore: mirror the in-memory index to a GCS-shaped tree. Thin translator
from Store notifications to CONTAINED filesystem ops -- every path component is
opened with O_NOFOLLOW so a planted/swapped symlink cannot escape the bucket
root. All name/path/security logic lives in pathing/containment/sidecar."""

import contextlib
import os

import testbench.common
import testbench.error
from testbench import containment, pathing, sidecar
from testbench.store import Store

_SUBDIRS = ("generations", "soft_deleted", "uploads", "folders", "overflow")


class FileStore(Store):
    def __init__(self, root):
        containment.assert_posix_support()
        self._root = os.path.realpath(root)
        os.makedirs(self._root, exist_ok=True)

    def _bucket_name(self, proto_name):
        return testbench.common.bucket_name_from_proto(proto_name)

    def _index_names(self):
        return set(os.listdir(self._root))

    @contextlib.contextmanager
    def _bucket_dirfd(self, short, create=False):
        rfd = os.open(self._root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            if create:
                try:
                    os.mkdir(short, 0o755, dir_fd=rfd)
                except FileExistsError:
                    pass
            bfd = containment.open_bucket_root_fd(rfd, short)
        finally:
            os.close(rfd)
        try:
            yield bfd
        finally:
            os.close(bfd)

    @contextlib.contextmanager
    def _leaf_dirfd(self, bfd, parts, create):
        dfd = containment.walk_dirs(bfd, parts, create=create)
        try:
            yield dfd
        finally:
            if dfd != bfd:
                os.close(dfd)

    # --- pre-commit validation (spec rules 2/4; only check for file backend) -
    def validate_bucket_name(self, name, context=None):
        try:
            pathing.validate_bucket_name(self._bucket_name(name))
        except ValueError as exc:
            # Clean 4xx (REST) / INVALID_ARGUMENT (gRPC) BEFORE commit -- context
            # is threaded from Database.insert_bucket so gRPC aborts correctly.
            testbench.error.invalid("Bucket name %s" % exc, context)

    # --- buckets -----------------------------------------------------------
    def bucket_inserted(self, bucket):
        short = self._bucket_name(bucket.metadata.name)
        with self._bucket_dirfd(short, create=True) as bfd:
            gcs_fd = containment.walk_dirs(bfd, [".gcs"], create=True)
            try:
                for sub in _SUBDIRS:
                    try:
                        os.mkdir(sub, 0o755, dir_fd=gcs_fd)
                    except FileExistsError:
                        pass
                sidecar.write_atomic(gcs_fd, "bucket.json",
                                     sidecar.dump(bucket.metadata, short))
            finally:
                os.close(gcs_fd)

    def bucket_updated(self, bucket):
        short = self._bucket_name(bucket.metadata.name)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, [".gcs"], create=True) as gcs_fd:
                sidecar.write_atomic(gcs_fd, "bucket.json",
                                     sidecar.dump(bucket.metadata, short))

    def bucket_deleted(self, bucket_name):
        short = self._bucket_name(bucket_name)
        path = os.path.join(self._root, short)
        containment.constrained_rmtree(path, self._root, self._index_names())

    # --- objects -----------------------------------------------------------
    def _dest_parts(self, object_name):
        """(reldir_parts, base) for the LIVE object; overflow -> .gcs/overflow."""
        kind, target = pathing.classify(object_name)
        if kind == "overflow":
            return [".gcs", "overflow"], target
        parts = target.split("/")
        return parts[:-1], parts[-1]

    def object_inserted(self, bucket_name, blob):
        object_name = blob.metadata.name
        short = self._bucket_name(bucket_name)
        parts, base = self._dest_parts(object_name)
        data = blob.media.to_bytes()  # MEDIA CALL SITE (tests/media_call_sites.txt)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, parts, create=True) as dfd:
                self._guard_collision(dfd, base, object_name)  # write-time (see below)
                containment.write_bytes_atomic(dfd, base, data)
                sidecar.write_atomic(dfd, base + ".gcsmeta",
                                     sidecar.dump(blob.metadata, object_name))
        # NOTE: no soft-deleted cleanup here. Restore reconciliation is the
        # object_purged signal fired by Database.restore_object (Task 3) --
        # exactly one mechanism, no `generation - 1` magic.

    def _guard_collision(self, dfd, base, object_name):
        """Refuse to clobber a live media whose sidecar carries a DIFFERENT true
        name -- the case-insensitive-FS collapse (Clip.wav vs clip.wav) surfaces
        here as an existing .gcsmeta with a mismatched true_name."""
        try:
            fd = containment.safe_open(dfd, base + ".gcsmeta", os.O_RDONLY)
        except FileNotFoundError:
            return
        try:
            _, existing_true, _ = sidecar.load(os.fdopen(fd).read())
        finally:
            pass
        if existing_true != object_name:
            raise RuntimeError(
                "collision: %r and %r map to one on-disk target" %
                (existing_true, object_name))

    def object_updated(self, bucket_name, blob):
        short = self._bucket_name(bucket_name)
        parts, base = self._dest_parts(blob.metadata.name)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, parts, create=False) as dfd:
                sidecar.write_atomic(dfd, base + ".gcsmeta",
                                     sidecar.dump(blob.metadata, blob.metadata.name))

    def object_deleted(self, bucket_name, object_name, generation):
        short = self._bucket_name(bucket_name)
        parts, base = self._dest_parts(object_name)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, parts, create=False) as dfd:
                for n in (base, base + ".gcsmeta"):
                    _unlink_quiet(dfd, n)

    def object_soft_deleted(self, bucket_name, blob, hard_delete_time):
        short = self._bucket_name(bucket_name)
        parts, base = self._dest_parts(blob.metadata.name)
        gen = str(blob.metadata.generation)
        with self._bucket_dirfd(short) as bfd:
            with self._leaf_dirfd(bfd, [".gcs", "soft_deleted"], create=True) as sfd:
                try:
                    os.mkdir(gen, 0o755, dir_fd=sfd)
                except FileExistsError:
                    pass
                dstfd = containment.open_dir_nofollow(sfd, gen)
                try:
                    with self._leaf_dirfd(bfd, parts, create=False) as dfd:
                        _move_quiet(dfd, base, dstfd, "media")
                        sidecar.write_atomic(dstfd, "meta.gcsmeta",
                                             sidecar.dump(blob.metadata, blob.metadata.name))
                        _unlink_quiet(dfd, base + ".gcsmeta")
                finally:
                    os.close(dstfd)

    def object_purged(self, bucket_name, object_name, generation):
        short = self._bucket_name(bucket_name)
        dst = os.path.join(self._root, short, ".gcs", "soft_deleted", str(generation))
        parent = os.path.dirname(dst)
        if os.path.isdir(dst):
            containment.constrained_rmtree(dst, parent, {str(generation)})

    # --- folders (touch only .gcs/folders; never re-enter resource state) ---
    def folder_inserted(self, folder_name, folder):
        self._write_folder(folder_name)

    def folder_deleted(self, folder_name):
        self._remove_folder(folder_name)

    def folder_renamed(self, src, dst, folder):
        self._remove_folder(src)
        self._write_folder(dst)

    def cleared(self):
        for name in self._index_names():
            containment.constrained_rmtree(
                os.path.join(self._root, name), self._root, self._index_names())
```

Fill in the remaining helpers with their exact contracts:
- `_unlink_quiet(dir_fd, name)` / `_move_quiet(src_fd, src_name, dst_fd, dst_name)` — module-level; `os.unlink(name, dir_fd=…)` / `os.replace(src, dst, src_dir_fd=…, dst_dir_fd=…)` swallowing `FileNotFoundError`.
- `_write_folder(folder_name)` / `_remove_folder(folder_name)` — split `bucket_short/prefix…`, select the bucket root dirfd, and write/remove `.gcs/folders/<pathing.escape(prefix)>.json` via `sidecar`-shaped atomic write; folder handlers touch ONLY `.gcs/folders`.
- `object_soft_deleted`'s `hard_delete_time` is already inside `blob.metadata`; persisting `meta.gcsmeta` lets the startup scan re-derive the expiry schedule.
- Media-first ordering (spec On-disk-layout rule 1): media `write_bytes_atomic` lands before the `.gcsmeta` sidecar.

- [ ] **Step 4: Run to green** — `PYTHONPATH=. python -m pytest tests/test_filestore.py -q` → PASS.

- [ ] **Step 5: Mutation-check the security + distinctness guards (each reverted)**
- `validate_bucket_name` → no-op; `test_validate_bucket_name_rejects_traversal` FAILS.
- In `object_inserted`, replace the `_leaf_dirfd`/`walk_dirs` write with a plain `open(os.path.join(...))` path write. Expected: `test_object_inserted_refuses_symlinked_intermediate_component` FAILS (the write follows the symlink and escapes; `outside` becomes non-empty). Revert. This proves `safe_open`/`walk_dirs` are LIVE in the write path, not dead code.
- `object_soft_deleted` → make it call `object_deleted`; `test_soft_delete_moves_to_gcs_soft_deleted_not_removed` FAILS.
- `object_updated` → stamp a timestamp into the sidecar; `test_object_updated_is_idempotent` FAILS.
- `_guard_collision` → `return` immediately (paired with the Task-8 collision test still using this at write time).

- [ ] **Step 6: 3.8 parse, format, commit**

```bash
PYTHONPATH=. python -c "import ast; ast.parse(open('testbench/filestore.py').read(), feature_version=(3,8))"
isort --quiet testbench/filestore.py tests/test_filestore.py && black --quiet testbench/filestore.py tests/test_filestore.py
git add testbench/filestore.py tests/test_filestore.py
git commit -m "feat(filestore): contained notification handlers over the .gcs/ layout (bytes still BytesMedia)"
```

**Safety gate:** memory harness untouched (`FileStore` not yet default). All handler tests green; the planted-symlink write-path test proves `safe_open`/`walk_dirs` are wired (not dead code); security/distinctness/idempotency/lock-safety/single-reconciliation guards mutation-checked.

---

### Task 8: Startup tree-scan, inode-based collision detection, and index hydration

**Files:**
- Modify: `testbench/filestore.py` (add `rebuild_index(database)`)
- Modify: `testbench/database.py` (`init` :69-71 — accept and hydrate from a file store)
- Test: `tests/test_filestore_scan.py`

**Interfaces:**
- Produces: `FileStore.rebuild_index(database)` — walk each bucket root, read `.gcs/bucket.json` and every `.gcsmeta`/`generations`/`soft_deleted`/`overflow` sidecar, and seed `database._buckets/_objects/_live_generations/_soft_deleted_objects/_folders` **without re-notifying** (direct seeding, not via `insert_*`). Fails loudly on a **collision** (two distinct true-names resolving to the same on-disk inode — the filesystem-truthful identity rule, so it fires on a case-insensitive FS and correctly stays silent on a case-sensitive one) and on a corrupt sidecar; a media file with no sidecar is an invisible orphan.

- [ ] **Step 1: Write failing tests (FS-aware collision + restore reconciliation)**

```python
# tests/test_filestore_scan.py -- representative slice.
import os, tempfile, unittest
from testbench.filestore import FileStore
import testbench.database


def _case_insensitive(root):
    probe = os.path.join(root, "CaseProbe")
    open(probe, "w").close()
    try:
        return os.path.exists(os.path.join(root, "caseprobe"))
    finally:
        os.remove(probe)


class TestScan(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.fs = FileStore(self.root)

    def test_round_trip_rebuild_matches(self):
        self.fs.bucket_inserted(_make_bucket("bucket-name"))
        blob = _make_object(None, "audio/clip.wav", media=b"hi")
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        db = testbench.database.Database.init(store=self.fs)  # hydrates
        self.assertIn("projects/_/buckets/bucket-name", db._buckets)
        # object present in the rebuilt index
        # (assert via the public get_object path used elsewhere in the suite)

    def test_case_collision_is_loud_on_case_insensitive_fs(self):
        self.fs.bucket_inserted(_make_bucket("bucket-name"))
        b1 = _make_object(None, "Clip.wav", media=b"a")
        self.fs.object_inserted("projects/_/buckets/bucket-name", b1)
        b2 = _make_object(None, "clip.wav", media=b"b")
        if _case_insensitive(self.root):
            # write-time guard fires first (different true-name at same target)
            with self.assertRaises(RuntimeError):
                self.fs.object_inserted("projects/_/buckets/bucket-name", b2)
        else:
            # case-sensitive FS: both coexist, rebuild sees two distinct inodes
            self.fs.object_inserted("projects/_/buckets/bucket-name", b2)
            db = testbench.database.Database.init(store=self.fs)
            self.assertIn("projects/_/buckets/bucket-name", db._buckets)

    def test_corrupt_sidecar_raises_loudly(self):
        self.fs.bucket_inserted(_make_bucket("bucket-name"))
        blob = _make_object(None, "clip.wav", media=b"x")
        self.fs.object_inserted("projects/_/buckets/bucket-name", blob)
        with open(os.path.join(self.root, "bucket-name", "clip.wav.gcsmeta"), "w") as fh:
            fh.write('{"schema_version":1,"proto"')  # truncated
        with self.assertRaises(ValueError):
            testbench.database.Database.init(store=self.fs)

    def test_restore_removes_soft_deleted_on_disk(self):
        # soft-delete then restore -> the .gcs/soft_deleted/<orig_gen> dir is
        # gone (object_purged reconciliation), live media/sidecar correct.
        db = testbench.database.Database.init(store=self.fs)
        db.insert_bucket(_make_sd_bucket("sd-bucket"), None)  # soft_delete_policy set
        blob = _make_object(None, "clip.wav", media=b"x")
        db.insert_object("sd-bucket", blob, None)
        orig_gen = blob.metadata.generation
        db.delete_object("sd-bucket", "clip.wav")  # soft-deletes
        sd_dir = os.path.join(self.root, "sd-bucket", ".gcs", "soft_deleted", str(orig_gen))
        self.assertTrue(os.path.isdir(sd_dir))
        db.restore_object("sd-bucket", "clip.wav", orig_gen)
        self.assertFalse(os.path.exists(sd_dir))  # purge reconciled the disk
```

- [ ] **Step 2: Run to see it fail** — `AttributeError: 'FileStore' object has no attribute 'rebuild_index'`.

- [ ] **Step 3: Implement `rebuild_index` + `Database.init` hydration**

`rebuild_index(database)` walks `self._root`; for each bucket dir: parse `.gcs/bucket.json` into `database._buckets[proto_name]`, seed the per-bucket dicts, then walk live natural-path sidecars + `.gcs/generations` + `.gcs/soft_deleted` + `.gcs/overflow`, reconstructing `gcs.object.Object` from each sidecar's proto (`sidecar.read`), keying live generations. Maintain `seen = {}` mapping `(st_dev, st_ino)` of each live media file (obtained via `os.stat` of the located target) to its true-name; if a **second** true-name maps to an already-seen `(st_dev, st_ino)` AND the true-names differ, raise `RuntimeError("collision: %r and %r resolve to the same inode")`. This is the filesystem-truthful rule: on a case-insensitive FS two case-variant names collapse to one inode and collide; on a case-sensitive FS they are two inodes and coexist. `sidecar.read`'s `ValueError` propagates (loud corrupt-sidecar failure). `Database.init(store=None)` gains: when `store` is a `FileStore`, call `store.rebuild_index(db)` before returning.

- [ ] **Step 4: Run to green** — `PYTHONPATH=. python -m pytest tests/test_filestore_scan.py -q` → PASS.

- [ ] **Step 5: Mutation-check (each reverted)**
- Disable the `(st_dev, st_ino)` dedup in `rebuild_index` AND `_guard_collision` in Task 7. Expected: on a case-insensitive host, `test_case_collision_is_loud_on_case_insensitive_fs` FAILS (no raise). (On a case-sensitive host this test's else-branch is the one exercised; note in the commit that the collision-detection mutation must be run on a case-insensitive host or via a forced-collision fixture.)
- Swallow the `sidecar.read` `ValueError` in `rebuild_index`. Expected: `test_corrupt_sidecar_raises_loudly` FAILS.
- Make `object_purged` a no-op. Expected: `test_restore_removes_soft_deleted_on_disk` FAILS (the `sd_dir` survives). This is the ONLY gate that can see the restore reconciliation — B ≡ C cannot, because the external restore response is identical whether or not the disk copy is cleaned.

- [ ] **Step 6: Format, commit**

```bash
isort --quiet testbench/filestore.py testbench/database.py tests/test_filestore_scan.py && black --quiet testbench/filestore.py testbench/database.py tests/test_filestore_scan.py
git add testbench/filestore.py testbench/database.py tests/test_filestore_scan.py
git commit -m "feat(filestore): startup tree-scan, inode-based collision detection, index hydration"
```

**Safety gate:** memory harness empty diff (empty-root scan is a no-op; `Database.init(store=None)` path unchanged). Scan/collision/corruption/restore-reconciliation tests green; collision detection reflects the target FS's inode identity rule; the restore purge is gated by a dedicated unit test with a mutation check.

---

### Task 9: Wire the file backend end-to-end (Mechanism 1 + Mechanism 2 file leg live, loopback-bound)

**Files:**
- Modify: `testbench/rest_server.py` (module-level `db` construction, :30 — select store from env)
- Modify: `testbench/grpc_server.py` (`run` :1347-1348 — bind host via `_bind_host()`)
- Modify: `testbench_run.py` (`start_server` — refuse non-loopback `--bind` under file backend)
- Create: `conftest.py` (repo root)
- Modify: `tests/media_call_sites.txt`
- Test: add a disk-touch guard test and a loopback-bind test (in `tests/test_filestore.py` or a new `tests/test_file_backend_wiring.py`)

**Interfaces:**
- Produces: `Database.init()` selects `FileStore(os.environ["TESTBENCH_ROOT"])` when `TESTBENCH_STORE=file`, else `NullStore`. gRPC `run` binds `127.0.0.1` when the file backend is live. `testbench_run.start_server` raises if `--bind` host is non-loopback under the file backend. `conftest.py` keyed on `TESTBENCH_TEST_STORE=memory|file` makes both the direct-`init` callers and the live `rest_server.db` singleton file-backed at a per-test temp root.

- [ ] **Step 1: Env-driven store selection + loopback bind**

In `testbench/rest_server.py:30`, replace `db = testbench.database.Database.init()` with a branch on `os.environ.get("TESTBENCH_STORE", "memory")`: for `"file"`, require `TESTBENCH_ROOT` and build `Database.init(store=FileStore(os.environ["TESTBENCH_ROOT"]))` (hydration runs inside `init`); else `Database.init()`.

In `testbench/grpc_server.py`, add:
```python
def _bind_host():
    # The traversal-capable file backend must never listen off-loopback.
    return "127.0.0.1" if os.environ.get("TESTBENCH_STORE") == "file" else "0.0.0.0"
```
and change `run`'s bind to `server.add_insecure_port("%s:%d" % (_bind_host(), port))`. (Memory keeps `0.0.0.0`; goldens do not record the bind host, so the memory harness diff stays empty.)

In `testbench_run.py:start_server`, after parsing `sock_host`, add:
```python
if os.environ.get("TESTBENCH_STORE") == "file" and sock_host not in ("127.0.0.1", "localhost", "::1"):
    raise SystemExit("file backend refuses non-loopback bind host %r" % sock_host)
```

- [ ] **Step 2: Add `conftest.py` (Mechanism 1 — rebind the live singleton AND fall through explicit stores)**

The endpoint tests use the module singleton `testbench.rest_server.db` (reset with `.clear()`), so monkeypatching `Database.init` alone would leave them memory-backed (the singleton is built once at import). Swap the store on the SAME singleton object (preserving the `retry_test` decorator's captured reference and gRPC's shared reference), and also override `init` so direct-`init` callers become file-backed — but ONLY when they passed no store, so `Database.init(store=RecordingStore())` in the seam tests still gets its explicit store.

```python
# conftest.py  (repo root)
import os
import shutil
import tempfile

import pytest

import testbench.database
import testbench.rest_server

_ACTIVE_ROOT = {"path": None}


@pytest.fixture
def file_root():
    return _ACTIVE_ROOT["path"]


@pytest.fixture(autouse=True)
def _backend(monkeypatch):
    if os.environ.get("TESTBENCH_TEST_STORE", "memory") != "file":
        yield
        return
    root = tempfile.mkdtemp(prefix="testbench-unit-")
    _ACTIVE_ROOT["path"] = root
    from testbench.filestore import FileStore

    orig_init = testbench.database.Database.init.__func__

    @classmethod
    def init_file(cls, store=None):
        # Fall through when the caller supplied a store (e.g. RecordingStore in
        # the seam tests); inject FileStore only for the default no-store call.
        return orig_init(cls, store=store if store is not None else FileStore(root))

    monkeypatch.setattr(testbench.database.Database, "init", init_file)

    # Swap the store on the LIVE singleton so Flask-client endpoint tests hit
    # disk. Same object -> retry_test's closure and gRPC's shared db stay valid.
    db = testbench.rest_server.db
    prev_store = db._store
    db._store = FileStore(root)
    db.clear()  # cleared() wipes the (empty) tree; index reset; env bucket re-seeded to disk
    try:
        yield
    finally:
        db._store = prev_store
        _ACTIVE_ROOT["path"] = None
        shutil.rmtree(root, ignore_errors=True)
```

Note: retry-injection tests set instructions through the same singleton, so they remain consistent; the retry registry is transient state `FileStore` does not persist, which is correct (it is not part of B ≡ C durable state).

- [ ] **Step 3: Disk-touch guard test (proves the file leg is NOT vacuous)**

```python
# tests/test_file_backend_wiring.py
import os
import unittest

import pytest

import testbench.rest_server


@pytest.mark.skipif(os.environ.get("TESTBENCH_TEST_STORE") != "file",
                    reason="file leg only")
class TestFileLegTouchesDisk(unittest.TestCase):
    def test_bucket_create_writes_bucket_json(self, file_root=None):
        # obtain file_root via the fixture in a pytest-style test; shown here
        # as the intent: POST a bucket through the Flask test client and assert
        # <root>/<bucket>/.gcs/bucket.json exists on disk.
        client = testbench.rest_server.gcs.test_client()
        client.post("/storage/v1/b", query_string={"project": "test-project"},
                    json={"name": "disk-touch-bucket"})
        root = os.environ["_ACTIVE_ROOT"]  # or the file_root fixture
        self.assertTrue(os.path.exists(
            os.path.join(root, "disk-touch-bucket", ".gcs", "bucket.json")))
```
(Implement it as a pytest function taking the `file_root` fixture rather than the sketch above; the load-bearing assertion is `bucket.json` existing under the per-test root.)

- [ ] **Step 4: Extend `tests/media_call_sites.txt`**

Append the `FileStore` media read (`blob.media.to_bytes()` in `object_inserted`) as an active site, or as `# UNCOVERED testbench/filestore.py:<line> -- object_inserted media persist; only exercised under TESTBENCH_STORE=file` if the memory-config conformance trace does not execute it. Recompute the exact line number after formatting.

- [ ] **Step 5: Run both backends (Mechanism 1), the file harness leg (Mechanism 2), and the observed-root-leak grep**

```bash
TESTBENCH_TEST_STORE=memory PYTHONPATH=. python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
TESTBENCH_TEST_STORE=file   PYTHONPATH=. python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
PYTHONPATH=. python -m tests.conformance.harness --store file
# Real leak proof (now possible): capture a file-mode trace and confirm the
# volatile root prefix appears in NO observed value.
PYTHONPATH=. python -c "
from tests.conformance import harness
obs = harness.serialize(harness.capture('rest', store='file'))
assert 'testbench-conf-' not in obs, 'temp root leaked into an observed value'
print('no root leak')"
```
Expected: suite green on both legs; the disk-touch guard passes on the file leg; the file harness FAILS **only** on `create-bucket-traversal` — every other interaction byte-identical under the masked comparison. This is the moment the single deliberate B≠C difference appears.

- [ ] **Step 6: Loopback-bind adversarial test + mutation-check**

```python
    def test_file_backend_binds_loopback(self, monkeypatch):
        monkeypatch.setenv("TESTBENCH_STORE", "file")
        from testbench import grpc_server
        self.assertEqual("127.0.0.1", grpc_server._bind_host())
        monkeypatch.setenv("TESTBENCH_STORE", "memory")
        self.assertEqual("0.0.0.0", grpc_server._bind_host())
```
Also assert `testbench_run.start_server` raises under a non-loopback host with `TESTBENCH_STORE=file` (drive `start_server` with `sys.argv = ["x", "0.0.0.0", "8080", "10"]` and `assertRaises(SystemExit)`). Mutation-check: make `_bind_host` always return `"0.0.0.0"`; expected the loopback test FAILS. Revert.

- [ ] **Step 7: Format, commit (allow-list still empty until Task 10 records the entry)**

```bash
isort --quiet testbench/rest_server.py testbench/grpc_server.py testbench_run.py conftest.py && black --quiet testbench/rest_server.py testbench/grpc_server.py testbench_run.py conftest.py
git add testbench/rest_server.py testbench/grpc_server.py testbench_run.py conftest.py tests/media_call_sites.txt tests/test_file_backend_wiring.py
git commit -m "feat(filestore): env-driven store selection, loopback bind, conftest both-backend suite (file leg touches disk)"
```

**Safety gate:** memory harness still empty diff; file suite green (Mechanism 1) with a mutation-checked disk-touch guard proving the file leg is not vacuous; file harness green except the one anticipated divergence; no listener binds `0.0.0.0` under the file backend (mutation-checked); no volatile root leaks into an observed value.

---

### Task 10: Phase-4 exit — record the one allow-list entry, prove B ≡ C, extend CI

**Files:**
- Modify: `tests/conformance/allowlist.json` (add the single entry)
- Modify: `tests/test_store.py` (flip the pin: keep memory-accepts, add file-rejects)
- Modify: `.github/workflows/build.yaml` (add both file legs)
- Update the handoff.

- [ ] **Step 1: Record the single reviewed allow-list entry**

```json
{
  "create-bucket-traversal": "Deliberate B!=C: the file backend rejects the dotted/traversal bucket name '../../etc/passwd' (spec Security rule 4 -- FileStore.validate_bucket_name is the only check, since gcs/bucket.py:62's validator is deliberately left unchanged so the memory golden digest is preserved). Rejection is a clean 4xx on REST and INVALID_ARGUMENT on gRPC (context threaded through Database.insert_bucket). The memory backend still accepts it (create-bucket-traversal pinned in Task 2). Reviewed: PR <n>."
}
```

- [ ] **Step 2: Extend the seam pin in `tests/test_store.py`**

Keep `test_dotted_traversal_shaped_bucket_name_is_accepted_and_notified` (memory still accepts, unchanged). Add a sibling asserting a `FileStore` rejects it before commit with a proper status:

```python
    def test_filestore_rejects_dotted_traversal_bucket_name(self):
        import tempfile
        from testbench.filestore import FileStore
        db = testbench.database.Database.init(store=FileStore(tempfile.mkdtemp()))
        with self.assertRaises(Exception):
            db.insert_bucket(_make_bucket("../../etc/passwd"), None)
```

- [ ] **Step 3: Run the full phase-4 gate**

```bash
TESTBENCH_TEST_STORE=memory PYTHONPATH=. python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
TESTBENCH_TEST_STORE=file   PYTHONPATH=. python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
PYTHONPATH=. python -m tests.conformance.harness --store memory   # OK; digest = Task-2 value
PYTHONPATH=. python -m tests.conformance.harness --store file     # OK; diverges only on the allow-listed label
PYTHONPATH=. python -m pytest tests/test_media_call_sites.py -q
nix develop --command make verify-linux                           # both legs
```

- [ ] **Step 4: Mutation-check the allow-list is load-bearing**

Remove the `create-bucket-traversal` entry from `allowlist.json`. Re-run `python -m tests.conformance.harness --store file`. Expected: FAIL (the divergence is no longer tolerated). Restore. Then set the entry to a label that does NOT diverge (e.g. `"get-bucket"`); expected the harness FAILS via `stale_allowlist_labels`. Restore. This proves the entry is exact and unlisted diffs (inside or outside a block) fail.

- [ ] **Step 5: Add the CI file legs**

In `.github/workflows/build.yaml`: add `TESTBENCH_TEST_STORE=file` as a second `pytest` invocation in the `python-tests` job (matrix preserved, Python floor 3.8), and a `python -m tests.conformance.harness --store file` step in the `conformance` job alongside the existing memory run. Confirm `make verify-linux`'s inner command runs both harness legs. Watch the `conformance` job's `timeout-minutes: 15` budget — the startup tree-scan + per-test roots add wall time.

- [ ] **Step 6: Confirm invariants, commit, push, update handoff**

```bash
grep -rnE "import (gcs|testbench)" tests/conformance/ | grep -v emulator.py   # expect empty
git diff --name-only e8c8507..HEAD -- setup.py                                 # expect empty (zero new runtime deps)
isort --quiet tests/test_store.py && black --quiet tests/test_store.py
git add tests/conformance/allowlist.json tests/test_store.py .github/workflows/build.yaml
git commit -m "test(filestore): record the single reviewed B-vs-C allow-list entry; add file CI legs"
git push origin file-backend-design
gh run list --branch file-backend-design --limit 3
```
Update the handoff: phase 4 landed; memory digest unchanged from the Task-2 value; the allow-list holds exactly one entry (`create-bucket-traversal`); `FileStore` owns the on-disk layout and the `.gcs/uploads/<id>` staging + natural/`generations`/`soft_deleted`/`overflow` destinations that phase 5's `FileMedia.finalize()` writes into; the file backend is loopback-bound; `gcs/bucket.py`'s validator remains deliberately unfixed for the memory backend.

**Safety gate (phase-4 exit):** suite green on both `TESTBENCH_TEST_STORE` legs (file leg proven non-vacuous by the disk-touch guard); memory harness byte-identical (digest = Task-2 value); file harness byte-identical **except** the one mutation-checked allow-list entry; no listener binds `0.0.0.0` under the file backend; `make verify-linux` green on both legs; media coverage gate green; `setup.py` unchanged; nothing in `tests/conformance/` imports internals except `emulator.py`.

---

## Self-Review

**Spec coverage (phase-4 row of the per-phase gate + Security/On-disk-layout sections):**
- `FileStore` implementing all 11 notifications + new `bucket_updated` → Tasks 3, 7. ✅
- Bucket-metadata bypass fix (`bucket_update`/`patch`/ACL/DOACL/IAM/lockRetention, REST + gRPC) routed through `Database.do_update_bucket` as a zero-diff refactor; the un-traced reroutes are response-preservation-gated by the existing endpoint suite (Task 3 Step 7 mutation-checks one) → Task 3. ✅
- On-disk layout, name escaping, overflow store (7 unrepresentable clauses, each tested + mutation-checked), sidecar persistence → Tasks 4, 6, 7. ✅
- Security rules 1–6: realpath containment + **O_NOFOLLOW openat on every write component, wired live and proven by the planted-symlink test** (rules 1, 3, 5); reject-not-sanitise via pre-commit `validate_bucket_name(name, context)` (clean 4xx/INVALID_ARGUMENT) + object→overflow (rule 2); file-backend-only bucket validation (rule 4); **loopback bind for the file backend, pulled into phase 4** (rule 6) → Tasks 4, 5, 7, 9. ✅
- Startup tree-scan, **inode-based** collision detection (FS-truthful, fires on case-insensitive host), hydration → Task 8. ✅
- Carried-forward `object_updated` advisory fire → Task 7 idempotent handler. Restore's stale soft-deleted copy → Task 3 `object_purged` signal for the ORIGINAL generation, reconciled by Task 7's handler, **as the single mechanism** (no duplicate cleanup in `object_inserted`), gated by a dedicated Task 8 unit test. ✅
- B ≡ C proof: Mechanism 1 (both-backend suite via `conftest.py`, **non-vacuous** — the live singleton is rebound and a disk-touch guard proves endpoint ops hit disk), Mechanism 2 (file-config harness + **byte-exact masked** overlay), Mechanism 4 (hypothesis + adversarial name/containment suites) → Tasks 1, 9, 4, 5. ✅
- The one deliberate B≠C difference recorded in a reviewed, mutation-checked allow-list; `--regenerate` never turns a red gate green → Tasks 1, 2, 10. ✅

**Deliberate design decisions (resolving the review):**
1. `gcs/bucket.py` validator left UNCHANGED — fixing it moves the memory golden and breaks A ≡ B; the file backend's own check is the only one (spec rule 4). The dotted-bucket rejection is a genuine B≠C entry.
2. Pre-commit `validate_bucket_name(name, context)` threads the gRPC context so the rejection is a clean 4xx (REST) / INVALID_ARGUMENT (gRPC), not a post-commit 500 or an uncaught UNKNOWN. Object names are never rejected — unrepresentable/hostile object names route to `.gcs/overflow/<sha256>` (no caller bytes), so object handlers are total.
3. `do_update_bucket` mirrors `do_update_object`'s unconditional-notify precedent; a single mutator funnels every bucket-metadata path on both transports.
4. **Exactly one** restore-reconciliation mechanism: `object_purged` for the original generation (fired by `restore_object`). `object_inserted` does NO soft-deleted cleanup — the `generation - 1` guess is removed.
5. **safe_open/walk_dirs are LIVE** in the media/sidecar/bucket write and rmtree paths, opening every component with O_NOFOLLOW; the planted-symlink test proves they are not dead code. `write_atomic` is fd-based (`write_atomic(dir_fd, filename, text)`), matching every call site.
6. **Collision detection is inode-identity based** (`st_dev`/`st_ino`) plus a write-time true-name guard, so it fires exactly when the target FS collapses two names — case-insensitive host detected, case-sensitive host correctly silent. Tests probe the FS and branch.
7. **The file backend never listens off-loopback** (gRPC `_bind_host()` + REST bootstrap refusal), a phase-4 exit-gate item, not a phase-6 deferral.
8. The file-leg harness gate is a **byte-exact masked comparison** (mask allow-listed blocks in both sides, compare the full remaining text), strictly as strong as the memory leg; duplicate/unlabeled blocks are hard errors so no block can shadow another.
9. Phase 4 persists the whole (small) `BytesMedia` buffer via `containment.write_bytes_atomic` (staging + `os.replace`); the outside-the-lock streaming discipline is phase 5's `FileMedia`.

**Placeholder scan:** no `TBD`/`TODO`/"handle edge cases"; every code step shows real code and every run step a real command with an expected result. The gRPC `_apply` closure (Task 3 Step 5) is written out with `now` bound inside; a full worked DOACL handler with the holder cell is shown (Task 3 Step 4). Private helpers named in Task 7 Step 3 (`_unlink_quiet`, `_move_quiet`, `_write_folder`, `_remove_folder`, `_guard_collision`) carry exact contracts.

**Type consistency:** `pathing.classify` → `(str, str)`; `pathing.validate_bucket_name` raises `ValueError`; the Store boundary (`FileStore.validate_bucket_name(name, context)`) translates to `testbench.error.invalid(msg, context)`; `sidecar.load` → `(kind, name, proto)`; `sidecar.write_atomic(dir_fd, filename, text)` and `containment.write_bytes_atomic(dir_fd, name, data)` are fd-based and match all Task 7 call sites; `capture(name, store="memory")`/`verify(name, store="memory")` carry defaults so the regenerate branch is unaffected; `Store.validate_bucket_name(self, name, context=None)` matches `Database.insert_bucket`'s call. `blob.media.to_bytes()` matches the `BytesMedia` surface the phase-3 handoff pins.

**Known risk carried into execution:** filesystem case-sensitivity and `NAME_MAX` differ between the macOS capture host and Linux CI; `make verify-linux` (Task 10 Step 3) and the CI file leg (Step 5) are the mitigation, with CI authoritative. The collision-detection mutation check must run on a case-insensitive host (or a forced-collision fixture) — flagged in Task 8 Step 5. The `conformance` job's 15-minute budget is the one capacity risk once the tree-scan and per-test roots are live — flagged in Task 10 Step 5.