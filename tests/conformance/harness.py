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

"""Capture or verify the emulator's external behavior against goldens.

    python -m tests.conformance.harness              # verify
    python -m tests.conformance.harness --regenerate # rewrite goldens

Regenerating is a reviewable act: any resulting change to a golden file must
be justified in the commit message. An unexplained golden diff is a defect,
not a nuisance.
"""

import argparse
import difflib
import json
import os
import re
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

# Interaction dicts are the elements of the top-level "interactions" list;
# with `serialize`'s `indent=2`, each element -- and only a list element,
# never a value nested deeper inside one -- starts and ends on its own line
# at exactly this 4-space indent.
_INTERACTION_START = re.compile(r"^    \{$")
_INTERACTION_END = re.compile(r"^    \},?$")
_LABEL_LINE = re.compile(r'^\s*"label":\s*"(?P<label>[^"]*)"')
_HUNK_HEADER = re.compile(r"^@@ -(?P<start>\d+)")
_CONTEXT_LINE = re.compile(r"^ ")


def golden_path(name):
    return os.path.join(GOLDEN_DIR, name + ".json")


def serialize(record):
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


def capture(name, store="memory"):
    with Emulator(store=store) as emulator:
        return TRACES[name](emulator)


def verify(name, store="memory"):
    """Return a unified diff of golden versus observed, or "" when identical.

    The existence check precedes `capture()` so that a missing golden costs
    nothing: running a full trace only to report "missing golden" wastes an
    emulator launch.

    In memory mode the comparison is byte-for-byte. In file mode the
    allow-listed interaction blocks are masked on both sides before an
    otherwise byte-exact compare, and any allow-list entry that did not
    actually diverge is reported as stale.
    """
    path = golden_path(name)
    if not os.path.exists(path):
        return "missing golden %s; run with --regenerate" % path
    with open(path, "r", encoding="utf-8") as handle:
        expected = handle.read()
    observed = serialize(capture(name, store))
    if store == "memory":
        if expected == observed:
            return ""
        expected_lines = expected.splitlines(True)
        diff = list(
            difflib.unified_diff(
                expected_lines,
                observed.splitlines(True),
                fromfile="golden/%s.json" % name,
                tofile="observed/%s.json" % name,
            )
        )
        return "".join(_annotate_hunks_with_labels(diff, expected_lines))
    allowlist = _load_allowlist()
    residual = diff_with_allowlist(expected, observed, allowlist)
    stale = stale_allowlist_labels(expected, observed, allowlist)
    if stale:
        residual += "\nstale allow-list entries (did not diverge): %r\n" % stale
    return residual


def _annotate_hunks_with_labels(diff, expected_lines):
    """Append the enclosing interaction's label to each hunk header.

    A changed field can sort many lines away from its own interaction's
    "label" field -- JSON keys are emitted alphabetically, and "label" is
    neither first nor last among them -- so a hunk showing only the changed
    field and a line number is not enough to tell a CI-log reader which
    interaction moved.

    The hunk header's own declared start line is *not* used to look up the
    interaction: `difflib.unified_diff` prepends up to three lines of
    unchanged context ahead of the first real change, so for a change near
    the top of a short interaction block, the header's start line can sit
    in the *previous* interaction's context instead. This walks the hunk
    body from the header's start, tracking the golden-side line number, and
    attributes to whichever golden line the first actually-changed
    ("-" or "+") line sits at.
    """
    labels_by_line = _labels_by_line(expected_lines)
    out = []
    hunk = None
    for line in diff:
        if _HUNK_HEADER.match(line):
            if hunk is not None:
                out.extend(_annotate_one_hunk(hunk, labels_by_line))
            hunk = [line]
        elif hunk is not None:
            hunk.append(line)
        else:
            out.append(line)
    if hunk is not None:
        out.extend(_annotate_one_hunk(hunk, labels_by_line))
    return out


def _annotate_one_hunk(hunk_lines, labels_by_line):
    """Annotate one hunk (header line, then its body lines) with a label."""
    header = hunk_lines[0]
    # 1-based golden-side line number the hunk's context starts at.
    cursor = int(_HUNK_HEADER.match(header).group("start"))
    label = None
    for line in hunk_lines[1:]:
        if _CONTEXT_LINE.match(line):
            cursor += 1
            continue
        # The first "-" (removed/changed) or "+" (added) line: a removal
        # sits exactly at golden line `cursor`; an insertion has no golden
        # line of its own, but belongs to whichever interaction is open at
        # `cursor`, the golden line it is inserted ahead of. Either way,
        # `cursor` -- not the header's start -- is the right lookup key, and
        # only the first such line matters: it already fixes the enclosing
        # interaction for the whole hunk.
        label = labels_by_line.get(cursor - 1)
        break
    if label is not None:
        header = header.rstrip("\n") + " interaction: %r\n" % label
    return [header] + hunk_lines[1:]


def _labels_by_line(lines):
    """Map each 0-based line index to its enclosing interaction's label.

    Scanning forward for the next "label" line from a hunk's start --
    rather than finding which interaction actually encloses it -- would
    misattribute a change in a field that sorts after "label" (`status`,
    `type`, `length`, `offsets`, `sha256`) to the *following* interaction
    instead of its own, since that interaction's own "label" line has
    already gone by. Every interaction has exactly one "label" field
    somewhere inside it, so this instead finds each interaction's line
    range first and assigns its one label to every line in that range.
    """
    labels = {}
    block_start = None
    block_label = None
    for index, line in enumerate(lines):
        if _INTERACTION_START.match(line):
            block_start = index
            block_label = None
            continue
        if block_start is None:
            continue
        found = _LABEL_LINE.match(line)
        if found:
            block_label = found.group("label")
        if _INTERACTION_END.match(line):
            for i in range(block_start, index + 1):
                labels[i] = block_label
            block_start = None
    return labels


# byte-exact masked overlay. `verify` uses these in file mode; the memory
# path keeps its existing `expected == observed`.

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
            me.splitlines(True),
            mo.splitlines(True),
            fromfile="golden (masked)",
            tofile="observed (masked)",
        )
    )


def _blocks(text):
    return {s[1]: s[2] for s in _segments(text) if s[0] == "int"}


def stale_allowlist_labels(expected, observed, allowlist):
    """Allow-listed labels that are PRESENT in this trace but whose block did
    NOT diverge -- a stale entry would silently absorb a future regression, so
    verify() fails on any.

    Presence matters because the allow-list is global across the rest/grpc/
    faults traces while any one divergence appears in only some of them. A
    label absent from THIS trace is Not-Applicable here, not stale: without the
    presence guard, `exp.get(l) == obs.get(l)` is `None == None` for an absent
    label, which would falsely flag e.g. a rest-only entry as stale in the
    faults trace and redden a trace that has no divergence at all."""
    exp, obs = _blocks(expected), _blocks(observed)
    present = set(exp) | set(obs)
    return sorted(l for l in allowlist if l in present and exp.get(l) == obs.get(l))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regenerate", action="store_true")
    parser.add_argument("--trace", action="append", choices=sorted(TRACES))
    parser.add_argument("--store", choices=("memory", "file"), default="memory")
    args = parser.parse_args(argv)
    names = args.trace or sorted(TRACES)

    if args.regenerate:
        os.makedirs(GOLDEN_DIR, exist_ok=True)
        for name in names:
            content = serialize(capture(name))
            with open(golden_path(name), "w", encoding="utf-8") as handle:
                handle.write(content)
            print("wrote", golden_path(name))
        return 0

    failed = False
    for name in names:
        diff = verify(name, args.store)
        if diff:
            failed = True
            print("FAIL %s\n%s" % (name, diff))
        else:
            print("OK   %s" % name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
