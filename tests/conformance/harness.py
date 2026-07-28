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
be justified in the commit message and, if intended, recorded in
allowlist.json. An unexplained golden diff is a defect, not a nuisance.
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
ALLOWLIST = os.path.join(_HERE, "allowlist.json")


def golden_path(name):
    return os.path.join(GOLDEN_DIR, name + ".json")


def serialize(record):
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


def capture(name):
    with Emulator() as emulator:
        return TRACES[name](emulator)


def load_allowlist():
    if not os.path.exists(ALLOWLIST):
        return {}
    with open(ALLOWLIST, "r", encoding="utf-8") as handle:
        return json.load(handle)


def verify(name):
    """Return a unified diff of golden versus observed, or "" when identical."""
    observed = serialize(capture(name))
    path = golden_path(name)
    if not os.path.exists(path):
        return "missing golden %s; run with --regenerate" % path
    with open(path, "r", encoding="utf-8") as handle:
        expected = handle.read()
    allowed = set(load_allowlist().get(name, {}).keys())
    if expected == observed:
        return ""
    diff = list(
        difflib.unified_diff(
            expected.splitlines(True),
            observed.splitlines(True),
            fromfile="golden/%s.json" % name,
            tofile="observed/%s.json" % name,
        )
    )
    if allowed:
        diff = _drop_allowed_labels(diff, allowed)
    return "".join(diff)


def _drop_allowed_labels(diff, allowed):
    """Drop hunks that touch only allow-listed interaction labels."""
    kept, hunk = [], []
    for line in diff + ["@@ sentinel @@\n"]:
        if line.startswith("@@"):
            if hunk and not _hunk_is_allowed(hunk, allowed):
                kept.extend(hunk)
            hunk = [line]
        elif hunk:
            hunk.append(line)
        else:
            kept.append(line)
    return kept


def _hunk_is_allowed(hunk, allowed):
    changed = [
        ln
        for ln in hunk
        if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
    ]
    if not changed:
        return True
    return all(any('"%s"' % label in ln for label in allowed) for ln in changed)


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
