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
