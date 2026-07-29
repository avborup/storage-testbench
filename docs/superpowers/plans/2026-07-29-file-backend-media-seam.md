# Media Seam (`Media` + `BytesMedia`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a `Media` abstraction between the emulator and the raw object/upload
bytes — with a `BytesMedia` implementation that is byte-for-byte identical to today — so
that Plan 3's `FileMedia` can later back the same interface with real files.

**Architecture:** `Object.media` and `Upload.media` stop being `bytes` and become a `Media`
object. `BytesMedia` wraps a `bytearray` and behaves enough like `bytes` (`__len__`,
slicing, concatenation, `+=`) that most of the ~71 call sites are unchanged, while the
size-sensitive paths — checksums, range reads, REST download, decompressive transcoding,
compose, and rewrite — are rerouted through explicit streaming methods (`chunks`,
`reader`, incremental `crc32c`/`md5`). This is spec phase 3: a **pure refactor**, proven by
an empty conformance-harness diff plus a coverage gate that pins every media call site.

**Tech Stack:** Python 3.8–3.12, `crc32c==2.7.1` (incremental seed API, verified in Plan 1),
`hashlib` (streaming md5), `coverage==7.x` (already a dev dependency; `.codecov.yml`
present), the Plan 1 conformance harness (`tests/conformance/`).

## Global Constraints

- **Zero new runtime dependencies.** `setup.py` stays as-is; anything new (`coverage`,
  `hypothesis` later) is a dev dependency in `flake.nix` only. The emulator must keep its
  zero-runtime-dependency property.
- **Python floor is 3.8.** Every new file must parse under `ast.parse(feature_version=(3, 8))`.
  CI runs a 3.8–3.12 matrix.
- **The harness measures external behavior only.** Nothing in `tests/conformance/` may
  import `testbench` or `gcs` internals (`emulator.py` excepted). `Media`/`BytesMedia` live
  in `testbench/`, never in `tests/conformance/`.
- **The conformance gate is the safety net; `--regenerate` never turns a red build green.**
  For phase 3 the harness diff must be **empty**, with no allow-list entries. Any diff is a
  bug. `--regenerate` is only for a reviewed, intentional behavior change — which this phase
  has none of.
- **Mutation-check every guard test.** After a guard passes, reintroduce the defect it
  guards and confirm the test fails. A test that passes against the reintroduced bug is
  testing nothing (six such tests were found in Plan 1).
- **Interpreter/OS/library-internals hazard.** Ask of every recorded or asserted value
  whether it depends on the interpreter, the OS, or a third-party library's internals
  (five Plan 1 defects were this one class). `crc32c` and `hashlib` digests are
  deterministic across platforms; wire framing and gzip internals are not.
- **Single gunicorn worker, `--reload` on.** Writing a `.py` file into the repo while an
  emulator is live restarts the worker mid-trace and wipes its state. Never edit source
  while a harness run or emulator-backed test is in flight.
- **Formatting is `isort` then `black`, in that order** (`isort==5.12.0`, `black==22.3.0`).
  A bare `isort --check-only` disagrees with the combination on one pre-existing import
  line; CI enforces the combination (`isort … && black … && git diff --exit-code`).

---

## Prerequisites already landed on this branch

These were meant to be this plan's first two tasks; they were pulled forward and landed as
their own commits because the conformance gate must be able to *see* the framing and
mutation axes that phase 3 refactors **before** any phase-3 code changes them. Do not
re-do them.

- **Framing gap closed** — commit `39193a7`. `Recorder.record_http` now records a derived
  `framing` field (`mode`: `content-length|chunked|none`, plus `content_length_matches_body`)
  so a switch to chunked encoding or a `Content-Length` inconsistent with the body moves a
  golden. Goldens regenerated; the change is purely additive; gate green on macOS + Linux +
  CI. **This is load-bearing for Task 7** (REST download → chunked generator): without it,
  the download refactor could change wire framing invisibly.
- **gRPC trace coverage added** — commit `772bdc8`. `UpdateObject`, `UpdateBucket`, and
  soft-deleted reads (`GetObject`/`ListObjects` with `soft_deleted=True`) now appear in
  `golden/grpc.json` (they had zero interactions). Determinism test green across two
  emulators.
- **Windows determinism flake fixed** — commit `ed9bb33` (unrelated to the seam; kept the
  branch's CI green).

### Configuration-A baseline (the only irreproducible artifact)

- **Configuration A** (pristine upstream, no `Store` seam): commit **`e8c8507`**.
- **A ≡ B re-proven against the final goldens** on 2026-07-29: restoring `gcs/` and
  `testbench/` from `e8c8507`, overlaying the current `tests/`, and running
  `PYTHONPATH=. python -m tests.conformance.harness` yields `OK faults / OK grpc / OK rest`,
  exit 0. So the committed goldens describe untouched-upstream behavior, framing field and
  all.
- **Golden digest** the A ≡ B proof matched (sha256 of `rest.json`+`grpc.json`+`faults.json`
  concatenated): `8eda6110f35c511b9afc7588bac771a5e18cc3b54b0dfa89eef960deba0c2fbb`. If a
  task other than a deliberate `--regenerate` changes this digest, the refactor was not a
  no-op — stop and diagnose.

### The phase-3 exit gate (what "done" means)

From the spec's per-phase gates, phase 3 is green when **all** of these hold:

1. `PYTHONPATH=. pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py`
   is green (the one ignored file hangs on macOS pre-existing; never add that ignore to CI).
2. `PYTHONPATH=. python -m tests.conformance.harness` prints `OK` for all three traces with
   an **empty diff** — the golden digest above is unchanged.
3. `nix develop --command make verify-linux` prints `OK` for all three traces.
4. The new coverage gate (Task 2) passes: every line in `tests/media_call_sites.txt` was
   executed by the conformance trace.
5. `git diff --name-only e8c8507..HEAD -- gcs/ testbench/` shows the seam files plus the new
   `testbench/media.py` and the phase-3 refactors — and nothing in `tests/conformance/`
   imports `gcs`/`testbench`.

---

## File Structure

- **Create `testbench/media.py`** — the `Media` protocol and `BytesMedia`. One
  responsibility: own object/upload bytes behind a streaming interface. No emulator logic.
- **Create `tests/test_media.py`** — unit tests for `BytesMedia` (every method, plus the
  bytes-compatibility surface the call sites rely on).
- **Create `tests/media_call_sites.txt`** — the committed, machine-readable audit: every
  `path:line` where object/upload media is accessed through the `Media` interface.
- **Create `tests/test_media_call_sites.py`** — runs the conformance trace under
  `coverage.py` and asserts every listed line executed. Converts the audit from a claim into
  a check.
- **Modify `gcs/object.py`** — `Object.media` becomes `BytesMedia`; `init` checksums,
  `_download_range`, the download assembly, and decompressive transcoding use the interface.
- **Modify `gcs/upload.py`** — `Upload.media` becomes `BytesMedia`; accumulation uses
  `append`; the O(n²) `crc32c.crc32c(upload.media)` recompute at :590 uses the incremental
  cached checksum.
- **Modify `testbench/grpc_server.py`** — compose (`:530`, `:1022`) and rewrite (`:1115`)
  stream through the interface instead of concatenating `bytes`.
- **Modify `testbench/rest_server.py`, `testbench/common.py`** — only where they touch
  `.media`; most sites are `len()`/slice and are unchanged by `BytesMedia`'s compatibility
  surface (verified by Task 3's gate, not assumed).

Each task ends with the conformance gate green (empty diff) and, from Task 2 on, the
coverage gate green.

---

### Task 1: `Media` interface and `BytesMedia`

**Files:**
- Create: `testbench/media.py`
- Test: `tests/test_media.py`

**Interfaces:**
- Produces: `class BytesMedia` with:
  - `BytesMedia(initial: bytes = b"")` constructor
  - `__len__() -> int`
  - `__getitem__(key: int | slice) -> int | bytes` (slice returns `bytes`)
  - `append(data: bytes) -> None` (accumulate; updates rolling checksums)
  - `__iadd__(data: bytes) -> "BytesMedia"` (so `x.media += content` keeps working)
  - `__add__(data: bytes) -> bytes` and `__radd__(data: bytes) -> bytes` (so
    `dst_bytes + x.media` and `x.media + dst_bytes` keep working during the refactor)
  - `chunks(begin: int, end: int, size: int) -> Iterator[bytes]` (streaming reads)
  - `crc32c() -> int` (cached, incremental)
  - `md5() -> bytes` (cached, incremental, `hashlib` digest)
  - `reader() -> io.BufferedReader`-like (file-like over the bytes, for streaming gzip)
  - `finalize(dest) -> None` (no-op for `BytesMedia`; `FileMedia` overrides in Plan 3)
  - `to_bytes() -> bytes` (explicit escape hatch for the few sites that genuinely need a
    contiguous copy)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_media.py
import hashlib
import unittest

import crc32c

from testbench.media import BytesMedia


class TestBytesMedia(unittest.TestCase):
    def test_len_and_slice_match_bytes(self):
        m = BytesMedia(b"The quick brown fox")
        self.assertEqual(19, len(m))
        self.assertEqual(b"quick", m[4:9])
        self.assertEqual(b"fox", m[-3:])
        self.assertEqual(ord("T"), m[0])

    def test_append_and_iadd_accumulate(self):
        m = BytesMedia(b"hello")
        m.append(b" ")
        m += b"world"
        self.assertEqual(b"hello world", m.to_bytes())
        self.assertEqual(11, len(m))

    def test_concatenation_both_sides_yields_bytes(self):
        # Compose/rewrite do `dst += src_object.media` and `composed += blob.media`.
        m = BytesMedia(b"world")
        self.assertEqual(b"helloworld", b"hello" + m)
        self.assertEqual(b"worldhello", m + b"hello")

    def test_crc32c_matches_whole_buffer_and_chains_on_append(self):
        # The load-bearing property: an incrementally-maintained crc32c must equal
        # crc32c over the whole buffer. Plan 1 pinned crc32c(data, seed) chaining.
        m = BytesMedia(b"hello")
        m.append(b"world")
        self.assertEqual(crc32c.crc32c(b"helloworld"), m.crc32c())

    def test_md5_matches_whole_buffer_and_chains_on_append(self):
        m = BytesMedia(b"hello")
        m.append(b"world")
        self.assertEqual(hashlib.md5(b"helloworld").digest(), m.md5())

    def test_chunks_covers_range_without_gaps_or_overlap(self):
        m = BytesMedia(b"0123456789")
        self.assertEqual([b"012", b"345", b"678", b"9"], list(m.chunks(0, 10, 3)))
        self.assertEqual([b"23", b"45"], list(m.chunks(2, 6, 2)))

    def test_reader_streams_the_whole_buffer(self):
        m = BytesMedia(b"streamed content")
        self.assertEqual(b"streamed content", m.reader().read())

    def test_empty_media_checksums_match_empty_bytes(self):
        m = BytesMedia()
        self.assertEqual(crc32c.crc32c(b""), m.crc32c())
        self.assertEqual(hashlib.md5(b"").digest(), m.md5())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_media.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'testbench.media'`.

- [ ] **Step 3: Write `testbench/media.py`**

```python
#!/usr/bin/env python3
# Copyright 2026 Google LLC (Apache-2.0 header as in sibling files)
"""Media abstraction between the emulator and object/upload bytes.

`BytesMedia` is behaviourally identical to the raw `bytes` the emulator used
before this seam: it is what `NullStore` uses and what keeps phase 3 a pure
refactor. `FileMedia` (Plan 3) will back the same interface with a real file.

The bytes-compatibility surface (`__len__`, `__getitem__`, `__add__`,
`__radd__`, `__iadd__`) exists so the ~71 existing `.media` call sites that
only slice, measure, or concatenate keep working unchanged; the streaming
methods (`chunks`, `reader`, incremental `crc32c`/`md5`) are what the
size-sensitive paths migrate onto so `FileMedia` can later avoid
materialising multi-GB buffers.
"""

import hashlib
import io

import crc32c


class BytesMedia:
    def __init__(self, initial=b""):
        self._buf = bytearray(initial)
        # Rolling checksums. Maintained incrementally so crc32c()/md5() are O(1)
        # and the O(n^2) whole-buffer recompute at gcs/upload.py:590 becomes O(n)
        # total. crc32c(data, seed) chaining was pinned in Plan 1
        # (tests/test_crc32c_assumptions.py); hashlib is natively incremental.
        self._crc = crc32c.crc32c(bytes(self._buf))
        self._md5 = hashlib.md5(bytes(self._buf))

    def __len__(self):
        return len(self._buf)

    def __getitem__(self, key):
        # A slice returns bytes (not bytearray) so callers see exactly what raw
        # `bytes[...]` gave them; an int index returns an int, as bytes does.
        result = self._buf[key]
        return bytes(result) if isinstance(key, slice) else result

    def append(self, data):
        self._buf.extend(data)
        self._crc = crc32c.crc32c(data, self._crc)
        self._md5.update(data)

    def __iadd__(self, data):
        self.append(data)
        return self

    def __add__(self, data):
        return bytes(self._buf) + data

    def __radd__(self, data):
        return data + bytes(self._buf)

    def chunks(self, begin, end, size):
        pos = begin
        while pos < end:
            stop = min(pos + size, end)
            yield bytes(self._buf[pos:stop])
            pos = stop

    def crc32c(self):
        return self._crc

    def md5(self):
        return self._md5.digest()

    def reader(self):
        return io.BytesIO(bytes(self._buf))

    def finalize(self, dest):
        # BytesMedia has nothing to promote; FileMedia (Plan 3) overrides this to
        # os.replace a staging file into its final path.
        return None

    def to_bytes(self):
        return bytes(self._buf)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_media.py -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Mutation-check the checksum-chaining tests**

Temporarily change `append`'s `self._crc = crc32c.crc32c(data, self._crc)` to
`self._crc = crc32c.crc32c(data)` (drop the seed). Re-run
`test_crc32c_matches_whole_buffer_and_chains_on_append`. Expected: FAIL. Revert. Repeat by
replacing `self._md5.update(data)` with `pass` and confirm
`test_md5_matches_whole_buffer_and_chains_on_append` FAILS. Revert. This proves the tests
actually pin incremental chaining rather than coincidentally passing.

- [ ] **Step 6: Confirm 3.8 parse and format, then commit**

```bash
.venv/bin/python -c "import ast; ast.parse(open('testbench/media.py').read(), feature_version=(3,8))"
.venv/bin/isort --quiet testbench/media.py tests/test_media.py && .venv/bin/black --quiet testbench/media.py tests/test_media.py
git add testbench/media.py tests/test_media.py
git commit -m "feat(media): add Media interface and BytesMedia (byte-identical wrapper)"
```

---

### Task 2: Call-site audit and coverage gate

**Files:**
- Create: `tests/media_call_sites.txt`
- Create: `tests/test_media_call_sites.py`

**Interfaces:**
- Consumes: the conformance trace entry points (`tests/conformance/trace_rest.py`,
  `trace_grpc.py`, `trace_faults.py`) run under `coverage.py`.
- Produces: a passing gate that fails if any listed `path:line` is not executed by the
  trace. Later tasks add lines to `media_call_sites.txt` as they migrate sites onto the
  `Media` interface.

**Why this task before the refactor:** the audit output is only trustworthy if it is
checked. This gate turns "we audited the ~71 sites" into "every migrated site is executed
by the trace, enforced on every commit". Start it with the sites the trace *already*
exercises today, so the gate is green before Task 3 and each later task adds its own lines.

- [ ] **Step 1: Generate the initial audit list from real coverage**

Run the trace under coverage and capture which `.media` lines actually execute today. This
is a one-time bootstrap of the committed file, not the test:

```bash
PYTHONPATH=. .venv/bin/python -m coverage run --source=gcs,testbench \
  -m pytest tests/conformance/test_traces.py -q
PYTHONPATH=. .venv/bin/python - <<'PY'
import re
from coverage import Coverage
cov = Coverage(); cov.load()
data = cov.get_data()
out = []
for f in ("gcs/object.py", "gcs/upload.py", "testbench/grpc_server.py",
          "testbench/rest_server.py", "testbench/common.py"):
    src = open(f).read().splitlines()
    executed = set(data.lines(__import__("os").path.abspath(f)) or [])
    for i, line in enumerate(src, 1):
        if re.search(r"\.media\b", line) and "media_link" not in line and i in executed:
            out.append("%s:%d" % (f, i))
open("tests/media_call_sites.txt", "w").write("\n".join(out) + "\n")
print("wrote", len(out), "covered media call sites")
PY
```

Review the file by hand: every entry must be a genuine object/upload media access, not a
`media_link`/comment false positive. Sites that are *not* covered by the trace today are a
coverage gap — note them in the file as commented `# UNCOVERED path:line reason` lines so
the gap is visible rather than silently dropped.

- [ ] **Step 2: Write the failing gate test**

```python
# tests/test_media_call_sites.py
import os
import unittest

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST = os.path.join(REPO, "tests", "media_call_sites.txt")


def _listed_sites():
    sites = []
    with open(LIST) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            path, num = line.rsplit(":", 1)
            sites.append((path, int(num)))
    return sites


@pytest.mark.skipif(
    os.name == "nt", reason="coverage line numbers pinned on the Linux/macOS capture"
)
class TestMediaCallSitesAreCovered(unittest.TestCase):
    """Every media call site in the committed audit must be executed by the
    conformance trace. A new `.media` migration added without trace coverage
    fails here -- converting the ~71-site audit from a claim into a check."""

    @classmethod
    def setUpClass(cls):
        from coverage import Coverage

        cls.cov = Coverage(source=["gcs", "testbench"])
        cls.cov.start()
        # Import and run the traces in-process so coverage sees the emulator's
        # own lines. The emulator subprocess is a separate interpreter, so this
        # measures the client-visible call paths the traces drive.
        from tests.conformance import trace_faults, trace_grpc, trace_rest
        from tests.conformance.emulator import Emulator

        for module in (trace_rest, trace_grpc, trace_faults):
            with Emulator() as emu:
                module.run(emu)
        cls.cov.stop()
        cls.cov.save()
        cls.data = cls.cov.get_data()

    def test_every_listed_site_executed(self):
        missing = []
        for path, num in _listed_sites():
            abspath = os.path.join(REPO, path)
            executed = set(self.data.lines(abspath) or [])
            if num not in executed:
                missing.append("%s:%d" % (path, num))
        self.assertEqual([], missing, "media call sites never executed: %s" % missing)
```

> **Coverage-of-the-emulator caveat, resolve during Step 3:** the emulator runs in a
> subprocess (`Emulator` launches `testbench_run.py`), so in-process `Coverage` will *not*
> see its lines. Two options; pick one and record the choice in the test's docstring:
> (a) set `COVERAGE_PROCESS_START` and use `coverage.process_startup()` so the subprocess
> writes its own `.coverage.<pid>` (the emulator's env allowlist in
> `tests/conformance/emulator.py` must pass `COVERAGE_PROCESS_START` through — that is a
> real edit to the allowlist), then `combine()`; or (b) drive the emulator's request
> handlers in-process without a subprocess for this test only. Option (a) matches
> `.codecov.yml`'s existing subprocess-coverage setup and is preferred.

- [ ] **Step 3: Wire subprocess coverage and run the gate to green**

Add `COVERAGE_PROCESS_START` to the `Emulator` env allowlist in
`tests/conformance/emulator.py` (a named allowlist entry, commented as coverage-only), and
create a `.coveragerc` (or extend the existing config) with `[run] parallel = true` and
`concurrency = multiprocessing,thread`. Have the emulator's entry point call
`coverage.process_startup()` when `COVERAGE_PROCESS_START` is set. Then:

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_media_call_sites.py -q`
Expected: PASS — every listed site executed.

- [ ] **Step 4: Mutation-check the gate**

Add a deliberately-unexecuted line to `tests/media_call_sites.txt` (e.g. a `.media` line
inside a branch the trace never takes, or a fabricated `gcs/object.py:1`). Re-run the gate.
Expected: FAIL naming that site. Remove the line. This proves the gate detects an
unexercised site rather than passing vacuously.

- [ ] **Step 5: Format and commit**

```bash
.venv/bin/isort --quiet tests/test_media_call_sites.py && .venv/bin/black --quiet tests/test_media_call_sites.py
git add tests/media_call_sites.txt tests/test_media_call_sites.py tests/conformance/emulator.py .coveragerc
git commit -m "test(media): coverage-gate the media call-site audit"
```

---

### Task 3: Route `Object.media` through `BytesMedia`

**Files:**
- Modify: `gcs/object.py:70` (`self.media = media`) and every construction of `Object`
- Test: the conformance gate (empty diff) + `tests/test_media_call_sites.py`

**Interfaces:**
- Consumes: `BytesMedia` from Task 1.
- Produces: `Object.media` is a `BytesMedia`. Read sites (`blob.media[a:b]`,
  `len(blob.media)`, `dst += blob.media`) are unchanged because `BytesMedia`'s
  compatibility surface covers them.

**Approach:** this is where "does `BytesMedia` really behave like `bytes` at every site?"
gets *tested*, not assumed. Wrap at construction, run the full suite + gate, and let any
site that `BytesMedia` does not cover surface as a failure to fix explicitly.

- [ ] **Step 1: Confirm the gate is green before the change**

Run: `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness`
Expected: `OK faults / OK grpc / OK rest`, empty diff. (This is the baseline the refactor
must preserve.)

- [ ] **Step 2: Wrap media at construction**

In `gcs/object.py`, change the constructor to hold a `BytesMedia`:

```python
# gcs/object.py, near the top
from testbench.media import BytesMedia

# in Object.__init__ (currently `self.media = media` at line 70):
self.media = media if isinstance(media, BytesMedia) else BytesMedia(media)
```

Do the same at every place an `Object` is built with raw bytes (`init`, `init_dict`,
`init_media`, `init_multipart`, `init_xml`, and the compose/rewrite destinations). Grep:
`grep -n "Object(" gcs/object.py testbench/grpc_server.py`.

- [ ] **Step 3: Run the full suite and the gate**

Run:
```bash
PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness
```
Expected: suite green; harness `OK` all three with empty diff. **If any site fails** (e.g.
a `MessageTo*` call that needs raw `bytes`, or a `+` where neither side is `BytesMedia`),
fix that site to use `.to_bytes()` or the streaming method — do not widen `BytesMedia` to
paper over a genuinely bytes-only API. Record each such site in
`tests/media_call_sites.txt`.

- [ ] **Step 4: Mutation-check that the gate sees a media regression**

Temporarily make `BytesMedia.__getitem__` return the wrong bytes for a slice (e.g.
`self._buf[key][:-1]`). Re-run the harness. Expected: FAIL with a diff in a download or read
interaction. Revert. This proves the gate actually polices media content, so the empty-diff
claim in Steps 3/final means something.

- [ ] **Step 5: Format and commit**

```bash
.venv/bin/isort --quiet gcs/object.py && .venv/bin/black --quiet gcs/object.py
git add gcs/object.py tests/media_call_sites.txt
git commit -m "refactor(object): back Object.media with BytesMedia (no behaviour change)"
```

---

### Task 4: Route `Upload.media` through `BytesMedia` and kill the O(n²) checksum recompute

**Files:**
- Modify: `gcs/upload.py:272,588` (`upload.media += content`), `gcs/upload.py:590`
  (`persisted_crc32c = crc32c.crc32c(upload.media)`)
- Test: the conformance gate + a linear-time checksum detector

**Interfaces:**
- Consumes: `BytesMedia` (accumulation via `append`/`+=`, `crc32c()`).
- Produces: `Upload.media` is a `BytesMedia`; `persisted_crc32c` reads the cached rolling
  checksum in O(1).

- [ ] **Step 1: Write the failing linear-time detector**

```python
# tests/test_upload_checksum_scaling.py
import time
import unittest

from testbench.media import BytesMedia


class TestChecksumScaling(unittest.TestCase):
    """The pre-seam BidiWrite path recomputed crc32c over the whole accumulated
    buffer on every flush (gcs/upload.py:590), which is O(n^2) in the upload
    size. BytesMedia's rolling checksum makes N flushes O(n) total. Timing the
    'N flushes of a fixed chunk' pattern at size N and 2N must stay roughly
    linear: quadratic gives ~4x, linear ~2x. The 3x threshold is machine-speed
    independent (a ratio), the robust-detector shape from the spec."""

    def _time_flush_pattern(self, flushes, chunk=b"x" * 65536):
        m = BytesMedia()
        start = time.perf_counter()
        for _ in range(flushes):
            m.append(chunk)
            _ = m.crc32c()  # what :590 does every flush
        return time.perf_counter() - start

    def test_flush_checksum_is_not_quadratic(self):
        t_n = self._time_flush_pattern(500)
        t_2n = self._time_flush_pattern(1000)
        self.assertLess(t_2n / max(t_n, 1e-6), 3.0)
```

- [ ] **Step 2: Verify it fails against a whole-buffer recompute**

Temporarily give `BytesMedia.crc32c` a whole-buffer recompute
(`return crc32c.crc32c(bytes(self._buf))`) to model the current bug.
Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_upload_checksum_scaling.py -q`
Expected: FAIL (ratio ≈ 4). Revert `BytesMedia.crc32c` to the cached `return self._crc`.
Re-run: PASS. This proves the detector distinguishes O(n²) from O(n).

- [ ] **Step 3: Reroute the upload accumulation and the :590 recompute**

```python
# gcs/upload.py — accumulation (was `upload.media += content` at :272 and :588)
upload.media += content            # unchanged token; BytesMedia.__iadd__ chains the checksum

# gcs/upload.py:590 — was `persisted_crc32c = crc32c.crc32c(upload.media)`
persisted_crc32c = upload.media.crc32c()
```

Ensure `Upload.media` is constructed as `BytesMedia` (grep `Upload(` and the
`_insert_empty_appendable_object` path at :298-:304; wrap the initial media there).

- [ ] **Step 4: Run the suite, the gate, and the detector**

Run:
```bash
PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness
PYTHONPATH=. .venv/bin/python -m pytest tests/test_upload_checksum_scaling.py -q
```
Expected: suite green; harness `OK` all three, empty diff; detector PASS.

- [ ] **Step 5: Format, add the migrated sites to the audit, and commit**

```bash
.venv/bin/isort --quiet gcs/upload.py && .venv/bin/black --quiet gcs/upload.py
git add gcs/upload.py tests/test_upload_checksum_scaling.py tests/media_call_sites.txt
git commit -m "perf(upload): back Upload.media with BytesMedia; O(1) rolling crc32c at flush"
```

---

### Task 5: Incremental checksums in `Object.init`

**Files:**
- Modify: `gcs/object.py:114-115` (`hashlib.md5(media)` / `crc32c.crc32c(media)`)
- Test: the conformance gate

**Interfaces:**
- Consumes: `BytesMedia.crc32c()` / `.md5()`.

- [ ] **Step 1: Confirm the gate is green (baseline)**

Run: `PYTHONPATH=. .venv/bin/python -m tests.conformance.harness` → empty diff.

- [ ] **Step 2: Use the media's cached checksums**

```python
# gcs/object.py:114-115 — was:
#   actual_md5Hash = hashlib.md5(media).digest()
#   actual_crc32c = crc32c.crc32c(media)
# media here is now a BytesMedia (Task 3):
actual_md5Hash = media.md5()
actual_crc32c = media.crc32c()
```

If `media` at this site can still be raw `bytes` on some path, normalise once at the top of
`init` (`media = media if isinstance(media, BytesMedia) else BytesMedia(media)`) rather than
branching at each use.

- [ ] **Step 3: Run the suite and the gate**

Run:
```bash
PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness
```
Expected: green; empty diff. The digests are identical because `BytesMedia` computes the
same crc32c/md5 over the same bytes.

- [ ] **Step 4: Format, update the audit, commit**

```bash
.venv/bin/isort --quiet gcs/object.py && .venv/bin/black --quiet gcs/object.py
git add gcs/object.py tests/media_call_sites.txt
git commit -m "refactor(object): compute init checksums via BytesMedia"
```

---

### Task 6: Range reads via `chunks`

**Files:**
- Modify: `gcs/object.py:462-505` (`_download_range` slicing), `testbench/grpc_server.py`
  read sites (`:595-638`, `:723-742`)
- Test: the conformance gate (the `read-object-ranged`, `download-range-*` interactions)

**Interfaces:**
- Consumes: `BytesMedia.__getitem__` (already used by slicing) and `chunks(begin, end,
  size)` for the streamed gRPC/REST read paths.

**Note:** at phase 3 the slicing (`response_payload[begin:end]`) already works via
`BytesMedia.__getitem__` returning `bytes`, so the *behaviour* is preserved by Task 3
alone. This task migrates the **streaming** read loops onto `chunks(...)` so `FileMedia`
(Plan 5) streams from mmap instead of materialising, and records those lines in the audit.

- [ ] **Step 1: Confirm the range interactions are in the goldens**

The conformance trace already covers `read-object-ranged`, `read-object-negative-offset`,
`read-object-offset-past-end`, `bidi-read-two-ranges`, and REST `download-range-middle`,
`download-range-open-ended`, `download-range-suffix`, `download-range-unsatisfiable`. Verify:
`grep -c "download-range\|read-object-ranged" tests/conformance/golden/*.json`.

- [ ] **Step 2: Migrate the gRPC read chunk loop onto `chunks`**

```python
# testbench/grpc_server.py, ReadObject/BidiReadObject chunk emission — was slicing
# blob.media[start:end] into a fixed CHUNK. Now:
for chunk in blob.media.chunks(start, read_end, GRPC_CHUNK_SIZE):
    yield storage_pb2.ReadObjectResponse(
        checksummed_data=storage_pb2.ChecksummedData(
            content=chunk, crc32c=crc32c.crc32c(chunk)
        )
    )
```

Preserve the exact chunk size the emulator uses today so chunk boundaries — which the
harness records via `record_stream` offsets — do not move. Find it near the current read
loop; do not invent a new size.

- [ ] **Step 3: Run the suite and the gate**

Run:
```bash
PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness
```
Expected: green; empty diff — including identical `offsets` on the streamed reads.

- [ ] **Step 4: Mutation-check chunk boundaries are policed**

Temporarily change `GRPC_CHUNK_SIZE` (or the size passed to `chunks`) to a different value.
Re-run the harness. Expected: FAIL with an `offsets` diff on `read-object-*`/`bidi-read-*`.
Revert. This confirms the streamed-read golden pins chunk boundaries.

- [ ] **Step 5: Format, update the audit, commit**

```bash
.venv/bin/isort --quiet testbench/grpc_server.py && .venv/bin/black --quiet testbench/grpc_server.py
git add testbench/grpc_server.py tests/media_call_sites.txt
git commit -m "refactor(grpc): stream ranged reads through BytesMedia.chunks"
```

---

### Task 7: REST download → chunked generator

**Files:**
- Modify: `gcs/object.py` download assembly (`:497-517`, the single `yield response_payload`)
- Test: the conformance gate, specifically the **framing** field added in commit `39193a7`

**Interfaces:**
- Consumes: `BytesMedia.chunks(...)` / `reader()`.

**Why the framing prerequisite matters here:** turning the single-shot
`yield response_payload` into a chunked generator is exactly the change that could flip the
response from `Content-Length` framing to chunked transfer encoding. Because commit `39193a7`
records `framing.mode` and `content_length_matches_body`, such a flip now moves a golden. Do
not proceed if the framing field is absent from the goldens.

- [ ] **Step 1: Record the current framing of the download interactions**

`grep -A3 '"download-full"\|"download-range-middle"' tests/conformance/golden/rest.json` and
note the `framing` block (expected `mode: content-length` today). This is what must not
change.

- [ ] **Step 2: Convert the download to a chunked generator that preserves framing**

```python
# gcs/object.py — was a single `yield response_payload`. Stream in chunks while
# preserving the framing the client sees today: if the emulator set an explicit
# Content-Length header, keep setting it (so mode stays "content-length" and
# content_length_matches_body stays true); only the number of yields changes.
def _stream(media, begin, end, size=DOWNLOAD_CHUNK_SIZE):
    for chunk in media.chunks(begin, end, size):
        yield chunk
```

Keep whatever header the response object sets for `Content-Length` unchanged — the point of
phase 3 is that `BytesMedia` still knows its full length (`len(media)`), so framing is
identical; only `FileMedia` streaming (Plan 5) might later change it, and the framing field
will catch that.

- [ ] **Step 3: Run the suite and the gate**

Run:
```bash
PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness
```
Expected: green; empty diff — the `framing` block on every download interaction unchanged.

- [ ] **Step 4: Mutation-check that framing is policed**

Temporarily drop the `Content-Length` header on the download response (forcing chunked
framing). Re-run the harness. Expected: FAIL with a `framing.mode` diff
(`content-length` → `chunked` or `none`). Revert. This confirms the framing field earns its
place and the download refactor is genuinely covered.

- [ ] **Step 5: Format, update the audit, commit**

```bash
.venv/bin/isort --quiet gcs/object.py && .venv/bin/black --quiet gcs/object.py
git add gcs/object.py tests/media_call_sites.txt
git commit -m "refactor(object): stream REST downloads via BytesMedia.chunks (framing preserved)"
```

---

### Task 8: Streaming decompressive transcoding

**Files:**
- Modify: `gcs/object.py:462-499` (`gzip.decompress(self.media)`)
- Test: the conformance gate (`download-transcoded`, `download-not-transcoded`)

**Interfaces:**
- Consumes: `BytesMedia.reader()`.

**Hazard (interpreter/library-internals class):** the trace deliberately distinguishes the
transcoded path (server decompresses, no `Content-Encoding`) from the not-transcoded path
(raw gzip bytes, `Content-Encoding: gzip`) — see `trace_rest.py`'s `download-not-transcoded`
comment. The `download-transcoded` golden pins the *decompressed* bytes; streaming gzip must
produce identical output, not merely valid gzip.

- [ ] **Step 1: Note the transcoded golden**

`grep -A6 '"download-transcoded"' tests/conformance/golden/rest.json` — record the body
`length`/`sha256`. That is the decompressed payload; it must be byte-identical after the
change.

- [ ] **Step 2: Stream gzip over the media reader**

```python
# gcs/object.py:499 — was `gzip.decompress(self.media)` (materialises both buffers).
import gzip
# is_decompressive_transcode branch:
with gzip.GzipFile(fileobj=self.media.reader(), mode="rb") as gz:
    response_payload = gz.read()
```

At phase 3 this still reads the whole payload (`BytesMedia` is in memory); the shape change
is that it streams over `reader()`, so Plan 5's `FileMedia.reader()` makes it O(1) memory.
Behaviour is identical: `gzip.GzipFile(...).read()` yields the same bytes as
`gzip.decompress(...)`.

- [ ] **Step 3: Run the suite and the gate**

Run:
```bash
PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness
```
Expected: green; empty diff — `download-transcoded` body `length`/`sha256` unchanged, and
`download-not-transcoded` still carries the raw gzip bytes and `Content-Encoding: gzip`.

- [ ] **Step 4: Format, update the audit, commit**

```bash
.venv/bin/isort --quiet gcs/object.py && .venv/bin/black --quiet gcs/object.py
git add gcs/object.py tests/media_call_sites.txt
git commit -m "refactor(object): stream decompressive transcoding via BytesMedia.reader"
```

---

### Task 9: Streaming compose and rewrite

**Files:**
- Modify: `testbench/grpc_server.py:530` (compose `composed_media += source_blob.media`),
  `:1022` (`dst_media += src_object.media`), `:1115` (rewrite slice-and-append)
- Test: the conformance gate (`compose-object`, `rewrite-step-*`, `move-object`)

**Interfaces:**
- Consumes: `BytesMedia` accumulation (`append`) and the compatibility surface (`+=`,
  `dst += src.media`).

- [ ] **Step 1: Confirm compose/rewrite interactions are in the goldens**

`grep -c "compose-object\|rewrite-step\|move-object" tests/conformance/golden/grpc.json`.

- [ ] **Step 2: Accumulate into a `BytesMedia` staging buffer**

```python
# compose (was `composed_media += source_blob.media` at :530, `dst_media += ...` at :1022):
composed_media = BytesMedia()
for source_blob in sources:
    composed_media.append(source_blob.media.to_bytes())   # Plan 5: chunked file-to-file

# rewrite (was `rewrite.media += src_object.media[len(rewrite.media):total]` at :1115):
rewrite.media.append(src_object.media[len(rewrite.media):total_bytes_rewritten])
```

`rewrite.media` must be a `BytesMedia` (wrap where the rewrite state is created). The
`.to_bytes()` in compose is the explicit escape hatch; Plan 5 replaces it with
`chunks(...)` into a staging file, and the coverage gate will force that line to be
exercised.

- [ ] **Step 3: Run the suite and the gate**

Run:
```bash
PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness
```
Expected: green; empty diff on `compose-object`, `rewrite-step-*`, `move-object`.

- [ ] **Step 4: Mutation-check compose/rewrite output is policed**

Temporarily append sources in reverse order (or drop the last source). Re-run the harness.
Expected: FAIL with a body-digest diff on `compose-object`. Revert. Confirms the compose
golden pins the composed content.

- [ ] **Step 5: Format, update the audit, commit**

```bash
.venv/bin/isort --quiet testbench/grpc_server.py && .venv/bin/black --quiet testbench/grpc_server.py
git add testbench/grpc_server.py tests/media_call_sites.txt
git commit -m "refactor(grpc): accumulate compose/rewrite through BytesMedia"
```

---

### Task 10: Phase-3 exit — full verification and audit completeness

**Files:** none (verification only), except a final pass on `tests/media_call_sites.txt`.

- [ ] **Step 1: Every `.media` site is either migrated-and-listed or justified**

```bash
grep -rnE "\.media\b" gcs/ testbench/ | grep -v "media_link\|media_call" \
  | grep -v "testbench/media.py" > /tmp/all_media_sites.txt
```
For each of the ~71 sites, confirm it is either in `tests/media_call_sites.txt` (covered by
the trace) or annotated in that file as `# UNCOVERED …` with a reason (e.g. an error branch
the trace does not hit). No site may be silently absent — silent absence reads as "audited"
when it was not.

- [ ] **Step 2: Run the full phase-3 gate**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q --ignore=tests/test_testbench_continue_after_fault_injection.py
PYTHONPATH=. .venv/bin/python -m tests.conformance.harness      # empty diff; digest unchanged
PYTHONPATH=. .venv/bin/python -m pytest tests/test_media_call_sites.py -q
nix develop --command make verify-linux                          # OK all three on Linux
```
Confirm the golden digest still equals
`8eda6110f35c511b9afc7588bac771a5e18cc3b54b0dfa89eef960deba0c2fbb` — an unchanged digest is
the proof phase 3 was a no-op.

- [ ] **Step 3: Confirm no harness-internals leak and the production diff is only the seam**

```bash
grep -rnE "import (gcs|testbench)" tests/conformance/ | grep -v emulator.py   # expect empty
git diff --name-only e8c8507..HEAD -- gcs/ testbench/                          # seam + media.py + phase-3 refactors only
```

- [ ] **Step 4: Push and confirm CI green (all matrix legs + Conformance baseline)**

```bash
git push origin file-backend-design
gh run list --branch file-backend-design --limit 3
```

- [ ] **Step 5: Update the handoff for Plan 3**

Record: phase 3 landed; the golden digest is unchanged (no-op proven); `media_call_sites.txt`
is the audit Plan 3 extends; `FileMedia` (Plan 5) overrides `chunks`/`reader`/`finalize` and
the `.to_bytes()` escape hatches in compose/rewrite are the lines it must replace with
streaming.

---

## Self-Review

**Spec coverage (phase 3 rows of the per-phase gate + Seam B table):**
- `Media` interface (`__len__`, `__getitem__`, `chunks`, `append`, `crc32c`, `md5`,
  `reader`, `finalize`) → Task 1. ✅
- `BytesMedia` preserves today's semantics → Tasks 1, 3, 4 (gate empty diff). ✅
- Call-site audit → `tests/media_call_sites.txt` + coverage gate → Task 2, extended by every
  refactor task, completeness enforced in Task 10. ✅
- Incremental crc32c/md5, O(n²) recompute at `upload.py:590` removed → Tasks 1, 4, 5. ✅
- Range reads, REST download chunking (`object.py:515-517`), transcoding (`object.py:499`),
  compose (`grpc_server.py:530`), rewrite (`grpc_server.py:1115`) → Tasks 6–9. ✅
- Empty harness diff / no allow-list at phase 3 → every refactor task's gate + Task 10. ✅
- Framing observability prerequisite → landed (`39193a7`), consumed by Task 7. ✅

**Placeholder scan:** no `TBD`/`TODO`/"handle edge cases"; every code step shows real code
or a real command. The one deferred decision (subprocess coverage mechanism, Task 2 Step 2)
is stated as an explicit choice with both options and a recommendation, not a blank.

**Type consistency:** `BytesMedia` method names (`append`, `chunks(begin, end, size)`,
`crc32c()`, `md5()`, `reader()`, `finalize(dest)`, `to_bytes()`) are used identically in
Tasks 3–9. Slicing returns `bytes`; `crc32c()` returns `int`; `md5()` returns the digest
`bytes` — matching how `gcs/object.py:114-115` and `gcs/upload.py:590` consume them.

**Known risk carried into execution:** Task 2's in-process-vs-subprocess coverage is the one
place that can fight the `Emulator` subprocess model. It is called out with a concrete
resolution (env-allowlist + `coverage.process_startup()` + parallel combine) rather than
discovered mid-task.
