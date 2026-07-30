# Handoff: Media seam (spec phase 3) → Plan 3

**Status:** Phase 3 landed on `file-backend-design` (PR #1, draft). CI green on
every matrix leg + the Conformance baseline on x86_64; `make verify-linux` green
on Linux; the macOS conformance gate green.

## What landed

The `Media` abstraction (`testbench/media.py`, class `BytesMedia`) now sits
between the emulator and the raw object/upload/rewrite bytes. `Object.media`,
`Upload.media`, and `Rewrite.media` are `BytesMedia`. The size-sensitive paths
(init checksums, gRPC ranged reads, REST download, decompressive transcoding,
compose, rewrite) stream through explicit methods (`chunks`, `reader`,
incremental `crc32c`/`md5`, `append`, `to_bytes`); a bytes-compatibility surface
(`__len__`, `__getitem__`, `__add__`/`__radd__`/`__iadd__`, `__eq__`) keeps the
remaining call sites unchanged.

## The no-op is proven

This phase is a **pure refactor**. The conformance harness diff is EMPTY and the
golden digest is unchanged:

    sha256(rest.json + grpc.json + faults.json) =
    8eda6110f35c511b9afc7588bac771a5e18cc3b54b0dfa89eef960deba0c2fbb

If a future change moves that digest without an intended, reviewed behavior
change, the change is not a no-op — diagnose, don't `--regenerate`.

## The audit Plan 3 extends

`tests/media_call_sites.txt` is the machine-checked call-site audit
(`tests/test_media_call_sites.py` runs the conformance trace under subprocess
coverage and asserts every ACTIVE `path:line` executed). Current state:
**86 sites = 57 active + 29 `# UNCOVERED`**. Every genuine object/upload/rewrite
`.media` access is either active or annotated `# UNCOVERED … <reason>` — no
silent absences. Plan 3 extends this list as it migrates more sites.

Subprocess-coverage wiring (so the gunicorn worker's lines are measured):
`.coveragerc` (`parallel`/`concurrency=multiprocessing,thread`/`sigterm=true`),
`COVERAGE_PROCESS_START` forwarded through the emulator env allowlist
(`tests/conformance/emulator.py`), and a guarded `coverage.process_startup()`
at the top of `testbench/__init__.py` (imports `coverage` only under the gate,
so the zero-runtime-dependency property holds).

## What `FileMedia` (Plan 3/5) must do

`FileMedia` backs the same interface with a real file. It overrides
`chunks(begin, end, size)`, `reader()`, and `finalize(dest)` (a no-op on
`BytesMedia`) to stream from disk/mmap instead of materialising a buffer. The
explicit **`.to_bytes()` escape hatches are the lines Plan 3 must replace with
true streaming** — each one materialises the whole payload today:

- Compose accumulation — `testbench/grpc_server.py` (`composed_media.append(source_blob.media.to_bytes())`).
- Rewrite single-shot / MoveObject dst — `testbench/grpc_server.py` (`dst_media.append(src_object.media.to_bytes())`).
- gRPC `WriteObject` → `Object.init` — `testbench/grpc_server.py` (`upload.media.to_bytes()`).
- REST resumable finalize → `Object.init` — `testbench/rest_server.py` (`upload.media.to_bytes()`).
- Appendable-upload insert / finalize → `Object.init` — `gcs/upload.py` (`upload.media.to_bytes()`).
- gzip decompressive transcode already streams over `self.media.reader()`
  (`gcs/object.py`); `FileMedia.reader()` makes it O(1) memory automatically.
- REST download already streams via `self.media.chunks(...)` through
  `_stream_media` (`gcs/object.py`); `FileMedia.chunks()` makes it O(1) memory.
  Framing is pinned by the `framing` field in the goldens — keep `Content-Length`
  set so a switch to chunked transfer encoding moves a golden.

## Two carried-forward findings (documented in code)

1. **REST compose/rewrite still use the bytes-compatibility operators**
   (`composed_media += source_object.media`, `dst_media += src_object.media`,
   `rewrite.media += src_object.media[...]` in `testbench/rest_server.py`) rather
   than the explicit `.append(...to_bytes())` idiom the gRPC path uses. Phase 3
   scoped the streaming migration to gRPC. These work today via `__radd__`/
   `__iadd__`, but a `FileMedia` `b"" + file_media` would silently materialise the
   whole file with no `.to_bytes()` breadcrumb. Plan 3 should migrate these REST
   sites to explicit streaming. (See the in-code comments there.)

2. **Appendable-upload `blob.media = upload.media` now aliases** the same mutable
   `BytesMedia` (the `isinstance` guard is always true because `Upload.init` seeds
   `BytesMedia(b"")`), whereas the pre-seam `bytes` code kept a frozen snapshot per
   flush. Currently benign (finalized object identical; the path always
   checkpoints/flushes and no mid-upload read exists) and trace-uncovered. A
   `FileMedia` with real staging/`finalize` must decide whether a snapshot copy is
   needed at that handoff. (See the in-code comment in `gcs/upload.py`.)

## Non-negotiables carried forward

- Zero new runtime dependencies (`setup.py` unchanged; `coverage`/`hypothesis`
  are dev-only in `flake.nix`).
- Python floor 3.8; `isort` then `black`.
- Nothing in `tests/conformance/` imports `gcs`/`testbench` internals except
  `emulator.py`.
- The one hanging test on macOS
  (`tests/test_testbench_continue_after_fault_injection.py`) is pre-existing;
  ignore it locally, never add that ignore to CI. The 3 `test_testbench_startup.py`
  failures seen locally are an environment gap (system `python3` lacks
  `waitress`); CI installs it and they pass there.
- Unit Test CI's Py3.9 leg has an intermittent core-dump flake
  (`Aborted (core dumped)` under `pytest --cov`) that predates this work; re-run
  to confirm a flake rather than a regression.
