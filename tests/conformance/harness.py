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


def _annotate_hunks_with_labels(diff, expected_lines):
    """Append the enclosing interaction's label to each hunk header.

    A changed field can sort many lines away from its own interaction's
    "label" field -- JSON keys are emitted alphabetically, and "label" is
    neither first nor last among them -- so a hunk showing only the changed
    field and a line number is not enough to tell a CI-log reader which
    interaction moved.
    """
    labels_by_line = _labels_by_line(expected_lines)
    out = []
    for line in diff:
        match = _HUNK_HEADER.match(line)
        if match:
            # Unified diff hunk headers are 1-based; `labels_by_line` is
            # keyed by 0-based index into `expected_lines`.
            label = labels_by_line.get(int(match.group("start")) - 1)
            if label is not None:
                line = line.rstrip("\n") + " interaction: %r\n" % label
        out.append(line)
    return out


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regenerate", action="store_true")
    parser.add_argument("--trace", action="append", choices=sorted(TRACES))
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
        diff = verify(name)
        if diff:
            failed = True
            print("FAIL %s\n%s" % (name, diff))
        else:
            print("OK   %s" % name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
