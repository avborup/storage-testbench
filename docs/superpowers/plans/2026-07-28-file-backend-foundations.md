# File Backend Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the reproducible dev environment, capture a black-box behavioral baseline of the emulator, and land the `Store` seam inside `Database` — all with provably zero change to external behavior.

**Architecture:** Three independent pieces. A `flake.nix` devShell provides the interpreter and toolchain while a pinned venv provides exact dependency versions. A black-box conformance harness in `tests/conformance/` drives a subprocess emulator purely over HTTP and gRPC, canonicalizes non-deterministic fields through a symbol table, and diffs against committed goldens. A `Store` protocol in `testbench/store.py` is called from every mutating method of `Database`, with `NullStore` as the default so behavior is unchanged.

**Tech Stack:** Python 3.8–3.12, Flask/Werkzeug, gRPC (`google.storage.v2` stubs already generated in-repo), pytest, coverage.py, Nix flakes, `requests`.

**Spec:** `docs/superpowers/specs/2026-07-27-file-backend-design.md` — this plan implements phases 1 and 2 and Mechanism 2 of the verification plan.

## Global Constraints

Every task's requirements implicitly include this section.

- **Python floor is 3.8.** `setup.py` declares `python_requires=">=3.8"` and CI runs a 3.8–3.12 matrix. No `match` statements, no PEP 604 `X | Y` annotations evaluated at runtime, no `dict1 | dict2`, no `functools.cache`, no positional-only `/` markers. Use `typing.Optional`/`typing.Dict`.
- **No new runtime dependencies.** `setup.py`'s `install_requires` must not grow. Test-only and dev-only dependencies live in `flake.nix` and CI, never in `setup.py`. This preserves the upstreaming option.
- **Formatting is enforced by CI and must be exact:** `black==22.3.0` and `isort==5.12.0`. `.github/workflows/style.yaml` runs both and then `git diff --exit-code`, so unformatted code fails the build. Generated files (`*_pb2.py`, `*_pb2_grpc.py`) are excluded.
- **Windows CI must stay green.** `.github/workflows/build.yaml` runs the suite on `windows-2022` with Python 3.11. Anything POSIX-only must be skipped there, not merely untested. (See "Spec gap" below.)
- **Tests run as** `PYTHONPATH="." pytest` from the repository root. The existing suite is also runnable via `python -m unittest discover -s tests/`.
- **The existing test suite must pass unmodified** at the end of every task in this plan. No existing test file is edited by this plan.
- **Apache 2.0 licence header** on every new `.py` and `.nix` file. Task 1's `tests/test_crc32c_assumptions.py` shows the exact 13-line form; later tasks write `# ... (licence header) ...` as shorthand for "copy those 13 lines verbatim, changing nothing". A file without it fails review.

### Deferred to later plans

Named here so their absence is deliberate rather than an oversight:

- **Mechanism 1** (running the existing suite against both backends via a `conftest.py` parametrized on `TESTBENCH_TEST_STORE`) belongs to Plan 3. It is meaningless until `FileStore` exists, and adding a no-op parametrization now would only slow CI.
- **Mechanism 3** (the coverage-gated `tests/media_call_sites.txt` audit) belongs to Plan 2, which introduces the `Media` seam the checklist describes.
- **Mechanism 4** (`hypothesis` property-based name round-trips) belongs to Plan 3, which is why `hypothesis` is absent from `flake.nix` here.
- **Mechanisms 5 through 9** (large-object bounds, durability, concurrency, real-GCS cross-validation, the downstream Rust client) belong to Plan 4.

### Spec gap recorded: Windows

The spec does not address Windows, but CI runs there. The file backend in later plans is POSIX-only — `O_NOFOLLOW`, `openat`, and `NAME_MAX` semantics have no portable Windows equivalent, and NTFS is case-insensitive by default, which conflicts with the collision-detection rule. Nothing in *this* plan is POSIX-only, so no skips are needed yet; Plan 3 must mark every file-backend test `skipif(os.name == "nt")` and the spec should record the file backend as POSIX-only. Deployment is Linux containers, so this costs nothing.

### A note on "pristine main"

The goldens must capture behavior *before* production code changes. "Pristine" here means **no changes to `gcs/` or `testbench/`**, not "no changes to the repo" — the harness itself is additive, test-only, black-box code that cannot alter emulator behavior. Goldens are therefore captured at the end of Task 6, before Task 7 touches `testbench/database.py`.

---

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `flake.nix` | devShell: interpreter, docker client, venv bootstrap |
| `.gitignore` (modify) | ignore `.venv/`, `.direnv/`, flake artifacts |
| `tests/test_crc32c_assumptions.py` | pins the incremental-crc32c assumption the design rests on |
| `tests/conformance/__init__.py` | package marker |
| `tests/conformance/symbols.py` | `SymbolTable` — stable placeholders preserving identity |
| `tests/conformance/canonicalize.py` | walks recorded JSON/headers, applies the symbol table, asserts ordering invariants |
| `tests/conformance/recorder.py` | `Recorder` — append-only record of canonicalized interactions |
| `tests/conformance/emulator.py` | subprocess lifecycle: start emulator, wait for readiness, start gRPC, tear down |
| `tests/conformance/trace_rest.py` | the JSON and XML API trace |
| `tests/conformance/trace_grpc.py` | the gRPC v2 trace |
| `tests/conformance/trace_faults.py` | the fault-injection trace |
| `tests/conformance/harness.py` | CLI: run traces, write or diff goldens |
| `tests/conformance/golden/*.json` | committed baseline (configuration A) |
| ~~`tests/conformance/allowlist.json`~~ | **Removed after review — see "Allowlist: removed" below.** |
| `tests/test_conformance.py` | pytest entry point that runs the harness and diffs |
| `testbench/store.py` | `Store` protocol and `NullStore` |
| `tests/test_store.py` | `RecordingStore` tests pinning the `Database` → `Store` contract |

**Modified:**

| Path | Change |
|---|---|
| `testbench/database.py` | accept an optional `store`; notify it from every mutating method |
| `.github/workflows/build.yaml` | add a conformance job |
| `README.md` | document the devShell and the harness |

Rationale for the split inside `tests/conformance/`: the symbol table, the canonicalizer, and the recorder are each independently unit-testable and have no knowledge of GCS; the three `trace_*.py` files hold domain knowledge and no infrastructure; `emulator.py` is the only file that knows about subprocesses. `harness.py` wires them together and is the only file with a CLI.

---

### Task 1: Nix devShell and the crc32c assumption

The design's incremental-checksum plan rests on an unverified claim about the `crc32c` package. Verify it first, because if it is false, `FileMedia` needs a different implementation. The devShell exists to make that verification (and everything after) reproducible.

**Files:**
- Create: `flake.nix`
- Create: `tests/test_crc32c_assumptions.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: a `nix develop` shell in which `pytest` and `python -m testbench` work; a committed assertion that `crc32c.crc32c(data, seed)` chains correctly.

- [ ] **Step 1: Write the failing test**

Create `tests/test_crc32c_assumptions.py`:

```python
#!/usr/bin/env python3
#
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

"""Pin the third-party assumptions the file backend design depends on.

The file backend replaces whole-buffer checksums with incremental ones. That
is only correct if `crc32c.crc32c()` accepts a seed and chains, and if
`hashlib.md5()` supports incremental updates. If either assumption breaks
under a dependency upgrade, this test fails loudly rather than silently
producing wrong checksums for every uploaded object.
"""

import hashlib
import unittest

import crc32c


class TestCrc32cAssumptions(unittest.TestCase):
    def test_crc32c_accepts_a_seed_and_chains(self):
        whole = crc32c.crc32c(b"helloworld")
        chained = crc32c.crc32c(b"world", crc32c.crc32c(b"hello"))
        self.assertEqual(whole, chained)

    def test_crc32c_chains_across_many_chunks(self):
        payload = bytes(range(256)) * 97
        whole = crc32c.crc32c(payload)
        chained = 0
        for offset in range(0, len(payload), 1000):
            chained = crc32c.crc32c(payload[offset : offset + 1000], chained)
        self.assertEqual(whole, chained)

    def test_crc32c_of_empty_input_is_zero_seed_identity(self):
        self.assertEqual(crc32c.crc32c(b""), 0)
        self.assertEqual(crc32c.crc32c(b"", 12345), 12345)

    def test_md5_supports_incremental_update(self):
        payload = bytes(range(256)) * 97
        incremental = hashlib.md5()
        for offset in range(0, len(payload), 1000):
            incremental.update(payload[offset : offset + 1000])
        self.assertEqual(hashlib.md5(payload).digest(), incremental.digest())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails for the right reason**

Run: `PYTHONPATH="." python3 -m pytest tests/test_crc32c_assumptions.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'crc32c'`. The dependency is not installed yet — that is the failure this task exists to fix. Do not proceed until you have seen this exact error; a different error means something else is wrong.

- [ ] **Step 3: Create the flake**

Create `flake.nix`:

```nix
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
{
  description = "Development environment for the GCS storage testbench";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            # 3.12 matches the Dockerfile base image. The CI matrix also
            # covers 3.8-3.11; use nix-shell -p python38 to reproduce those.
            pkgs.python312
            pkgs.docker-client
            pkgs.docker-compose
            pkgs.curl
            pkgs.jq
          ];

          shellHook = ''
            # Dependency versions are pinned in setup.py and must match what
            # the Dockerfile and CI install, so they come from pip rather than
            # nixpkgs. Nix supplies the interpreter and the toolchain; the venv
            # supplies exact versions. Re-provision only when setup.py changes.
            export VENV=.venv
            stamp="$VENV/.provisioned"
            want=$(cksum setup.py | cut -d' ' -f1)
            # The steps are &&-chained so that a failure aborts before the
            # stamp is written. Without the chain, a failed `pip install -e .`
            # would still stamp the venv as provisioned, and every later
            # `nix develop` would silently reuse the broken venv until
            # setup.py changed -- surfacing as inexplicable task failures.
            if [ ! -f "$stamp" ] || [ "$(cat "$stamp")" != "$want" ]; then
              echo "provisioning $VENV from setup.py ..."
              ${pkgs.python312}/bin/python3 -m venv "$VENV" \
                && "$VENV/bin/pip" install --quiet --upgrade pip \
                && "$VENV/bin/pip" install --quiet -e . \
                && "$VENV/bin/pip" install --quiet \
                     pytest pytest-cov coverage requests \
                     black==22.3.0 isort==5.12.0 \
                && echo "$want" > "$stamp"
            fi
            source "$VENV/bin/activate"
            export PYTHONPATH="$PWD"
            echo "storage-testbench devShell: python $(python3 --version 2>&1 | cut -d' ' -f2)"
          '';
        };
      });
}
```

Note on the tradeoff: pinning twelve dependencies through nixpkgs would drift from `setup.py`'s exact versions, and behavioral fidelity to what CI and the Dockerfile install matters more here than a fully hermetic closure. `black` and `isort` are pinned to the versions `style.yaml` enforces. `hypothesis` is deliberately absent — Plan 3 introduces it when Mechanism 4 lands.

- [ ] **Step 4: Add ignores**

Append to `.gitignore`:

```
.venv/
.direnv/
result
result-*
```

- [ ] **Step 5: Enter the shell and run the test to verify it passes**

Run:
```bash
nix develop --command bash -c 'pytest tests/test_crc32c_assumptions.py -v'
```

Expected: 4 passed.

**If `test_crc32c_accepts_a_seed_and_chains` fails**, the design assumption is wrong. Stop and report it — the fix is to implement CRC chaining manually via `crc32c.crc32c` over a running buffer or a table-driven combine, and the spec's "Large-object handling" section needs amending. Do not work around it silently.

- [ ] **Step 6: Verify the existing suite is green in the shell**

Run:
```bash
nix develop --command bash -c 'pytest -q 2>&1 | tail -20'
```

Expected: all tests pass. This is the baseline every later task must preserve. Record the passing count in the commit message.

- [ ] **Step 7: Format and commit**

```bash
nix develop --command bash -c 'isort --quiet tests/test_crc32c_assumptions.py && black --quiet tests/test_crc32c_assumptions.py && git diff --exit-code'
git add flake.nix flake.lock .gitignore tests/test_crc32c_assumptions.py
git commit -m "build: add nix devShell and pin third-party checksum assumptions"
```

---

### Task 2: Symbol table

The canonicalizer's job is to erase non-deterministic values *without* erasing the relationships between them. A naive scrubber that maps every generation to `"<GEN>"` would hide a bug where two objects wrongly share a generation. A symbol table binds each distinct value to a numbered placeholder on first sight and reuses it thereafter, so identity survives and only the value is lost.

**Files:**
- Create: `tests/conformance/__init__.py`
- Create: `tests/conformance/symbols.py`
- Create: `tests/conformance/test_symbols.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SymbolTable` with `bind(kind: str, value) -> str` and `bindings() -> dict`. Task 3 consumes it.

- [ ] **Step 1: Write the failing test**

Create `tests/conformance/test_symbols.py`:

```python
#!/usr/bin/env python3
#
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

"""Unit tests for the conformance harness symbol table."""

import unittest

from tests.conformance.symbols import SymbolTable


class TestSymbolTable(unittest.TestCase):
    def test_first_binding_is_numbered_one(self):
        table = SymbolTable()
        self.assertEqual("<GEN:1>", table.bind("GEN", 1753000000000001))

    def test_same_value_reuses_its_placeholder(self):
        table = SymbolTable()
        first = table.bind("GEN", 1753000000000001)
        second = table.bind("GEN", 1753000000000001)
        self.assertEqual(first, second)

    def test_distinct_values_get_distinct_placeholders(self):
        table = SymbolTable()
        self.assertEqual("<GEN:1>", table.bind("GEN", 1753000000000001))
        self.assertEqual("<GEN:2>", table.bind("GEN", 1753000000000002))

    def test_counters_are_per_kind(self):
        table = SymbolTable()
        self.assertEqual("<GEN:1>", table.bind("GEN", 7))
        self.assertEqual("<UPLOAD:1>", table.bind("UPLOAD", "abc"))

    def test_equal_values_of_different_kinds_do_not_alias(self):
        # A generation of 7 and an upload id of "7" are unrelated facts, and
        # collapsing them would let a bug that confuses the two go unnoticed.
        table = SymbolTable()
        self.assertEqual("<GEN:1>", table.bind("GEN", 7))
        self.assertEqual("<UPLOAD:1>", table.bind("UPLOAD", 7))

    def test_int_and_str_spellings_of_a_value_alias(self):
        # The JSON API returns generations as strings, gRPC as ints. The same
        # underlying generation must canonicalize identically across both.
        table = SymbolTable()
        self.assertEqual("<GEN:1>", table.bind("GEN", 1753000000000001))
        self.assertEqual("<GEN:1>", table.bind("GEN", "1753000000000001"))

    def test_bindings_are_reported_for_diagnostics(self):
        table = SymbolTable()
        table.bind("GEN", 7)
        self.assertEqual({("GEN", "7"): "<GEN:1>"}, table.bindings())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `nix develop --command bash -c 'pytest tests/conformance/test_symbols.py -v'`

Expected: FAIL — `ModuleNotFoundError: No module named 'tests.conformance'`.

- [ ] **Step 3: Write the implementation**

Create `tests/conformance/__init__.py` containing only the licence header and:

```python
"""Black-box conformance harness for the storage testbench.

Nothing in this package may import `testbench` or `gcs` internals, with the
sole exception of `emulator.py`, which needs a module path to launch a
subprocess, and the generated `google.storage.v2` gRPC stubs, which are the
public API surface rather than internals. The harness must measure external
behavior only, so that it stays valid across the refactors it exists to
police.
"""
```

Create `tests/conformance/symbols.py`:

```python
#!/usr/bin/env python3
#
# Copyright 2026 Google LLC
#
# ... (full Apache 2.0 header as above) ...

"""Stable placeholders for non-deterministic values."""


class SymbolTable:
    """Maps non-deterministic values to stable, numbered placeholders.

    Values are erased but identity is preserved: the same value always
    produces the same placeholder, and distinct values never collide. That
    keeps aliasing bugs visible -- two objects sharing a generation, or a
    rewrite token leaking between requests -- which a scrubber that mapped
    everything to a constant would hide.

    Values are keyed by their string spelling because the JSON API renders
    64-bit integers as strings while gRPC renders them as ints, and both must
    canonicalize to the same placeholder.
    """

    def __init__(self):
        self._bindings = {}
        self._counters = {}

    def bind(self, kind, value):
        key = (kind, str(value))
        if key not in self._bindings:
            count = self._counters.get(kind, 0) + 1
            self._counters[kind] = count
            self._bindings[key] = "<%s:%d>" % (kind, count)
        return self._bindings[key]

    def bindings(self):
        return dict(self._bindings)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command bash -c 'pytest tests/conformance/test_symbols.py -v'`

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
nix develop --command bash -c 'isort --quiet tests/conformance/ && black --quiet tests/conformance/ && git diff --exit-code'
git add tests/conformance/
git commit -m "test: add symbol table for conformance canonicalization"
```

---

### Task 3: Canonicalizer

**Files:**
- Create: `tests/conformance/canonicalize.py`
- Create: `tests/conformance/test_canonicalize.py`

**Interfaces:**
- Consumes: `SymbolTable` from Task 2.
- Produces: `Canonicalizer` with `body(obj)`, `headers(mapping)`, and `assert_invariants()`. `NONDETERMINISTIC_FIELDS` maps field name to symbol kind. `DROPPED_HEADERS` is the set of headers removed entirely. Task 4 consumes all of these.

Field handling comes from the spec's canonicalization table. `metageneration` and `etag` are deliberately **not** canonicalized: `etag` is an md5 of `metageneration` (`gcs/object.py:91`), both are deterministic, and both are real signal.

- [ ] **Step 1: Write the failing test**

Create `tests/conformance/test_canonicalize.py`:

```python
#!/usr/bin/env python3
#
# Copyright 2026 Google LLC
#
# ... (full Apache 2.0 header) ...

"""Unit tests for conformance canonicalization."""

import unittest

from tests.conformance.canonicalize import Canonicalizer


class TestCanonicalizer(unittest.TestCase):
    def setUp(self):
        self.canon = Canonicalizer()

    def test_generation_is_replaced(self):
        out = self.canon.body({"generation": "1753000000000001"})
        self.assertEqual({"generation": "<GEN:1>"}, out)

    def test_timestamps_are_replaced(self):
        out = self.canon.body({"timeCreated": "2026-07-28T10:00:00.000Z"})
        self.assertEqual({"timeCreated": "<TIME:1>"}, out)

    def test_metageneration_and_etag_are_preserved(self):
        payload = {"metageneration": "3", "etag": "eccbc87e4b5ce2fe28308fd9f2a7baf3"}
        self.assertEqual(payload, self.canon.body(payload))

    def test_nested_and_listed_values_are_replaced(self):
        out = self.canon.body(
            {"items": [{"generation": "10"}, {"generation": "11"}]}
        )
        self.assertEqual(
            {"items": [{"generation": "<GEN:1>"}, {"generation": "<GEN:2>"}]}, out
        )

    def test_repeated_generation_reuses_its_placeholder(self):
        out = self.canon.body(
            {"a": {"generation": "10"}, "b": {"generation": "10"}}
        )
        self.assertEqual(out["a"]["generation"], out["b"]["generation"])

    def test_generation_embedded_in_a_link_is_replaced(self):
        out = self.canon.body(
            {
                "generation": "10",
                "mediaLink": "http://h/download/storage/v1/b/bk/o/o?generation=10&alt=media",
            }
        )
        self.assertEqual("<GEN:1>", out["generation"])
        self.assertIn("<GEN:1>", out["mediaLink"])
        self.assertNotIn("10", out["mediaLink"].replace("v1", ""))

    def test_volatile_headers_are_dropped(self):
        out = self.canon.headers({"Date": "Tue, 28 Jul 2026 10:00:00 GMT", "ETag": "x"})
        self.assertNotIn("Date", out)
        self.assertEqual("x", out["etag"])

    def test_header_names_are_lowercased_for_stability(self):
        out = self.canon.headers({"X-Goog-Generation": "10"})
        self.assertEqual({"x-goog-generation": "<GEN:1>"}, out)

    def test_body_and_headers_share_one_symbol_space(self):
        body = self.canon.body({"generation": "10"})
        headers = self.canon.headers({"x-goog-generation": "10"})
        self.assertEqual(body["generation"], headers["x-goog-generation"])

    def test_monotonic_generations_pass_invariants(self):
        self.canon.body({"generation": "10"})
        self.canon.body({"generation": "11"})
        self.canon.assert_invariants()

    def test_non_monotonic_generations_fail_invariants(self):
        # Generations are assigned from a monotonic counter, so a decrease is
        # a real defect and must not be canonicalized away.
        self.canon.body({"generation": "11"})
        self.canon.body({"generation": "10"})
        with self.assertRaises(AssertionError):
            self.canon.assert_invariants()

    def test_malformed_timestamp_fails_invariants(self):
        self.canon.body({"timeCreated": "not-a-timestamp"})
        with self.assertRaises(AssertionError):
            self.canon.assert_invariants()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `nix develop --command bash -c 'pytest tests/conformance/test_canonicalize.py -v'`

Expected: FAIL — `ModuleNotFoundError: No module named 'tests.conformance.canonicalize'`.

- [ ] **Step 3: Write the implementation**

Create `tests/conformance/canonicalize.py`:

```python
#!/usr/bin/env python3
#
# Copyright 2026 Google LLC
#
# ... (full Apache 2.0 header) ...

"""Replace non-deterministic response fields with stable placeholders."""

import re

from tests.conformance.symbols import SymbolTable

# Field name (JSON API and gRPC spellings) -> symbol kind. Taken from the
# canonicalization table in the file backend design spec.
NONDETERMINISTIC_FIELDS = {
    "generation": "GEN",
    "sourceGeneration": "GEN",
    "source_generation": "GEN",
    "timeCreated": "TIME",
    "create_time": "TIME",
    "updated": "TIME",
    "update_time": "TIME",
    "timeFinalized": "TIME",
    "finalize_time": "TIME",
    "softDeleteTime": "TIME",
    "soft_delete_time": "TIME",
    "hardDeleteTime": "TIME",
    "hard_delete_time": "TIME",
    "timeStorageClassUpdated": "TIME",
    "customTime": "TIME",
    "uploadId": "UPLOAD",
    "upload_id": "UPLOAD",
    "rewriteToken": "REWRITE",
    "rewrite_token": "REWRITE",
    "id": "ID",
    # Link fields are NOT bound wholesale. They are composite: a volatile
    # origin (the emulator binds an ephemeral port, so scheme://host:port
    # differs every run) followed by a path and query that are meaningful
    # behavior worth diffing. Binding the whole URL would erase the port but
    # also hide a regression in the emulator's URL scheme, and would hide a
    # link that points at the wrong generation. Instead `_canonical_link`
    # binds only the origin and leaves the rest to substitution, so the
    # generation embedded in a link aliases the sibling `generation` field.
    "selfLink": "LINK",
    "mediaLink": "LINK",
}

# Headers whose values are inherently volatile or are recomputed by the
# server framework rather than by the emulator.
DROPPED_HEADERS = frozenset(
    [
        "date",
        "server",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "content-length",
    ]
)

HEADER_FIELDS = {
    "x-goog-generation": "GEN",
    "x-goog-metageneration": None,  # deterministic, keep verbatim
}

_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


class Canonicalizer:
    """Canonicalizes recorded bodies and headers through one symbol space.

    Bodies and headers share a single `SymbolTable` so that a generation
    reported in a header and in a body canonicalize identically; a mismatch
    between the two is a bug worth catching.
    """

    def __init__(self):
        self._symbols = SymbolTable()
        self._generations = []
        self._timestamps = []

    def body(self, obj):
        return self._walk(obj)

    def headers(self, mapping):
        out = {}
        for raw_name, value in mapping.items():
            name = raw_name.lower()
            if name in DROPPED_HEADERS:
                continue
            kind = HEADER_FIELDS.get(name)
            if kind is not None:
                out[name] = self._bind(kind, value)
            else:
                out[name] = self._substitute_known_values(str(value))
        return out

    def assert_invariants(self):
        """Assert the properties canonicalization would otherwise hide."""
        ordered = self._generations
        for earlier, later in zip(ordered, ordered[1:]):
            assert earlier <= later, (
                "generations must be non-decreasing in the order first seen; "
                "saw %d then %d" % (earlier, later)
            )
        for value in self._timestamps:
            assert _RFC3339.match(value), "not an RFC 3339 timestamp: %r" % (value,)

    def _walk(self, node):
        if isinstance(node, dict):
            return {k: self._walk_value(k, v) for k, v in node.items()}
        if isinstance(node, list):
            return [self._walk(v) for v in node]
        if isinstance(node, str):
            return self._substitute_known_values(node)
        return node

    def _walk_value(self, key, value):
        kind = NONDETERMINISTIC_FIELDS.get(key)
        if kind is not None and isinstance(value, (str, int)):
            return self._bind(kind, value)
        return self._walk(value)

    def _bind(self, kind, value):
        if kind == "GEN":
            self._generations.append(int(value))
        if kind == "TIME" and isinstance(value, str):
            self._timestamps.append(value)
        return self._symbols.bind(kind, value)

    def _substitute_known_values(self, text):
        """Replace already-bound values appearing inside free-form strings.

        Links and `Content-Range`-style headers embed generations and upload
        ids. Substituting bound values keeps those strings comparable without
        needing a parser per field.
        """
        result = text
        for (_, value), placeholder in sorted(
            self._symbols.bindings().items(), key=lambda kv: -len(kv[0][1])
        ):
            if value and value in result:
                result = result.replace(value, placeholder)
        return result
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command bash -c 'pytest tests/conformance/test_canonicalize.py -v'`

Expected: 12 passed.

**Substitution is scoped, and binding is a separate whole-tree pass.** Two defects in an earlier revision of this task, both confirmed by executing the code, motivate the structure below.

*Over-erasure.* Substituting every bound value into every string corrupts unrelated fields: with `{"generation": "1", "name": "file-v1-final.txt"}` the name became `"file-v<GEN:1>-final.txt"`. It fires on real GCS payloads too, since an `id` field's text is a substring of the `selfLink` path. A canonicalizer that mangles data erases the very regressions it exists to catch. Substitution therefore applies **only** where composite values genuinely live — link fields and header values — and never to arbitrary body strings.

*Cross-depth ordering.* Substitution can only replace values already bound, so binding must complete before any emitting begins. Ordering the keys within a single dict is not enough: a link at one depth and its `generation` at another still race. Bind the whole tree first, then emit the whole tree.

```python
    def body(self, obj):
        # Two whole-tree passes. Binding must complete before emitting,
        # because a link can only have an embedded value substituted once
        # that value is bound, and the two may sit at different depths.
        self._bind_pass(obj)
        return self._emit(obj)

    def _bind_pass(self, node):
        if isinstance(node, dict):
            for key, value in node.items():
                kind = NONDETERMINISTIC_FIELDS.get(key)
                if kind is not None and kind != "LINK" and isinstance(value, (str, int)):
                    self._bind(kind, value)
                    continue
                self._bind_pass(value)
        elif isinstance(node, list):
            for value in node:
                self._bind_pass(value)

    def _emit(self, node):
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                kind = NONDETERMINISTIC_FIELDS.get(key)
                if kind == "LINK" and isinstance(value, str):
                    out[key] = self._canonical_link(value)
                elif kind is not None and isinstance(value, (str, int)):
                    # Already bound in the first pass; bind() is idempotent,
                    # and going through the symbol table directly avoids
                    # recording the value twice for the invariant checks.
                    out[key] = self._symbols.bind(kind, value)
                else:
                    out[key] = self._emit(value)
            return out
        if isinstance(node, list):
            return [self._emit(value) for value in node]
        # Strings are left alone. Only links and headers are composite.
        return node
```

`headers()` keeps its substitution — `Content-Range`, `Location`, and the resumable session URI all embed bound values — and `_ORIGIN` must be case-insensitive, because URI schemes are case-insensitive per RFC 3986 §3.1 and a lowercase-only pattern would let `HTTP://127.0.0.1:51423/…` pass through with its ephemeral port intact:

```python
_ORIGIN = re.compile(r"^[a-z][a-z0-9+.-]*://[^/]+", re.IGNORECASE)
```

Tests pinning all three properties:

```python
    def test_a_bound_value_does_not_corrupt_an_unrelated_string(self):
        # The canonicalizer must not rewrite fields that merely contain a
        # bound value as a substring; doing so would hide real regressions
        # behind mangled data.
        out = self.canon.body({"generation": "1", "name": "file-v1-final.txt"})
        self.assertEqual("<GEN:1>", out["generation"])
        self.assertEqual("file-v1-final.txt", out["name"])

    def test_a_link_substitutes_a_generation_bound_at_another_depth(self):
        out = self.canon.body(
            {
                "selfLink": "http://h/o?generation=10",
                "inner": {"generation": "10"},
            }
        )
        self.assertEqual("<ORIGIN:1>/o?generation=<GEN:1>", out["selfLink"])
        self.assertEqual("<GEN:1>", out["inner"]["generation"])

    def test_an_uppercase_scheme_origin_is_still_erased(self):
        out = self.canon.body({"selfLink": "HTTP://127.0.0.1:51423/storage/v1/b/bk"})
        self.assertEqual("<ORIGIN:1>/storage/v1/b/bk", out["selfLink"])
        self.assertNotIn("51423", out["selfLink"])
```

Two ordering hazards this structure resolves, recorded for the reader:

**Field order within a dict.** `_substitute_known_values` can only replace a value that has already been bound, and bind order follows insertion order, so a link appearing *before* its `generation` in the same object would not substitute. Fix by walking each dict in two passes — first bind every directly-replaceable field, then walk the remainder — so the result is independent of field order. Do not fix this by reordering the test.

**Link fields are composite, not opaque.** A `LINK`-kind field must not be bound wholesale, or the embedded generation disappears into a single placeholder and the test above cannot pass. But it must not be passed through untouched either: the emulator binds an ephemeral port (`emulator.py` uses `free_port()`), so `http://127.0.0.1:<random>/…` would differ on every run and the goldens would never match twice — the conformance job would be permanently red.

Canonicalize a link **structurally**: bind only the volatile origin, keep the path and query, and let substitution handle values embedded in them.

```python
_ORIGIN = re.compile(r"^[a-z][a-z0-9+.-]*://[^/]+")


    def _canonical_link(self, text):
        """Erase a URL's volatile origin, keep its meaningful remainder.

        The origin carries the ephemeral port and nothing behavioral. The
        path and query carry the emulator's URL scheme and the generation
        the link points at, both of which must stay visible to a diff.
        """
        match = _ORIGIN.match(text)
        if match is None:
            return self._substitute_known_values(text)
        origin = self._symbols.bind("ORIGIN", match.group(0))
        return origin + self._substitute_known_values(text[match.end() :])
```

so that

```
"mediaLink": "http://127.0.0.1:51423/download/storage/v1/b/bk/o/o?generation=10&alt=media"
```

canonicalizes to

```
"mediaLink": "<ORIGIN:1>/download/storage/v1/b/bk/o/o?generation=<GEN:1>&alt=media"
```

Add a test pinning that the port is erased, alongside the existing link test:

```python
    def test_link_origin_is_erased_but_path_is_kept(self):
        # The emulator binds an ephemeral port, so the origin must not reach
        # the golden. The path and query must, or a change to the emulator's
        # URL scheme would be invisible to the diff.
        out = self.canon.body(
            {
                "generation": "10",
                "mediaLink": "http://127.0.0.1:51423/download/storage/v1/b/bk/o/o?generation=10&alt=media",
            }
        )
        self.assertEqual(
            "<ORIGIN:1>/download/storage/v1/b/bk/o/o?generation=<GEN:1>&alt=media",
            out["mediaLink"],
        )
        self.assertNotIn("51423", out["mediaLink"])

    def test_same_origin_across_links_reuses_its_placeholder(self):
        out = self.canon.body(
            {
                "selfLink": "http://127.0.0.1:51423/storage/v1/b/bk/o/o",
                "mediaLink": "http://127.0.0.1:51423/download/storage/v1/b/bk/o/o",
            }
        )
        self.assertTrue(out["selfLink"].startswith("<ORIGIN:1>"))
        self.assertTrue(out["mediaLink"].startswith("<ORIGIN:1>"))
```

- [ ] **Step 5: Commit**

```bash
nix develop --command bash -c 'isort --quiet tests/conformance/ && black --quiet tests/conformance/ && git diff --exit-code'
git add tests/conformance/
git commit -m "test: canonicalize non-deterministic fields for conformance diffs"
```

---

### Task 4: Recorder and emulator lifecycle

**Files:**
- Create: `tests/conformance/recorder.py`
- Create: `tests/conformance/emulator.py`
- Create: `tests/conformance/test_recorder.py`

**Interfaces:**
- Consumes: `Canonicalizer` from Task 3.
- Produces:
  - `Recorder(name)` with `record_http(label, response)`, `record_grpc(label, message)`, `record_error(label, exception)`, `finish() -> dict`.
  - `Emulator(rest_port, grpc_port)` as a context manager yielding an object with `.rest_url` and `.grpc_target`.
  - Tasks 5, 6, and 7 consume both.

- [ ] **Step 1: Write the failing test**

Create `tests/conformance/test_recorder.py`:

```python
#!/usr/bin/env python3
#
# Copyright 2026 Google LLC
#
# ... (full Apache 2.0 header) ...

"""Unit tests for the conformance recorder."""

import unittest

import requests

from tests.conformance.recorder import Recorder


class FakeResponse:
    def __init__(self, status_code, headers, content):
        self.status_code = status_code
        self.headers = headers
        self.content = content

    def json(self):
        import json

        return json.loads(self.content)


class TestRecorder(unittest.TestCase):
    def test_records_status_headers_and_json_body(self):
        rec = Recorder("demo")
        rec.record_http(
            "create-bucket",
            FakeResponse(200, {"Content-Type": "application/json"}, b'{"generation":"7"}'),
        )
        out = rec.finish()
        entry = out["interactions"][0]
        self.assertEqual("create-bucket", entry["label"])
        self.assertEqual(200, entry["status"])
        self.assertEqual({"generation": "<GEN:1>"}, entry["body"])

    def test_records_binary_body_as_digest_and_length(self):
        # Media payloads must be compared without committing megabytes of
        # bytes to git, but a digest still catches any corruption.
        rec = Recorder("demo")
        rec.record_http(
            "download", FakeResponse(200, {"Content-Type": "application/octet-stream"}, b"abc")
        )
        entry = rec.finish()["interactions"][0]
        self.assertEqual(3, entry["body"]["length"])
        self.assertEqual(
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            entry["body"]["sha256"],
        )

    def test_labels_must_be_unique(self):
        rec = Recorder("demo")
        rec.record_http("x", FakeResponse(200, {}, b"{}"))
        with self.assertRaises(AssertionError):
            rec.record_http("x", FakeResponse(200, {}, b"{}"))

    def test_records_stream_boundaries_and_digest(self):
        rec = Recorder("demo")
        rec.record_stream("read", [b"ab", b"cde", b"f"])
        entry = rec.finish()["interactions"][0]
        self.assertEqual([0, 2, 5], entry["offsets"])
        self.assertEqual(6, entry["length"])

    def test_finish_runs_canonicalizer_invariants(self):
        rec = Recorder("demo")
        rec.record_http("a", FakeResponse(200, {}, b'{"generation":"9"}'))
        rec.record_http("b", FakeResponse(200, {}, b'{"generation":"8"}'))
        with self.assertRaises(AssertionError):
            rec.finish()

    def test_transport_errors_normalize_to_one_token(self):
        # A broken stream surfaces as ReadTimeout on macOS and ConnectionError
        # on Linux for the same injected fault. Recording the subclass would
        # make goldens machine-specific and break the CI conformance job, so
        # every transport-level requests failure records identically.
        rec = Recorder("demo")
        rec.record_error("reset", requests.exceptions.ConnectionError("reset"))
        rec.record_error("slow", requests.exceptions.ReadTimeout("timed out"))
        entries = rec.finish()["interactions"]
        self.assertEqual("<TRANSPORT_ERROR>", entries[0]["type"])
        self.assertEqual("<TRANSPORT_ERROR>", entries[1]["type"])

    def test_non_transport_errors_keep_their_type(self):
        # gRPC status codes are chosen by the emulator, so they are
        # deterministic and must stay visible to the diff.
        rec = Recorder("demo")
        rec.record_error("boom", ValueError("nope"))
        self.assertEqual("ValueError", rec.finish()["interactions"][0]["type"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `nix develop --command bash -c 'pytest tests/conformance/test_recorder.py -v'`

Expected: FAIL — `ModuleNotFoundError: No module named 'tests.conformance.recorder'`.

- [ ] **Step 3: Write the recorder**

Create `tests/conformance/recorder.py`:

```python
#!/usr/bin/env python3
#
# Copyright 2026 Google LLC
#
# ... (full Apache 2.0 header) ...

"""Append-only record of canonicalized API interactions."""

import hashlib
import json

import requests
from google.protobuf import json_format

from tests.conformance.canonicalize import Canonicalizer

_JSON_CONTENT_TYPES = ("application/json", "text/json")


class Recorder:
    """Accumulates canonicalized interactions for one trace.

    Bodies that are JSON are recorded structurally so that a diff points at a
    field. Bodies that are not JSON are recorded as a length and a SHA-256, so
    that media payloads are verified byte-exactly without committing them to
    git.
    """

    def __init__(self, name):
        self.name = name
        self._canon = Canonicalizer()
        self._interactions = []
        self._labels = set()

    def record_http(self, label, response):
        self._claim(label)
        content_type = ""
        for key, value in response.headers.items():
            if key.lower() == "content-type":
                content_type = value
                break
        self._interactions.append(
            {
                "label": label,
                "kind": "http",
                "status": response.status_code,
                "headers": self._canon.headers(response.headers),
                "body": self._body(response.content, content_type),
            }
        )

    def record_grpc(self, label, message):
        self._claim(label)
        as_dict = json_format.MessageToDict(
            message,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        )
        self._interactions.append(
            {
                "label": label,
                "kind": "grpc",
                "type": message.DESCRIPTOR.full_name,
                "body": self._canon.body(as_dict),
            }
        )

    def record_stream(self, label, chunks):
        """Record a streamed payload as boundaries plus a digest.

        Chunk boundaries are externally visible to a client, and the Media
        seam is exactly what could change them, so they belong in the golden
        alongside the bytes.
        """
        self._claim(label)
        offsets, offset = [], 0
        digest = hashlib.sha256()
        for chunk in chunks:
            offsets.append(offset)
            offset += len(chunk)
            digest.update(chunk)
        self._interactions.append(
            {
                "label": label,
                "kind": "stream",
                "offsets": offsets,
                "length": offset,
                "sha256": digest.hexdigest(),
            }
        )

    def record_error(self, label, exception):
        """Record a failure as data, so error taxonomies are diffed too.

        Transport-level `requests` failures collapse to a single token. Which
        urllib3 subclass surfaces for a broken stream is a property of the
        client OS and socket timing, not of the emulator: macOS reports
        `ReadTimeout` where Linux reports `ConnectionError` for the same
        injected fault. Recording the subclass would make the goldens valid
        only on the machine that captured them, so the CI conformance job
        would fail on a clean checkout. The interaction's label still says
        which instruction was injected, so "this fault broke the transfer"
        stays distinguishable from "this fault returned an HTTP status".
        gRPC errors are not normalized -- their status codes are chosen by
        the emulator and are therefore deterministic.
        """
        self._claim(label)
        if isinstance(exception, requests.exceptions.RequestException):
            kind_name = "<TRANSPORT_ERROR>"
        else:
            kind_name = type(exception).__name__
        entry = {"label": label, "kind": "error", "type": kind_name}
        code = getattr(exception, "code", None)
        if callable(code):
            entry["grpc_code"] = code().name
        details = getattr(exception, "details", None)
        if callable(details):
            entry["has_details"] = bool(details())
        self._interactions.append(entry)

    def finish(self):
        self._canon.assert_invariants()
        return {"name": self.name, "interactions": self._interactions}

    def _claim(self, label):
        assert label not in self._labels, "duplicate interaction label %r" % (label,)
        self._labels.add(label)

    def _body(self, content, content_type):
        if not content:
            return None
        if any(content_type.startswith(ct) for ct in _JSON_CONTENT_TYPES):
            return self._canon.body(json.loads(content))
        return {
            "length": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
```

Note on the keyword name, corrected after this plan's first revision asserted the opposite: the pinned `protobuf==5.29.3` accepts **`always_print_fields_with_no_presence`**, not `including_default_value_fields`. The older name was removed rather than deprecated, so passing it raises `TypeError` on *every* call — meaning `record_grpc` fails unconditionally rather than degrading. An earlier revision of this plan stated the wrong keyword as fact and shipped no test for `record_grpc`, so the breakage was invisible until a unit test was added; that is why every interface method the plan lists needs its own test, even the ones that look like thin wrappers. If a future protobuf renames it again, update the harness in lockstep with the pin and regenerate the goldens, recording an allow-list entry with the protobuf version as justification.

- [ ] **Step 4: Run the recorder tests to verify they pass**

Run: `nix develop --command bash -c 'pytest tests/conformance/test_recorder.py -v'`

Expected: 4 passed.

- [ ] **Step 5: Write the emulator launcher**

Create `tests/conformance/emulator.py`:

```python
#!/usr/bin/env python3
#
# Copyright 2026 Google LLC
#
# ... (full Apache 2.0 header) ...

"""Start and stop a testbench subprocess for black-box tracing."""

import contextlib
import os
import socket
import subprocess
import sys
import time

import requests

_STARTUP_TIMEOUT_SECONDS = 30


def free_port():
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Emulator:
    """A testbench subprocess reachable over REST and gRPC.

    The emulator runs as a subprocess rather than in-process so that the
    trace is genuinely black-box: nothing the harness does can perturb the
    server's state except through its API.
    """

    def __init__(self, rest_port=None, grpc_port=None):
        self.rest_port = rest_port or free_port()
        self.grpc_port = grpc_port or free_port()
        self._process = None

    @property
    def rest_url(self):
        return "http://127.0.0.1:%d" % self.rest_port

    @property
    def grpc_target(self):
        return "127.0.0.1:%d" % self.grpc_port

    def __enter__(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.getcwd()
        # Werkzeug's reloader forks a child, which would leave an orphan when
        # we terminate the parent. Setting WERKZEUG_RUN_MAIN makes run_simple
        # serve directly in this process instead.
        env["WERKZEUG_RUN_MAIN"] = "true"
        # Keep the well-known auto-created bucket out of traces.
        env.pop("GOOGLE_CLOUD_CPP_STORAGE_TEST_BUCKET_NAME", None)
        self._process = subprocess.Popen(
            [sys.executable, "-m", "testbench", "--port", str(self.rest_port)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self._await_rest()
        self._start_grpc()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=10)
        return False

    def logs(self):
        if self._process is None or self._process.stdout is None:
            return ""
        return self._process.stdout.read().decode("utf-8", "replace")

    def _await_rest(self):
        deadline = time.time() + _STARTUP_TIMEOUT_SECONDS
        while time.time() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("emulator exited early:\n%s" % self.logs())
            try:
                response = requests.get(self.rest_url + "/", timeout=1)
                if response.status_code == 200:
                    return
            except requests.exceptions.RequestException:
                time.sleep(0.1)
        raise RuntimeError("emulator did not become ready within %ds" % _STARTUP_TIMEOUT_SECONDS)

    def _start_grpc(self):
        # The gRPC server must be started inside the serving process so that
        # it shares one Database; see the comment at rest_server.start_grpc.
        response = requests.get(
            self.rest_url + "/start_grpc", params={"port": self.grpc_port}, timeout=10
        )
        response.raise_for_status()
        reported = int(response.text)
        assert reported == self.grpc_port, "gRPC started on %d, wanted %d" % (
            reported,
            self.grpc_port,
        )
```

- [ ] **Step 6: Verify the launcher works end to end**

This is the step that validates the `WERKZEUG_RUN_MAIN` assumption. Run:

```bash
nix develop --command python3 - <<'PY'
from tests.conformance.emulator import Emulator
import requests, grpc
from google.storage.v2 import storage_pb2, storage_pb2_grpc

with Emulator() as emu:
    r = requests.post(emu.rest_url + "/storage/v1/b",
                      params={"project": "test-project"},
                      json={"name": "probe-bucket"})
    print("REST create bucket:", r.status_code)
    assert r.status_code == 200, r.text
    stub = storage_pb2_grpc.StorageStub(grpc.insecure_channel(emu.grpc_target))
    got = stub.GetBucket(storage_pb2.GetBucketRequest(name="projects/_/buckets/probe-bucket"))
    print("gRPC GetBucket:", got.name)
    assert got.name == "projects/_/buckets/probe-bucket"
print("OK")
PY
```

Expected: `REST create bucket: 200`, a gRPC bucket name, then `OK`, and the process exits without hanging.

If it hangs on exit, the reloader assumption is wrong; switch `__enter__` to launch `gunicorn` on POSIX via `testbench_run.py` and mark the harness `skipif(os.name == "nt")`, noting the deviation in the commit message.

- [ ] **Step 7: Commit**

```bash
nix develop --command bash -c 'isort --quiet tests/conformance/ && black --quiet tests/conformance/ && git diff --exit-code'
git add tests/conformance/
git commit -m "test: add conformance recorder and emulator subprocess harness"
```

---

### Task 5: The traces

Three trace modules, each a plain function that drives the emulator and returns a finished record. They contain domain knowledge and no infrastructure.

**Files:**
- Create: `tests/conformance/trace_rest.py`
- Create: `tests/conformance/trace_grpc.py`
- Create: `tests/conformance/trace_faults.py`

**Interfaces:**
- Consumes: `Emulator`, `Recorder`.
- Produces: `TRACES`, an ordered mapping of trace name to `callable(emulator) -> dict`. Task 6 consumes it. Each trace module exposes `run(emulator) -> dict`.

Coverage required by the spec: bucket CRUD and IAM; object insert via simple, multipart, resumable, and XML upload; `WriteObject` and `BidiWriteObject`; ranged and full reads over REST, `ReadObject`, and `BidiReadObject`; decompressive transcoding; compose; multi-call rewrite with continuation tokens; move; generations and versioning; soft-delete and restore; folders; ACLs; CSEK; precondition failures; and every fault-injection instruction documented in `README.md`.

- [ ] **Step 1: Write the REST trace**

Create `tests/conformance/trace_rest.py`. Full structure, with the operation list complete:

```python
#!/usr/bin/env python3
#
# Copyright 2026 Google LLC
#
# ... (full Apache 2.0 header) ...

"""JSON and XML API conformance trace.

Every interaction is recorded under a stable label. Labels are the diff's
vocabulary, so they must describe the operation rather than its ordinal:
`get-object-after-patch`, not `step-14`.
"""

import gzip
import json

import requests

from tests.conformance.recorder import Recorder

BUCKET = "conformance-bucket"
VERSIONED = "conformance-versioned"
PAYLOAD = b"The quick brown fox jumps over the lazy dog"


def run(emulator):
    rec = Recorder("rest")
    base = emulator.rest_url
    session = requests.Session()

    def api(label, method, path, **kwargs):
        response = session.request(method, base + path, timeout=30, **kwargs)
        rec.record_http(label, response)
        return response

    # --- buckets -----------------------------------------------------------
    api("create-bucket", "POST", "/storage/v1/b",
        params={"project": "test-project"}, json={"name": BUCKET})
    api("create-bucket-duplicate", "POST", "/storage/v1/b",
        params={"project": "test-project"}, json={"name": BUCKET})
    api("get-bucket", "GET", "/storage/v1/b/" + BUCKET)
    api("list-buckets", "GET", "/storage/v1/b", params={"project": "test-project"})
    api("patch-bucket-labels", "PATCH", "/storage/v1/b/" + BUCKET,
        json={"labels": {"env": "conformance"}})
    api("get-bucket-iam", "GET", "/storage/v1/b/%s/iam" % BUCKET)
    api("test-bucket-iam-permissions", "GET",
        "/storage/v1/b/%s/iam/testPermissions" % BUCKET,
        params={"permissions": "storage.objects.get"})
    api("create-versioned-bucket", "POST", "/storage/v1/b",
        params={"project": "test-project"},
        json={"name": VERSIONED, "versioning": {"enabled": True}})

    # --- simple, multipart and XML uploads ---------------------------------
    api("upload-simple", "POST", "/upload/storage/v1/b/%s/o" % BUCKET,
        params={"uploadType": "media", "name": "simple.txt"},
        data=PAYLOAD, headers={"Content-Type": "text/plain"})
    api("upload-multipart", "POST", "/upload/storage/v1/b/%s/o" % BUCKET,
        params={"uploadType": "multipart"},
        files={
            "metadata": (None, json.dumps({"name": "multipart.txt"}), "application/json"),
            "media": ("multipart.txt", PAYLOAD, "text/plain"),
        })
    api("upload-xml", "PUT", "/%s/xml.txt" % BUCKET,
        data=PAYLOAD, headers={"Content-Type": "text/plain"})

    # --- resumable upload --------------------------------------------------
    start = api("start-resumable", "POST", "/upload/storage/v1/b/%s/o" % BUCKET,
                params={"uploadType": "resumable", "name": "resumable.txt"},
                json={"name": "resumable.txt"})
    location = start.headers["Location"]
    upload_path = location[len(base):] if location.startswith(base) else location
    api("resumable-chunk-1", "PUT", upload_path,
        data=PAYLOAD[:20],
        headers={"Content-Range": "bytes 0-19/%d" % len(PAYLOAD)})
    api("resumable-query-status", "PUT", upload_path,
        headers={"Content-Range": "bytes */%d" % len(PAYLOAD)})
    api("resumable-chunk-2", "PUT", upload_path,
        data=PAYLOAD[20:],
        headers={"Content-Range": "bytes 20-%d/%d" % (len(PAYLOAD) - 1, len(PAYLOAD))})

    # --- reads -------------------------------------------------------------
    api("get-object-metadata", "GET", "/storage/v1/b/%s/o/simple.txt" % BUCKET)
    api("download-full", "GET", "/storage/v1/b/%s/o/simple.txt" % BUCKET,
        params={"alt": "media"})
    api("download-range-middle", "GET", "/storage/v1/b/%s/o/simple.txt" % BUCKET,
        params={"alt": "media"}, headers={"Range": "bytes=10-19"})
    api("download-range-open-ended", "GET", "/storage/v1/b/%s/o/simple.txt" % BUCKET,
        params={"alt": "media"}, headers={"Range": "bytes=10-"})
    api("download-range-suffix", "GET", "/storage/v1/b/%s/o/simple.txt" % BUCKET,
        params={"alt": "media"}, headers={"Range": "bytes=-10"})
    api("download-range-unsatisfiable", "GET",
        "/storage/v1/b/%s/o/simple.txt" % BUCKET,
        params={"alt": "media"}, headers={"Range": "bytes=9999-10000"})
    api("download-xml", "GET", "/%s/xml.txt" % BUCKET)
    api("list-objects", "GET", "/storage/v1/b/%s/o" % BUCKET)
    api("list-objects-with-delimiter", "GET", "/storage/v1/b/%s/o" % BUCKET,
        params={"delimiter": "/"})

    # --- decompressive transcoding ----------------------------------------
    api("upload-gzipped", "POST", "/upload/storage/v1/b/%s/o" % BUCKET,
        params={"uploadType": "media", "name": "gz.txt", "contentEncoding": "gzip"},
        data=gzip.compress(PAYLOAD), headers={"Content-Type": "text/plain"})
    api("download-transcoded", "GET", "/storage/v1/b/%s/o/gz.txt" % BUCKET,
        params={"alt": "media"}, headers={"Accept-Encoding": "identity"})
    api("download-not-transcoded", "GET", "/storage/v1/b/%s/o/gz.txt" % BUCKET,
        params={"alt": "media"}, headers={"Accept-Encoding": "gzip"})

    # --- metadata mutation, ACLs, preconditions ---------------------------
    api("patch-object", "PATCH", "/storage/v1/b/%s/o/simple.txt" % BUCKET,
        json={"metadata": {"colour": "blue"}})
    api("update-object", "PUT", "/storage/v1/b/%s/o/simple.txt" % BUCKET,
        json={"contentType": "text/plain", "metadata": {"colour": "green"}})
    api("list-object-acl", "GET", "/storage/v1/b/%s/o/simple.txt/acl" % BUCKET)
    api("insert-object-acl", "POST", "/storage/v1/b/%s/o/simple.txt/acl" % BUCKET,
        json={"entity": "allUsers", "role": "READER"})
    api("precondition-generation-mismatch", "GET",
        "/storage/v1/b/%s/o/simple.txt" % BUCKET,
        params={"ifGenerationMatch": "1"})
    api("get-missing-object", "GET", "/storage/v1/b/%s/o/absent.txt" % BUCKET)
    api("get-missing-bucket", "GET", "/storage/v1/b/absent-bucket")

    # --- CSEK --------------------------------------------------------------
    # Fixed key material so the trace is deterministic. Not a secret.
    # Corrected after review. The values this plan originally carried were
    # malformed in two ways: the key was base64-OF-base64 (it decoded to the
    # 44-byte ASCII string "iXk9eXVbDwHUx2Dg6J5bT8A7OyUzpnuGqZOBTUoKCgM=",
    # not to a 32-byte AES-256 key), and the "sha256" decoded to the ASCII hex
    # text "774059E51423C444E38A0D6AD9AC4310957B6A68" rather than to a digest.
    # The emulator therefore rejected the upload with
    # customerEncryptionKeySha256IsInvalid, and the two download steps
    # returned 404 because the object had never been created -- three stable,
    # meaningless recordings that read as CSEK coverage. The pair below is
    # verified: the key decodes to exactly 32 bytes and the sha is the base64
    # SHA-256 of those bytes.
    key_b64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
    sha_b64 = "Yw3NKWbEM2aRElRIu7JbT/QSpJxzLbLIq8G4WBvXEN0="
    csek = {
        "x-goog-encryption-algorithm": "AES256",
        "x-goog-encryption-key": key_b64,
        "x-goog-encryption-key-sha256": sha_b64,
    }
    api("upload-csek", "POST", "/upload/storage/v1/b/%s/o" % BUCKET,
        params={"uploadType": "media", "name": "csek.txt"},
        data=PAYLOAD, headers=dict(csek, **{"Content-Type": "text/plain"}))
    api("download-csek", "GET", "/storage/v1/b/%s/o/csek.txt" % BUCKET,
        params={"alt": "media"}, headers=csek)
    api("download-csek-without-key", "GET",
        "/storage/v1/b/%s/o/csek.txt" % BUCKET, params={"alt": "media"})

    # --- compose, rewrite, move -------------------------------------------
    api("compose", "POST", "/storage/v1/b/%s/o/composed.txt/compose" % BUCKET,
        json={"sourceObjects": [{"name": "simple.txt"}, {"name": "multipart.txt"}],
              "destination": {"contentType": "text/plain"}})
    api("rewrite-single-call", "POST",
        "/storage/v1/b/%s/o/simple.txt/rewriteTo/b/%s/o/rewritten.txt" % (BUCKET, BUCKET))
    api("move-object", "POST",
        "/storage/v1/b/%s/o/rewritten.txt/moveTo/o/moved.txt" % BUCKET)

    # --- versioning, delete, soft delete ----------------------------------
    api("versioned-upload-v1", "POST", "/upload/storage/v1/b/%s/o" % VERSIONED,
        params={"uploadType": "media", "name": "v.txt"}, data=b"v1",
        headers={"Content-Type": "text/plain"})
    api("versioned-upload-v2", "POST", "/upload/storage/v1/b/%s/o" % VERSIONED,
        params={"uploadType": "media", "name": "v.txt"}, data=b"v2",
        headers={"Content-Type": "text/plain"})
    api("versioned-list-all", "GET", "/storage/v1/b/%s/o" % VERSIONED,
        params={"versions": "true"})
    api("delete-object", "DELETE", "/storage/v1/b/%s/o/moved.txt" % BUCKET)
    api("delete-missing-object", "DELETE", "/storage/v1/b/%s/o/moved.txt" % BUCKET)
    api("delete-non-empty-bucket", "DELETE", "/storage/v1/b/" + BUCKET)

    return rec.finish()
```

- [ ] **Step 2: Run the REST trace and inspect the output by eye**

Run:
```bash
nix develop --command python3 - <<'PY'
import json
from tests.conformance.emulator import Emulator
from tests.conformance import trace_rest
with Emulator() as emu:
    out = trace_rest.run(emu)
print(len(out["interactions"]), "interactions")
print(json.dumps(out["interactions"][:3], indent=2))
PY
```

Expected: a count matching the number of `api(...)` calls, and canonicalized bodies containing `<GEN:n>` and `<TIME:n>` placeholders rather than raw values.

Read the output. Any raw timestamp or 13-digit generation still present means a field name is missing from `NONDETERMINISTIC_FIELDS` in `canonicalize.py`; add it and re-run. Committing a golden that still contains a real timestamp would make the diff fail on every subsequent run, so do not skip this inspection.

- [ ] **Step 3: Write the gRPC trace**

Create `tests/conformance/trace_grpc.py`. Folders and `GetStorageLayout` live on the *control* stub (`StorageControlServicer`, `testbench/grpc_server.py:1202-1256`), not the storage stub.

```python
#!/usr/bin/env python3
#
# Copyright 2026 Google LLC
#
# ... (licence header) ...

"""gRPC v2 conformance trace.

Field spellings here were read off the emulator's own handlers in
testbench/grpc_server.py, which is the authority on what it accepts. Step 5
runs the trace; a field name error surfaces there as a ValueError, not as a
silent gap in coverage.
"""

import grpc

from google.storage.control.v2 import storage_control_pb2, storage_control_pb2_grpc
from google.storage.v2 import storage_pb2, storage_pb2_grpc
from tests.conformance.recorder import Recorder

BUCKET = "grpc-bucket"
SOFT_DELETE = "grpc-soft-delete"
PROJECT = "projects/test-project"
PAYLOAD = b"The quick brown fox jumps over the lazy dog"


def _bucket_path(name):
    return "projects/_/buckets/%s" % name


def run(emulator):
    rec = Recorder("grpc")
    channel = grpc.insecure_channel(emulator.grpc_target)
    storage = storage_pb2_grpc.StorageStub(channel)
    control = storage_control_pb2_grpc.StorageControlStub(channel)

    def call(label, method, request):
        try:
            rec.record_grpc(label, method(request))
        except grpc.RpcError as error:
            rec.record_error(label, error)

    # --- buckets -----------------------------------------------------------
    call("create-bucket", storage.CreateBucket,
         storage_pb2.CreateBucketRequest(
             parent=PROJECT, bucket_id=BUCKET,
             bucket=storage_pb2.Bucket(project=PROJECT)))
    call("create-bucket-duplicate", storage.CreateBucket,
         storage_pb2.CreateBucketRequest(
             parent=PROJECT, bucket_id=BUCKET,
             bucket=storage_pb2.Bucket(project=PROJECT)))
    call("get-bucket", storage.GetBucket,
         storage_pb2.GetBucketRequest(name=_bucket_path(BUCKET)))
    call("get-missing-bucket", storage.GetBucket,
         storage_pb2.GetBucketRequest(name=_bucket_path("absent")))
    call("list-buckets", storage.ListBuckets,
         storage_pb2.ListBucketsRequest(parent=PROJECT))
    call("get-iam-policy", storage.GetIamPolicy,
         __import__("google.iam.v1.iam_policy_pb2", fromlist=["x"]).GetIamPolicyRequest(
             resource=_bucket_path(BUCKET)))

    # --- WriteObject, single message and multi message ---------------------
    def write_spec(name, bucket=BUCKET):
        return storage_pb2.WriteObjectSpec(
            resource=storage_pb2.Object(name=name, bucket=_bucket_path(bucket)))

    call("write-object-single", storage.WriteObject, iter([
        storage_pb2.WriteObjectRequest(
            write_object_spec=write_spec("single.txt"),
            write_offset=0,
            checksummed_data=storage_pb2.ChecksummedData(content=PAYLOAD),
            finish_write=True),
    ]))

    half = len(PAYLOAD) // 2
    call("write-object-multi", storage.WriteObject, iter([
        storage_pb2.WriteObjectRequest(
            write_object_spec=write_spec("multi.txt"),
            write_offset=0,
            checksummed_data=storage_pb2.ChecksummedData(content=PAYLOAD[:half])),
        storage_pb2.WriteObjectRequest(
            write_offset=half,
            checksummed_data=storage_pb2.ChecksummedData(content=PAYLOAD[half:]),
            finish_write=True),
    ]))

    # --- BidiWriteObject, with a flush and a state lookup -----------------
    def bidi_write():
        yield storage_pb2.BidiWriteObjectRequest(
            write_object_spec=write_spec("bidi.txt"),
            write_offset=0,
            checksummed_data=storage_pb2.ChecksummedData(content=PAYLOAD[:half]),
            flush=True, state_lookup=True)
        yield storage_pb2.BidiWriteObjectRequest(
            write_offset=half,
            checksummed_data=storage_pb2.ChecksummedData(content=PAYLOAD[half:]),
            finish_write=True)

    try:
        for index, response in enumerate(storage.BidiWriteObject(bidi_write())):
            rec.record_grpc("bidi-write-response-%d" % index, response)
    except grpc.RpcError as error:
        rec.record_error("bidi-write", error)

    # --- resumable write --------------------------------------------------
    try:
        started = storage.StartResumableWrite(
            storage_pb2.StartResumableWriteRequest(
                write_object_spec=write_spec("resumable.txt")))
        rec.record_grpc("start-resumable-write", started)
        upload_id = started.upload_id
        call("query-write-status-empty", storage.QueryWriteStatus,
             storage_pb2.QueryWriteStatusRequest(upload_id=upload_id))
        call("resume-write", storage.WriteObject, iter([
            storage_pb2.WriteObjectRequest(
                upload_id=upload_id, write_offset=0,
                checksummed_data=storage_pb2.ChecksummedData(content=PAYLOAD),
                finish_write=True),
        ]))
    except grpc.RpcError as error:
        rec.record_error("start-resumable-write", error)

    # --- reads -------------------------------------------------------------
    call("get-object", storage.GetObject,
         storage_pb2.GetObjectRequest(bucket=_bucket_path(BUCKET), object="single.txt"))
    call("get-missing-object", storage.GetObject,
         storage_pb2.GetObjectRequest(bucket=_bucket_path(BUCKET), object="absent.txt"))
    call("get-object-precondition-mismatch", storage.GetObject,
         storage_pb2.GetObjectRequest(
             bucket=_bucket_path(BUCKET), object="single.txt", if_generation_match=1))

    def read(label, **kwargs):
        request = storage_pb2.ReadObjectRequest(
            bucket=_bucket_path(BUCKET), object="single.txt", **kwargs)
        try:
            chunks = [r.checksummed_data.content for r in storage.ReadObject(request)]
            rec.record_stream(label, chunks)
        except grpc.RpcError as error:
            rec.record_error(label, error)

    read("read-object-full")
    read("read-object-ranged", read_offset=10, read_limit=10)
    read("read-object-negative-offset", read_offset=-10)
    read("read-object-offset-past-end", read_offset=9999)

    def bidi_read(label, ranges):
        request = storage_pb2.BidiReadObjectRequest(
            read_object_spec=storage_pb2.BidiReadObjectSpec(
                bucket=_bucket_path(BUCKET), object="single.txt"),
            read_ranges=ranges)
        try:
            chunks = []
            for response in storage.BidiReadObject(iter([request])):
                for data in response.object_data_ranges:
                    chunks.append(data.checksummed_data.content)
            rec.record_stream(label, chunks)
        except grpc.RpcError as error:
            rec.record_error(label, error)

    bidi_read("bidi-read-two-ranges", [
        storage_pb2.ReadRange(read_offset=0, read_length=10, read_id=1),
        storage_pb2.ReadRange(read_offset=20, read_length=10, read_id=2),
    ])

    # --- listing and metadata ---------------------------------------------
    call("list-objects", storage.ListObjects,
         storage_pb2.ListObjectsRequest(parent=_bucket_path(BUCKET)))
    call("list-objects-prefix", storage.ListObjects,
         storage_pb2.ListObjectsRequest(parent=_bucket_path(BUCKET), prefix="multi"))
    call("list-objects-delimiter", storage.ListObjects,
         storage_pb2.ListObjectsRequest(parent=_bucket_path(BUCKET), delimiter="/"))

    # --- compose, rewrite with continuation, move -------------------------
    call("compose-object", storage.ComposeObject,
         storage_pb2.ComposeObjectRequest(
             destination=storage_pb2.Object(
                 name="composed.txt", bucket=_bucket_path(BUCKET)),
             source_objects=[
                 storage_pb2.ComposeObjectRequest.SourceObject(name="single.txt"),
                 storage_pb2.ComposeObjectRequest.SourceObject(name="multi.txt"),
             ]))

    # A small max_bytes_rewritten_per_call forces a continuation token, so the
    # multi-call rewrite path is covered rather than the single-call shortcut.
    token = ""
    for step in range(6):
        request = storage_pb2.RewriteObjectRequest(
            source_bucket=_bucket_path(BUCKET), source_object="single.txt",
            destination_bucket=_bucket_path(BUCKET), destination_name="rewritten.txt",
            max_bytes_rewritten_per_call=16, rewrite_token=token)
        try:
            response = storage.RewriteObject(request)
            rec.record_grpc("rewrite-step-%d" % step, response)
            token = response.rewrite_token
            if not token:
                break
        except grpc.RpcError as error:
            rec.record_error("rewrite-step-%d" % step, error)
            break

    call("move-object", storage.MoveObject,
         storage_pb2.MoveObjectRequest(
             bucket=_bucket_path(BUCKET), source_object="rewritten.txt",
             destination_object="moved.txt"))
    call("delete-object", storage.DeleteObject,
         storage_pb2.DeleteObjectRequest(
             bucket=_bucket_path(BUCKET), object="moved.txt"))
    call("delete-missing-object", storage.DeleteObject,
         storage_pb2.DeleteObjectRequest(
             bucket=_bucket_path(BUCKET), object="moved.txt"))

    # --- soft delete and restore ------------------------------------------
    call("create-soft-delete-bucket", storage.CreateBucket,
         storage_pb2.CreateBucketRequest(
             parent=PROJECT, bucket_id=SOFT_DELETE,
             bucket=storage_pb2.Bucket(
                 project=PROJECT,
                 soft_delete_policy=storage_pb2.Bucket.SoftDeletePolicy())))
    call("soft-delete-write", storage.WriteObject, iter([
        storage_pb2.WriteObjectRequest(
            write_object_spec=write_spec("sd.txt", SOFT_DELETE),
            write_offset=0,
            checksummed_data=storage_pb2.ChecksummedData(content=PAYLOAD),
            finish_write=True),
    ]))
    # The generation to restore is read back from the listing rather than
    # remembered, so this stays valid if generation assignment changes.
    listing = storage.ListObjects(
        storage_pb2.ListObjectsRequest(parent=_bucket_path(SOFT_DELETE)))
    generation = listing.objects[0].generation if listing.objects else 0
    call("soft-delete-object", storage.DeleteObject,
         storage_pb2.DeleteObjectRequest(
             bucket=_bucket_path(SOFT_DELETE), object="sd.txt"))
    call("restore-object", storage.RestoreObject,
         storage_pb2.RestoreObjectRequest(
             bucket=_bucket_path(SOFT_DELETE), object="sd.txt",
             generation=generation))

    # --- folders and layout, on the control stub --------------------------
    call("get-storage-layout", control.GetStorageLayout,
         storage_control_pb2.GetStorageLayoutRequest(
             name="%s/storageLayout" % _bucket_path(BUCKET)))
    call("create-folder", control.CreateFolder,
         storage_control_pb2.CreateFolderRequest(
             parent=_bucket_path(BUCKET), folder_id="folder-a/"))
    call("get-folder", control.GetFolder,
         storage_control_pb2.GetFolderRequest(
             name="%s/folders/folder-a/" % _bucket_path(BUCKET)))
    call("list-folders", control.ListFolders,
         storage_control_pb2.ListFoldersRequest(parent=_bucket_path(BUCKET)))
    call("rename-folder", control.RenameFolder,
         storage_control_pb2.RenameFolderRequest(
             name="%s/folders/folder-a/" % _bucket_path(BUCKET),
             destination_folder_id="folder-b/"))
    call("delete-folder", control.DeleteFolder,
         storage_control_pb2.DeleteFolderRequest(
             name="%s/folders/folder-b/" % _bucket_path(BUCKET)))

    return rec.finish()
```

Replace the `__import__` line with a normal top-level `from google.iam.v1 import iam_policy_pb2` import and use `iam_policy_pb2.GetIamPolicyRequest`; the inline form above is only to keep the import list visible in one place. `isort` will reject the inline form.

- [ ] **Step 4: Write the fault-injection trace**

Create `tests/conformance/trace_faults.py`:

```python
#!/usr/bin/env python3
#
# Copyright 2026 Google LLC
#
# ... (licence header) ...

"""Fault-injection conformance trace.

Covers every instruction documented in README.md. Outcomes include connection
resets and timeouts, which are recorded as errors: a change from "the client
saw a reset" to "the client got data" is exactly the regression this trace
exists to catch.

The emulator has TWO independent injection mechanisms with different
grammars, and an instruction only fires through the one that owns it. An
earlier revision of this plan pushed all twelve instructions through the
Retry Test API, and nine of them recorded either a creation-time 400 or an
unmodified 200 -- stable, meaningless interactions that read as coverage. A
reviewer disproved that by probing a live emulator directly, so the split
below is empirical, not inferred:

1. The `x-goog-emulator-instructions` REQUEST HEADER (README lines 146-199).
   `x-goog-testbench-instructions` is the deprecated spelling of the same
   header; use the current one. Verified to produce real faults this way:
   `return-corrupted-data` (200 with corrupted bytes), `stall-always`
   (ReadTimeout), `stall-at-256KiB` (ConnectionError),
   `return-503-after-256K` and `.../retry-1` (ChunkedEncodingError).
2. The Retry Test API, whose instruction grammar is a separate set of
   anchored regexes in `testbench/common.py:41-55` --
   `return-<code>-after-<N>K`, `stall-for-<T>s-after-<N>K`,
   `redirect-send-token-<lowercase>`, and friends. Note the anchoring: a
   `/retry-1` suffix does not match, which is why those spellings belong to
   the header mechanism. Verified: `stall-for-1s-after-256K` produced a real
   stall through this API.

The `redirect-*` trio is gRPC-only -- README marks it `[HTTP] Unsupported`,
and a probe confirms a clean 200 over HTTP even with the header set. Those
belong in `trace_grpc.py`, driven through gRPC initial metadata, not here.
"""

import requests

from tests.conformance.recorder import Recorder

BUCKET = "faults-bucket"
OBJECT = "faults.bin"
# Larger than the 256 KiB thresholds the byte-offset instructions trigger on.
PAYLOAD = b"0123456789abcdef" * 32 * 1024  # 512 KiB
STALL_TIMEOUT_SECONDS = 2

INSTRUCTIONS = [
    ("return-broken-stream", "storage.objects.get"),
    ("return-corrupted-data", "storage.objects.get"),
    ("stall-always", "storage.objects.get"),
    ("stall-at-256KiB", "storage.objects.get"),
    ("return-503-after-256K", "storage.objects.get"),
    ("return-503-after-256K/retry-1", "storage.objects.get"),
    ("return-503-after-256K/retry-2", "storage.objects.get"),
    ("redirect-send-token-T", "storage.objects.get"),
    ("redirect-send-handle-and-token-T", "storage.objects.get"),
    ("redirect-expect-token-T", "storage.objects.get"),
    ("return-503", "storage.objects.insert"),
    ("return-reset-connection", "storage.objects.get"),
]


def run(emulator):
    rec = Recorder("faults")
    base = emulator.rest_url
    session = requests.Session()

    setup = session.post(base + "/storage/v1/b", params={"project": "test-project"},
                         json={"name": BUCKET}, timeout=30)
    rec.record_http("setup-create-bucket", setup)
    upload = session.post(base + "/upload/storage/v1/b/%s/o" % BUCKET,
                          params={"uploadType": "media", "name": OBJECT},
                          data=PAYLOAD,
                          headers={"Content-Type": "application/octet-stream"},
                          timeout=30)
    rec.record_http("setup-upload", upload)

    for instruction, method in INSTRUCTIONS:
        label = instruction.replace("/", "-")
        created = session.post(
            base + "/retry_test",
            json={"instructions": {method: [instruction]}},
            timeout=30)
        if created.status_code != 200:
            # An unsupported instruction is itself a behavior worth pinning.
            rec.record_http("create-retry-test-%s" % label, created)
            continue
        rec.record_http("create-retry-test-%s" % label, created)
        test_id = created.json()["id"]
        headers = {"x-retry-test-id": test_id}
        try:
            response = session.get(
                base + "/storage/v1/b/%s/o/%s" % (BUCKET, OBJECT),
                params={"alt": "media"}, headers=headers,
                timeout=STALL_TIMEOUT_SECONDS, stream=False)
            rec.record_http("effect-%s" % label, response)
        except requests.exceptions.RequestException as error:
            rec.record_error("effect-%s" % label, error)
        status = session.get(base + "/retry_test/%s" % test_id, timeout=30)
        rec.record_http("retry-test-status-%s" % label, status)
        session.delete(base + "/retry_test/%s" % test_id, timeout=30)

    return rec.finish()
```

Two notes. The short `STALL_TIMEOUT_SECONDS` keeps the stall instructions from adding ten seconds each; the timeout is recorded as an error entry rather than swallowed. `requests.exceptions.RequestException` covers both the timeout and the connection reset that `return-broken-stream` produces, and `Recorder.record_error` collapses every such transport-level failure to the single token `<TRANSPORT_ERROR>` rather than recording its subclass name. The subclass is not a property of the emulator: the same injected fault surfaces as `ReadTimeout` on macOS and `ConnectionError` on Linux, so recording it would make these goldens valid only on the machine that captured them and would fail the ubuntu CI conformance job on a clean checkout. What the emulator controls — *that* the transfer failed rather than returning a status, and under which injected instruction — is preserved by the entry's label. Exception *messages* are never recorded either, because urllib3 embeds socket details that vary between runs.

If `create-retry-test-*` returns 400 for an instruction, the emulator does not support it for that method — `Database.__validate_injected_failure_description` (`testbench/database.py:659`) is the authority. That 400 is recorded and becomes part of the baseline, so removing support later shows up as a diff. Do not delete the entry to make the trace pass.

- [ ] **Step 5: Run all three traces and confirm they complete**

Run:
```bash
nix develop --command python3 - <<'PY'
from tests.conformance.emulator import Emulator
from tests.conformance import trace_rest, trace_grpc, trace_faults
for module in (trace_rest, trace_grpc, trace_faults):
    with Emulator() as emu:
        out = module.run(emu)
    print(out["name"], len(out["interactions"]), "interactions")
PY
```

Expected: three lines, each with a non-zero count. Each trace gets a fresh emulator so traces cannot contaminate one another.

- [ ] **Step 6: Commit**

```bash
nix develop --command bash -c 'isort --quiet tests/conformance/ && black --quiet tests/conformance/ && git diff --exit-code'
git add tests/conformance/
git commit -m "test: add REST, gRPC and fault-injection conformance traces"
```

---

### Task 6: Golden capture, diffing, and CI — the configuration-A baseline

This task produces the artifact that cannot be regenerated later. It must land before any change to `gcs/` or `testbench/`.

**Files:**
- Create: `tests/conformance/harness.py`
- Create: `tests/conformance/golden/rest.json`, `grpc.json`, `faults.json`
- Create: `tests/test_conformance.py`
- Modify: `.github/workflows/build.yaml`
- Modify: `README.md`

**Interfaces:**
- Consumes: the three trace modules, `Emulator`.
- Produces: `python -m tests.conformance.harness [--regenerate] [--trace NAME]`; `pytest tests/test_conformance.py`.

- [ ] **Step 1: Write the harness CLI**

Create `tests/conformance/harness.py`:

```python
#!/usr/bin/env python3
#
# Copyright 2026 Google LLC
#
# ... (full Apache 2.0 header) ...

"""Capture or verify the emulator's external behavior against goldens.

    python -m tests.conformance.harness              # verify
    python -m tests.conformance.harness --regenerate # rewrite goldens

Regenerating is a reviewable act: any resulting change to a golden file must
be justified in the commit message. An unexplained golden diff is a defect,
not a nuisance.

## Allowlist: removed

An earlier revision of this plan specified an `allowlist.json` for
suppressing justified, intended diffs, and mandated it stay empty through
both of this plan's phases. Review found the supplied implementation broken
in three ways at once: an entry keyed by an interaction label (the documented
key) suppressed nothing, because the hunk test searched *changed lines* for
the label and a changed line never contains it; an entry keyed by a field
name suppressed that field across the entire trace, which is precisely the
broad-pattern silencing the design meant to avoid; and even when every hunk
was dropped, the two `---`/`+++` file headers still made the returned diff
truthy, so the build stayed red anyway.

The net effect of adding an entry was therefore to hide the evidence while
keeping the failure — which routes a developer straight to `--regenerate`,
the one action that can make a real regression vanish.

Because nothing in either phase needs it, the mechanism is deleted rather
than repaired: a suppression path with no current user is pure risk surface,
and a future maintainer who fixed only the still-red half would silently
activate trace-wide field suppression, where one word like `etag` or
`generation` could hide a genuine regression. If a first genuinely justified
diff ever arrives, add it then, interaction-scoped, returning an empty diff
when nothing survives, and requiring a non-empty justification per entry.
"""

import argparse
import difflib
import json
import os
import sys

from tests.conformance import trace_faults, trace_grpc, trace_rest
from tests.conformance.emulator import Emulator

TRACES = {
    "rest": trace_rest.run,
    "grpc": trace_grpc.run,
    "faults": trace_faults.run,
}

_HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN_DIR = os.path.join(_HERE, "golden")


def golden_path(name):
    return os.path.join(GOLDEN_DIR, name + ".json")


def serialize(record):
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


def capture(name):
    with Emulator() as emulator:
        return TRACES[name](emulator)


def verify(name):
    """Return a unified diff of golden versus observed, or "" when identical.

    The existence check precedes `capture()` so that a missing golden costs
    nothing: running a full trace only to report "missing golden" wastes an
    emulator launch.
    """
    path = golden_path(name)
    if not os.path.exists(path):
        return "missing golden %s; run with --regenerate" % path
    with open(path, "r", encoding="utf-8") as handle:
        expected = handle.read()
    observed = serialize(capture(name))
    if expected == observed:
        return ""
    return "".join(
        difflib.unified_diff(
            expected.splitlines(True),
            observed.splitlines(True),
            fromfile="golden/%s.json" % name,
            tofile="observed/%s.json" % name,
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regenerate", action="store_true")
    parser.add_argument("--trace", action="append", choices=sorted(TRACES))
    args = parser.parse_args(argv)
    names = args.trace or sorted(TRACES)

    if args.regenerate:
        os.makedirs(GOLDEN_DIR, exist_ok=True)
        for name in names:
            with open(golden_path(name), "w", encoding="utf-8") as handle:
                handle.write(serialize(capture(name)))
            print("wrote", golden_path(name))
        return 0

    failed = False
    for name in names:
        diff = verify(name)
        if diff:
            failed = True
            print("FAIL %s\n%s" % (name, diff))
        else:
            print("OK   %s" % name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Confirm the tree has no production changes yet**

Run: `git status --short gcs/ testbench/`

Expected: **empty output.** If anything appears, stop — the baseline would capture already-modified behavior and the whole verification plan loses its meaning.

- [ ] **Step 4: Capture the goldens**

Run: `nix develop --command bash -c 'python3 -m tests.conformance.harness --regenerate'`

Expected: three `wrote .../golden/<name>.json` lines.

- [ ] **Step 5: Verify the goldens are stable across runs**

Run the verification twice:

```bash
nix develop --command bash -c 'python3 -m tests.conformance.harness && python3 -m tests.conformance.harness'
```

Expected: `OK rest`, `OK grpc`, `OK faults` — twice.

Flakiness here means residual non-determinism, and it must be fixed now rather than tolerated: a harness that fails intermittently will be ignored, and then it protects nothing. Diagnose by reading the printed diff — it names the field. Fixes are either a new entry in `NONDETERMINISTIC_FIELDS`, a new entry in `DROPPED_HEADERS`, or removing an order-dependent assertion from a trace.

- [ ] **Step 6: Write the pytest entry point**

Create `tests/test_conformance.py`:

```python
#!/usr/bin/env python3
#
# Copyright 2026 Google LLC
#
# ... (full Apache 2.0 header) ...

"""Assert the emulator's external behavior matches the committed baseline.

This is the regression gate for the file backend work: the Store and Media
seams are refactors, so every trace must match byte for byte.
"""

import unittest

from tests.conformance import harness


class TestConformance(unittest.TestCase):
    maxDiff = None

    def _check(self, name):
        diff = harness.verify(name)
        self.assertEqual("", diff, "conformance diff for %s:\n%s" % (name, diff))

    def test_rest_trace_matches_golden(self):
        self._check("rest")

    def test_grpc_trace_matches_golden(self):
        self._check("grpc")

    def test_faults_trace_matches_golden(self):
        self._check("faults")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 7: Run the whole suite**

Run: `nix develop --command bash -c 'pytest -q 2>&1 | tail -20'`

Expected: everything passes, including the three new conformance tests, with the same pre-existing count as Task 1 Step 6 plus the new tests.

- [ ] **Step 8: Add a CI job**

Add to `.github/workflows/build.yaml` under `jobs:`, matching the existing indentation and the pinned `actions/checkout` SHA already used in that file:

```yaml
  conformance:
    runs-on: ubuntu-22.04
    name: Conformance baseline
    steps:
      - uses: actions/checkout@692973e3d937129bcbf40652eb9f2f61becf3332 # v4
      - name: Set up Python 3.12
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install Dependencies
        run: |
          python -m pip install --upgrade pip
          pip install pytest requests
          pip install -e .
      - name: Verify external behavior against the committed baseline
        env:
          PYTHONPATH: "."
        run: python -m tests.conformance.harness
```

The job is deliberately separate from `python-tests` so a golden diff is legible in the CI summary rather than buried in a matrix cell, and it runs only on Linux because the harness starts a subprocess server.

- [ ] **Step 9: Document it**

Add to `README.md` in the "Developing for the testbench" section, after "Writing and running tests":

```markdown
### Reproducible development environment

With [Nix](https://nixos.org/download) installed:

```bash
nix develop
```

This provisions `.venv` from `setup.py`'s pinned versions, activates it, sets
`PYTHONPATH`, and adds the Docker client and Compose.

### Conformance harness

`tests/conformance/` records the emulator's external behavior over HTTP and
gRPC and diffs it against committed goldens in
`tests/conformance/golden/`. It is black-box by construction: it must not
import `testbench` or `gcs` internals, so it stays valid across refactors.

```bash
python -m tests.conformance.harness              # verify against goldens
python -m tests.conformance.harness --regenerate # rewrite goldens
```

A golden diff means external behavior changed. If the change is intended,
regenerate and explain it in the commit message. An unexplained diff is a
bug.
```

- [ ] **Step 10: Commit the baseline on its own**

Commit the goldens separately from everything else so the baseline is a single identifiable commit that can be pointed at later.

```bash
nix develop --command bash -c 'isort --quiet tests/ && black --quiet tests/ && git diff --exit-code'
git add tests/conformance/ tests/test_conformance.py .github/workflows/build.yaml README.md
git commit -m "test: capture the configuration-A behavioral baseline

Records the emulator's external behavior over REST, gRPC and fault
injection before any production code is touched, so the Store and Media
seam refactors can be proven behavior-preserving by diff.

Captured with gcs/ and testbench/ unmodified; see the design spec's
verification plan, Mechanism 2."
git rev-parse HEAD  # note this hash: it is the configuration-A commit
```

---

### Task 7: The `Store` seam

**Files:**
- Create: `testbench/store.py`
- Create: `tests/test_store.py`
- Modify: `testbench/database.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `testbench.store.Store` — the protocol, with no-op default methods: `bucket_inserted(bucket)`, `bucket_deleted(bucket_name)`, `object_inserted(bucket_name, blob)`, `object_deleted(bucket_name, object_name, generation)`, `object_updated(bucket_name, blob)`, `object_soft_deleted(bucket_name, blob, hard_delete_time)`, `object_purged(bucket_name, object_name, generation)`, `folder_inserted(folder_name, folder)`, `folder_deleted(folder_name)`, `folder_renamed(src_folder_name, dst_folder_name, folder)`, `cleared()`.

**Three corrections to this protocol, made after review and approved by the human partner. Each was a defect in the protocol as originally specified, not in its wiring.**

*`object_restored` is removed.* `restore_object` calls `insert_object` internally, which already notifies, so the original protocol produced **two** notifications for one logical restore — verified live, with identical bucket, name, and generation — and a `FileStore` would have written the blob twice. A restore genuinely *is* an insert from a persistence standpoint: the same blob reappears at the same generation. One notification, one write. It was also the single notification whose removal left the entire test suite green, so it was both redundant and unverified.

*Soft deletion gets its own two notifications.* `_soft_deleted_objects` is persistent state that is readable (`softDeleted` reads) and restorable, but the original protocol emitted a plain `object_deleted` for a soft delete — byte-identical to a hard delete — and nothing at all for the expiry purge. A `FileStore` that removed its record on `object_deleted`, the only reasonable reading of that name, would have destroyed the restorable copy, breaking `softDeleted` reads and `restore_object` across a restart. Silent data loss. `object_soft_deleted` carries `hard_delete_time` because a persistent store needs it to expire the copy itself; `object_purged` covers `__remove_expired_objects_from_soft_delete`.

*`cleared()` must fire inside `_resources_lock`.* An earlier revision placed it after that lock was released, under `_folders_lock` only. Another thread can then complete an `insert_bucket` in the window between the two, delivering `bucket_inserted` *before* `cleared()` — after which the store has dropped a bucket the database still holds. The locks are `threading.RLock`, so acquiring `_resources_lock` around the notification is safe.

### Known incompleteness, deferred to Plan 3 by decision

`bucket_update` and `bucket_patch` (`testbench/rest_server.py:251-273`) mutate the `Bucket` object returned by `db.get_bucket` **in place** and never call a `Database` mutator, so no bucket metadata change is observable by a `Store` at any point. This cannot be fixed from inside `Database`: it needs the REST handlers routed through a mutator, which changes request-handling control flow and carries real behavioral risk. Doing that inside a task whose defining property is being a provable no-op would forfeit exactly the guarantee this task exists to establish. **Plan 3's first task must route bucket mutations through `Database` and add a `bucket_updated` notification.** Until then the seam is complete for objects and folders and incomplete for bucket metadata; that is a recorded decision, not an oversight.

**Correction, made after the final whole-branch review.** An earlier revision of this section claimed the seam was "complete for objects and folders". That was false, and the bucket deferral above was decided on that false record. `object_updated` was declared on the protocol, mirrored in `RecordingStore`, smoke-tested against `NullStore` — and never fired by `Database`. The stated justification was that `do_update_object` takes an arbitrary `update_fn` so `Database` cannot know whether metadata actually changed, and that firing unconditionally would notify read-only callers. **There are no read-only callers.** All eleven are mutations:

| call site | what it mutates |
|---|---|
| `testbench/rest_server.py:533` | object PUT — metadata via `blob.update` |
| `testbench/rest_server.py:557` | object PATCH — metadata via `blob.patch` |
| `testbench/rest_server.py:867,908,934,950` | object ACL insert/update/patch/delete — ACLs + metageneration |
| `testbench/grpc_server.py:985` | `UpdateObject` — metadata, contexts, metageneration, update_time |
| `gcs/upload.py:330` | calls `insert_object` internally, so this one already notifies |
| `gcs/upload.py:430` | `bump_upload_gen` — takeover handle state |
| `gcs/upload.py:606` | `update_appendable_blob` — **`blob.media`**, size, crc32c |
| `gcs/upload.py:650` | `finalize_blob` — **`blob.media`**, finalize_time, size |

Two of them write object **content**. A `FileStore` built against the uncorrected seam would have persisted an appendable object at creation and then never learned about a single appended byte or its finalization — the same silent-data-loss shape as the soft-delete defect, with the opposite conclusion drawn. The asymmetry also runs the other way from what the justification assumed: a redundant `object_updated` for an unchanged blob costs one idempotent rewrite, while a missing one costs data. `do_update_object` therefore notifies unconditionally on its success path, inside the existing lock, after the mutation.

Relatedly, `insert_test_bucket` (`testbench/database.py:235-237`) sets `metageneration` and `versioning.enabled` *after* calling `insert_bucket`, so a store serializing at notification time persists the pre-mutation values. Those two assignments must move before the `insert_bucket` call.
  - `testbench.store.NullStore` — `Store` with no state.
  - `Database.__init__(..., store=None)` and `Database.init(store=None)`; `Database.store` property.
  - Plan 3's `FileStore` subclasses `Store` and relies on exactly these names.

Design notes constraining the implementation:

- `Store` is a **base class with no-op methods**, not a `typing.Protocol`. `Protocol` runtime behavior differs across the 3.8–3.12 range, and a base class lets `FileStore` override only what it needs.
- Notifications fire **inside** the existing lock, after the in-memory mutation has succeeded, so a store never observes a mutation that was rejected by a precondition, and the index and the store cannot disagree. This matches the spec's "sidecar metadata writes stay inside the lock".
- Notifications pass the `Bucket`/`Object` wrapper objects, not protos, because `FileStore` needs `blob.media` as well as `blob.metadata`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_store.py`:

```python
#!/usr/bin/env python3
#
# Copyright 2026 Google LLC
#
# ... (full Apache 2.0 header) ...

"""Tests for the Database -> Store notification contract.

These tests pin the contract that the file backend depends on. A mutation
that reaches the in-memory index but not the store would produce an emulator
whose disk silently diverges from its behavior, so each mutating path is
asserted explicitly.
"""

import json
import unittest

import gcs
import testbench
from testbench.store import NullStore, Store


class RecordingStore(Store):
    def __init__(self):
        self.calls = []

    def bucket_inserted(self, bucket):
        self.calls.append(("bucket_inserted", bucket.metadata.name))

    def bucket_deleted(self, bucket_name):
        self.calls.append(("bucket_deleted", bucket_name))

    def object_inserted(self, bucket_name, blob):
        self.calls.append(
            ("object_inserted", bucket_name, blob.metadata.name, blob.metadata.generation)
        )

    def object_deleted(self, bucket_name, object_name, generation):
        self.calls.append(("object_deleted", bucket_name, object_name, generation))

    def object_updated(self, bucket_name, blob):
        self.calls.append(("object_updated", bucket_name, blob.metadata.name))

    def cleared(self):
        self.calls.append(("cleared",))


def _make_bucket(name, **fields):
    payload = dict(fields)
    payload["name"] = name
    request = testbench.common.FakeRequest(args={}, data=json.dumps(payload))
    bucket, _ = gcs.bucket.Bucket.init(request, None)
    return bucket


def _make_object(bucket, name, media=b"hello"):
    # `headers` and `environ` are required: FakeRequest is a SimpleNamespace,
    # so absent attributes raise, and Object.init reaches request.headers via
    # extract_instruction() and csek.extract(). This mirrors the construction
    # used in tests/test_object.py.
    request = testbench.common.FakeRequest(args={}, headers={}, environ={})
    blob, _ = gcs.object.Object.init_dict(
        request, {"name": name}, media, bucket.metadata, False
    )
    return blob


class TestStoreContract(unittest.TestCase):
    def setUp(self):
        self.store = RecordingStore()
        self.db = testbench.database.Database.init(store=self.store)

    def test_default_store_is_a_null_store(self):
        self.assertIsInstance(testbench.database.Database.init().store, NullStore)

    def test_null_store_accepts_every_notification(self):
        # NullStore must implement the whole protocol, or a default-configured
        # emulator would crash on any mutation.
        store = NullStore()
        bucket = _make_bucket("b")
        blob = _make_object(bucket, "o")
        store.bucket_inserted(bucket)
        store.object_inserted("projects/_/buckets/b", blob)
        store.object_updated("projects/_/buckets/b", blob)
        store.object_restored("projects/_/buckets/b", blob)
        store.object_deleted("projects/_/buckets/b", "o", 1)
        store.folder_inserted("f", object())
        store.folder_renamed("f", "g", object())
        store.folder_deleted("g")
        store.bucket_deleted("projects/_/buckets/b")
        store.cleared()

    def test_insert_bucket_notifies(self):
        self.db.insert_bucket(_make_bucket("bucket-name"), None)
        self.assertIn(("bucket_inserted", "projects/_/buckets/bucket-name"), self.store.calls)

    def test_duplicate_insert_bucket_does_not_notify(self):
        self.db.insert_bucket(_make_bucket("bucket-name"), None)
        self.store.calls.clear()
        with self.assertRaises(Exception):
            self.db.insert_bucket(_make_bucket("bucket-name"), None)
        self.assertEqual([], self.store.calls)

    def test_delete_bucket_notifies(self):
        self.db.insert_bucket(_make_bucket("bucket-name"), None)
        self.db.delete_bucket("bucket-name", None)
        self.assertIn(("bucket_deleted", "projects/_/buckets/bucket-name"), self.store.calls)

    def test_insert_object_notifies_with_generation(self):
        bucket = _make_bucket("bucket-name")
        self.db.insert_bucket(bucket, None)
        blob = _make_object(bucket, "o.txt")
        self.db.insert_object("bucket-name", blob, None)
        self.assertIn(
            ("object_inserted", "projects/_/buckets/bucket-name", "o.txt",
             blob.metadata.generation),
            self.store.calls,
        )

    def test_failed_precondition_does_not_notify(self):
        bucket = _make_bucket("bucket-name")
        self.db.insert_bucket(bucket, None)
        self.store.calls.clear()
        never = lambda current, live_generation, context: False
        self.db.insert_object(
            "bucket-name", _make_object(bucket, "o.txt"), None, preconditions=[never]
        )
        self.assertEqual([], self.store.calls)

    def test_delete_object_notifies(self):
        bucket = _make_bucket("bucket-name")
        self.db.insert_bucket(bucket, None)
        blob = _make_object(bucket, "o.txt")
        self.db.insert_object("bucket-name", blob, None)
        self.store.calls.clear()
        self.db.delete_object("bucket-name", "o.txt", None)
        self.assertIn(
            ("object_deleted", "projects/_/buckets/bucket-name", "o.txt",
             blob.metadata.generation),
            self.store.calls,
        )

    def test_clear_notifies(self):
        self.db.clear()
        self.assertIn(("cleared",), self.store.calls)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `nix develop --command bash -c 'pytest tests/test_store.py -v'`

Expected: FAIL — `ModuleNotFoundError: No module named 'testbench.store'`.

- [ ] **Step 3: Write the store module**

Create `testbench/store.py`:

```python
# Copyright 2026 Google LLC
#
# ... (full Apache 2.0 header) ...

"""Persistence seam for the testbench database.

`Database` holds the authoritative in-memory index. A `Store` is notified
after each successful mutation so that an implementation can mirror the index
somewhere durable. `NullStore` is the default and does nothing, which makes
the seam free for every existing caller.

Notifications fire while the database still holds its lock, and only after
the in-memory mutation has succeeded, so a store never observes a change that
was rejected by a precondition.

Implemented as a base class with no-op methods rather than a
`typing.Protocol`: the supported Python range is 3.8 to 3.12, where
`Protocol` runtime semantics differ, and subclasses should be free to
override only the notifications they care about.
"""


class Store:
    """No-op base class defining the persistence notifications."""

    def bucket_inserted(self, bucket):
        """A new bucket was added. `bucket` is a `gcs.bucket.Bucket`."""

    def bucket_deleted(self, bucket_name):
        """A bucket was removed. `bucket_name` is the proto-form name."""

    def object_inserted(self, bucket_name, blob):
        """A new object generation became live. `blob` is a `gcs.object.Object`."""

    def object_deleted(self, bucket_name, object_name, generation):
        """A specific object generation was removed."""

    def object_updated(self, bucket_name, blob):
        """An existing object's metadata changed."""

    def object_restored(self, bucket_name, blob):
        """A soft-deleted object was restored as a new generation."""

    def folder_inserted(self, folder_name, folder):
        """A managed folder was created."""

    def folder_deleted(self, folder_name):
        """A managed folder was removed."""

    def folder_renamed(self, src_folder_name, dst_folder_name, folder):
        """A managed folder was renamed."""

    def cleared(self):
        """All resources were dropped, as in `Database.clear()`."""


class NullStore(Store):
    """The default store: keeps nothing, so behavior is unchanged."""
```

- [ ] **Step 4: Wire the seam into `Database`**

Modify `testbench/database.py`. Import the new module alongside the existing imports, then make these five edits.

Constructor and factory — add the parameter and default:

```python
    def __init__(
        self,
        buckets,
        objects,
        live_generations,
        uploads,
        rewrites,
        retry_tests,
        supported_methods,
        soft_deleted_objects,
        folders=None,
        store=None,
    ):
        self._store = store if store is not None else testbench.store.NullStore()
        ...  # existing body unchanged

    @classmethod
    def init(cls, store=None):
        return cls({}, {}, {}, {}, {}, {}, [], {}, {}, store=store)

    @property
    def store(self):
        return self._store
```

`clear()` — notify at the end, still inside the last lock block (`testbench/database.py:70-88`):

```python
        with self._folders_lock:
            self._folders = {}
        self._store.cleared()
```

`insert_bucket` (`:107`) — notify after the dictionaries are populated, so a duplicate-name rejection returns before any notification:

```python
            self._soft_deleted_objects[bucket.metadata.name] = {}
            self._store.bucket_inserted(bucket)
```

`delete_bucket` (`:191`) — notify after the four `del` statements:

```python
            del self._soft_deleted_objects[bucket.metadata.name]
            self._store.bucket_deleted(bucket.metadata.name)
```

`insert_object` (`:467`) — notify after the live generation is set, so the precondition `return` at `:483` skips it:

```python
            self.__set_live_generation(bucket_name, object_name, generation, context)
            self._store.object_inserted(self.__bucket_key(bucket_name, context), blob)
```

`delete_object` (`:491`) — notify after the `bucket.pop`:

```python
            bucket.pop("%s#%d" % (blob.metadata.name, blob.metadata.generation), None)
            self._store.object_deleted(
                self.__bucket_key(bucket_name, context),
                blob.metadata.name,
                blob.metadata.generation,
            )
```

`restore_object` (`:541`) — notify at the end of the `if blob is not None:` branch, after `__remove_restored_soft_deleted_object`:

```python
                self._store.object_restored(
                    self.__bucket_key(bucket_name, context), blob
                )
```

Folder operations (`:803`, `:821`, `:842`) — notify at the end of each of `insert_folder`, `delete_folder`, and `rename_folder`, inside `self._folders_lock`, using the names from the Interfaces block.

`do_update_object` (`:519`) is deliberately **not** wired here. It takes an arbitrary `update_fn`, so the database cannot tell whether metadata actually changed, and firing `object_updated` unconditionally would notify on read-only callers. Plan 3 addresses it by having callers that mutate metadata notify explicitly; the test for `object_updated` therefore only covers `NullStore` accepting the call, which is why `test_insert_object_notifies_with_generation` and not an update test carries the contract here. Leave a comment at `do_update_object` recording this.

- [ ] **Step 5: Run the store tests to verify they pass**

Run: `nix develop --command bash -c 'pytest tests/test_store.py -v'`

Expected: 9 passed.

- [ ] **Step 6: Run the existing suite to verify nothing regressed**

Run: `nix develop --command bash -c 'pytest -q 2>&1 | tail -20'`

Expected: the same pass count as Task 6 Step 7, plus the 9 new tests. Any failure here is a real regression in the seam wiring — most likely a notification placed before a precondition check rather than after it.

- [ ] **Step 7: Verify external behavior did not change — the actual gate**

Run: `nix develop --command bash -c 'python3 -m tests.conformance.harness'`

Expected: `OK rest`, `OK grpc`, `OK faults`.

This is the phase-2 gate from the spec: the diff must be **empty**, with no allow-list entries. A non-empty diff means the seam changed observable behavior, which for a `NullStore` default is impossible by design — so treat it as a bug in the wiring and find it before continuing. Do not add an allow-list entry to make this pass.

- [ ] **Step 8: Commit**

```bash
nix develop --command bash -c 'isort --quiet testbench/ tests/ && black --quiet testbench/ tests/ && git diff --exit-code'
git add testbench/store.py testbench/database.py tests/test_store.py
git commit -m "feat: add a Store persistence seam to Database

Notifies a Store after each successful mutation, inside the existing lock,
so an implementation can mirror the in-memory index durably. NullStore is
the default, so behavior is unchanged: the conformance baseline diffs clean."
```

---

## Definition of done

- [ ] `nix develop` provisions a shell where `pytest` and `python -m testbench` work.
- [ ] `tests/test_crc32c_assumptions.py` passes, so incremental checksums are known-viable.
- [ ] `tests/conformance/golden/{rest,grpc,faults}.json` are committed, captured with `gcs/` and `testbench/` unmodified.
- [ ] `python -m tests.conformance.harness` passes twice in a row with no flakiness.
- [ ] `testbench/store.py` exists with `Store` and `NullStore`; `Database` notifies it from every mutating path except the documented `do_update_object` exception.
- [ ] The pre-existing test suite passes with no test file modified.
- [ ] CI has a `conformance` job.
- [ ] `black` and `isort` produce no diff.

## Handoff to Plan 2

Plan 2 implements spec phase 3, the `Media` seam, and its coverage gate. It depends on this plan for the conformance baseline, and its gate is the same: an empty diff.

### What this plan actually delivered

- A `flake.nix` devShell (interpreter, docker client, `skopeo`, `gnumake`) plus a pip venv pinned from `setup.py`, and `make verify-linux` to run the gate in a Linux container.
- A black-box conformance harness in `tests/conformance/` and a committed baseline of **112 interactions** (`rest` 47, `grpc` 48, `faults` 17) across the JSON API, XML API, gRPC v2, and fault injection.
- A `Store` notification seam in `testbench/database.py` with `NullStore` as the default. Eleven notifications, all fired inside the existing lock after the mutation. The seam is a **verified no-op**: the conformance gate is green with the seam in place, on both macOS and Linux.
- The `crc32c` incremental-checksum assumption the design rests on is **verified true** (`crc32c==2.7.1`), so the large-object plan needs no amendment.

### Before Plan 2's first task, two things

**1. Close the framing gap.** `DROPPED_HEADERS` erases both `content-length` and `transfer-encoding`, so response framing is invisible to the gate. Plan 2 refactors exactly that axis — a change switching a download to chunked encoding, or declaring a length inconsistent with the body, would not move a golden. Fixing it after the refactor proves nothing. See the spec's "Known coverage gaps" for the suggested shape.

**2. Re-establish A ≡ B against the final goldens.** A subtlety worth ten minutes: the goldens were regenerated *after* the `Store` seam landed, because the gzip-literal fix legitimately changed a trace. So the committed goldens describe configuration **B** (post-seam), not A. The seam was proven a no-op against the *intermediate* goldens, which is good evidence but not the same claim. To close it, restore `gcs/` and `testbench/` from `e8c8507` (upstream `main`) alongside the current `tests/`, and run the gate. A green result proves the committed baseline describes untouched-upstream behavior — the property every later plan leans on. Record the result in Plan 2's header along with the configuration-A hash.

### Prerequisites Plan 3 will need, best added while writing Plan 2

- **gRPC `UpdateObject` and `UpdateBucket` have no baseline at all**, and Plan 3's first task is routing bucket mutations through `Database` and adding `bucket_updated`. Add those trace interactions *before* touching `Database`.
- **Soft-deleted reads and listings** (`soft_deleted=True`) are uncovered — the exact reads a `FileStore` must keep working across a restart.
- `FileStore` must validate bucket and object names itself; the emulator's dotted-name validation bypass means the spec's old "a validated bucket name is a safe path segment" premise is false, and rule 4 has been corrected accordingly.
- Expect the `Store` protocol to change. `object_updated`'s "may have changed" semantics is the known soft spot.

### Two habits this plan paid for, worth keeping

Both are recorded in the spec, and both were learned by shipping the mistake first:

- **Ask of every recorded value: does this depend on the interpreter, the OS, or a third-party library's internals?** Five separate defects in this plan were that one class, including the only one that reached CI.
- **Mutation-check every guard test.** Six tests in this plan passed while testing nothing, found only by reintroducing the defect and watching the suite stay green.
