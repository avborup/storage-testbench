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


class TestAnnotateHunksWithLabels(unittest.TestCase):
    """The label-attribution logic in isolation, over hand-built lines.

    Interaction dicts are serialized with keys in alphabetical order, so
    "label" is neither the first nor the last field: a change to a field
    that sorts *before* "label" (e.g. "body") and one that sorts *after* it
    (e.g. "status") must both resolve to the same enclosing interaction, not
    whichever interaction's "label" line happens to come next.
    """

    def _lines(self, *records):
        combined = {"name": "fixture", "interactions": list(records)}
        return harness.serialize(combined).splitlines(True)

    def test_field_sorting_before_label_resolves_to_its_own_interaction(self):
        lines = self._lines(
            {"kind": "http", "label": "first", "body": {"x": 1}},
            {"kind": "http", "label": "second", "body": {"x": 2}},
        )
        body_line = next(i for i, l in enumerate(lines) if '"x": 1' in l)
        labels = harness._labels_by_line(lines)
        self.assertEqual("first", labels[body_line])

    def test_field_sorting_after_label_resolves_to_its_own_interaction(self):
        lines = self._lines(
            {"kind": "http", "label": "first", "status": 200},
            {"kind": "http", "label": "second", "status": 200},
        )
        status_line = next(i for i, l in enumerate(lines) if '"status": 200' in l)
        labels = harness._labels_by_line(lines)
        self.assertEqual("first", labels[status_line])


if __name__ == "__main__":
    unittest.main()
