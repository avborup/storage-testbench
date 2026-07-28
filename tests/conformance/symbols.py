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
