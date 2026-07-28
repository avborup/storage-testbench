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

"""Unit tests for `harness.verify`'s comparison and diff logic.

This is the compare logic that IS the gate: if `verify()` ever returned ""
for a real difference, the conformance job would stay green forever. These
tests exercise it directly, against fixture records and a temporary golden
file, and deliberately never launch a real `Emulator` -- `capture()` is
monkeypatched out -- so they stay fast regardless of what `tests/
conformance/golden/` currently contains.
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests.conformance import harness


class TestVerify(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        self.golden_file = os.path.join(tmpdir, "fixture.json")
        patcher = mock.patch.object(
            harness, "golden_path", return_value=self.golden_file
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_golden(self, record):
        with open(self.golden_file, "w", encoding="utf-8") as handle:
            handle.write(harness.serialize(record))

    def test_identical_golden_produces_no_diff(self):
        record = {
            "name": "fixture",
            "interactions": [{"kind": "http", "label": "get-thing", "status": 200}],
        }
        self._write_golden(record)
        with mock.patch.object(harness, "capture", return_value=record):
            self.assertEqual("", harness.verify("fixture"))

    def test_perturbed_golden_names_the_changed_field_and_interaction(self):
        golden_record = {
            "name": "fixture",
            "interactions": [{"kind": "http", "label": "get-thing", "status": 200}],
        }
        observed_record = {
            "name": "fixture",
            "interactions": [{"kind": "http", "label": "get-thing", "status": 201}],
        }
        self._write_golden(golden_record)
        with mock.patch.object(harness, "capture", return_value=observed_record):
            diff = harness.verify("fixture")
        self.assertNotEqual("", diff)
        self.assertIn('"status": 200', diff)
        self.assertIn('"status": 201', diff)
        self.assertIn("interaction: 'get-thing'", diff)

    def test_missing_golden_reports_regenerate_without_capturing(self):
        # self.golden_file is never written, so it does not exist.
        with mock.patch.object(
            harness, "capture", side_effect=AssertionError("capture must not run")
        ):
            diff = harness.verify("fixture")
        self.assertIn("missing golden", diff)
        self.assertIn("--regenerate", diff)


class TestHunkNamesTheCorrectInteraction(unittest.TestCase):
    """Each hunk's label annotation names the interaction that actually
    changed -- exercised through the real diff path (`verify()`, which
    drives `difflib.unified_diff` and then `_annotate_hunks_with_labels`),
    not by calling `_labels_by_line` at the exact changed-line index
    directly. That shortcut would bypass unified diff's own context window
    entirely, and would not have caught the bug this class guards: unified
    diff prepends up to three lines of unchanged context ahead of the first
    real change, so the hunk header's *declared* start line can sit inside
    the *previous* interaction's trailing context rather than the block
    that actually changed. Naming the wrong interaction is worse than
    naming none -- a reader trusts the annotation and looks in the wrong
    place -- so each case below asserts the exact label, not just that some
    label was printed.
    """

    def setUp(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        self.golden_file = os.path.join(tmpdir, "fixture.json")
        patcher = mock.patch.object(
            harness, "golden_path", return_value=self.golden_file
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _diff(self, golden_interactions, observed_interactions):
        golden = {"name": "fixture", "interactions": golden_interactions}
        observed = {"name": "fixture", "interactions": observed_interactions}
        with open(self.golden_file, "w", encoding="utf-8") as handle:
            handle.write(harness.serialize(golden))
        with mock.patch.object(harness, "capture", return_value=observed):
            return harness.verify("fixture")

    def test_field_before_label_near_top_of_a_short_block(self):
        # "body" sorts before "kind" and "label", so it is the very first
        # field in "second"'s block -- only "second"'s own opening "{"
        # precedes it, nowhere near 3 lines of same-block context. This is
        # the case that a header-start-based lookup gets wrong: unified
        # diff's leading context reaches back across that "{" into
        # "first"'s trailing lines, and a lookup keyed on the header's
        # start line finds "first" instead of "second".
        golden = [
            {"kind": "http", "label": "first", "status": 200},
            {"kind": "http", "label": "second", "body": "orig"},
        ]
        observed = [
            {"kind": "http", "label": "first", "status": 200},
            {"kind": "http", "label": "second", "body": "changed"},
        ]
        diff = self._diff(golden, observed)
        self.assertIn("interaction: 'second'", diff)
        self.assertNotIn("interaction: 'first'", diff)

    def test_field_before_label_in_the_middle_of_a_block(self):
        # Padded with "aaa"/"bbb" ahead of the changed field "ccc" (all
        # sorting before "kind"/"label") so the full 3-line leading context
        # stays inside "second"'s own block rather than reaching "first".
        golden = [
            {"kind": "http", "label": "first", "status": 200},
            {
                "aaa": "x",
                "bbb": "y",
                "ccc": "orig",
                "ddd": "z",
                "kind": "http",
                "label": "second",
            },
        ]
        observed = [
            {"kind": "http", "label": "first", "status": 200},
            {
                "aaa": "x",
                "bbb": "y",
                "ccc": "changed",
                "ddd": "z",
                "kind": "http",
                "label": "second",
            },
        ]
        diff = self._diff(golden, observed)
        self.assertIn("interaction: 'second'", diff)
        self.assertNotIn("interaction: 'first'", diff)

    def test_field_after_label(self):
        # "status" sorts after "label"; this is the case the previous
        # (line-range) approach already got right and must stay right.
        golden = [
            {"kind": "http", "label": "first", "status": 200},
            {"kind": "http", "label": "second", "status": 200},
        ]
        observed = [
            {"kind": "http", "label": "first", "status": 200},
            {"kind": "http", "label": "second", "status": 201},
        ]
        diff = self._diff(golden, observed)
        self.assertIn("interaction: 'second'", diff)
        self.assertNotIn("interaction: 'first'", diff)

    def test_first_interaction_in_the_file_has_no_preceding_block_to_bleed_into(
        self,
    ):
        # A change near the top of the *first* interaction has nothing
        # before it but the top-level "{" and the "interactions": [ line --
        # neither belongs to any interaction, so this also checks that the
        # walk does not fall over when there is no previous block at all.
        golden = [
            {"kind": "http", "label": "first", "body": "orig"},
            {"kind": "http", "label": "second", "status": 200},
        ]
        observed = [
            {"kind": "http", "label": "first", "body": "changed"},
            {"kind": "http", "label": "second", "status": 200},
        ]
        diff = self._diff(golden, observed)
        self.assertIn("interaction: 'first'", diff)
        self.assertNotIn("interaction: 'second'", diff)


if __name__ == "__main__":
    unittest.main()
