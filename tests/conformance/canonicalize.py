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

_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)
_ORIGIN = re.compile(r"^[a-z][a-z0-9+.-]*://[^/]+")


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
            return self._walk_dict(node)
        if isinstance(node, list):
            return [self._walk(v) for v in node]
        if isinstance(node, str):
            return self._substitute_known_values(node)
        return node

    def _walk_dict(self, node):
        # Two passes, not one: bind every directly-replaceable field first,
        # then substitute into the rest. A single pass in iteration order
        # would miss a value embedded in a string field (e.g. mediaLink)
        # that appears before the field that binds it (e.g. generation) in
        # the same object; binding first makes the result independent of
        # field order. LINK-kind fields are composite URLs, not opaque
        # tokens, so they are never bound directly as a whole value --
        # instead they are canonicalized structurally in the second pass
        # (origin erased, remainder substituted) so an embedded generation
        # still canonicalizes to the same placeholder as the sibling
        # `generation` field.
        bound = {}
        for key, value in node.items():
            kind = NONDETERMINISTIC_FIELDS.get(key)
            if kind is not None and kind != "LINK" and isinstance(value, (str, int)):
                bound[key] = self._bind(kind, value)
        result = {}
        for key, value in node.items():
            if key in bound:
                result[key] = bound[key]
            elif NONDETERMINISTIC_FIELDS.get(key) == "LINK" and isinstance(value, str):
                result[key] = self._canonical_link(value)
            else:
                result[key] = self._walk(value)
        return result

    def _bind(self, kind, value):
        if kind == "GEN":
            self._generations.append(int(value))
        if kind == "TIME" and isinstance(value, str):
            self._timestamps.append(value)
        return self._symbols.bind(kind, value)

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
