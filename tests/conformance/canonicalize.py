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
    # `Bucket.SoftDeletePolicy.effective_time` and
    # `Bucket.RetentionPolicy.effective_time` (gcs/bucket.py sets both from
    # `datetime.now()` at creation/update time); found by running trace_grpc
    # twice and diffing -- see the trace-5 report.
    "effectiveTime": "TIME",
    "effective_time": "TIME",
    "uploadId": "UPLOAD",
    "upload_id": "UPLOAD",
    "rewriteToken": "REWRITE",
    "rewrite_token": "REWRITE",
    "id": "ID",
    # LINK fields are composite URLs, not opaque tokens: only their volatile
    # origin (scheme + host + ephemeral port) is bound as a whole value; the
    # path and query are kept and have already-bound values substituted in.
    "selfLink": "LINK",
    "mediaLink": "LINK",
}

# Query parameter names, as they appear inside a LINK's query string, that
# carry non-deterministic values needing their own direct binding. A link's
# other placeholders (e.g. a `generation` embedded in `selfLink`) rely on the
# *same* value already having been bound from an ordinary same-named JSON
# field elsewhere in the same interaction -- see `_substitute_known_values`.
# But a resumable upload session's `Location` header
# (`.../o?uploadType=resumable&upload_id=<hex>`) is the *only* place its
# upload_id ever appears; nothing else in the interaction binds it first.
# Found by running trace_rest twice and diffing -- see the trace-5 report.
LINK_QUERY_FIELDS = {
    "upload_id": "UPLOAD",
}
_LINK_QUERY_PARAM = re.compile(
    r"(?P<key>%s)=(?P<value>[^&]+)" % "|".join(LINK_QUERY_FIELDS)
)

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
    # A resumable upload session's URI, returned only on `start-resumable`.
    # Like `selfLink`/`mediaLink`, it is a composite URL, not an opaque
    # token: its origin carries the ephemeral port (leaking between runs
    # exactly as an un-erased `selfLink` would), and its query string
    # carries the upload_id (see `LINK_QUERY_FIELDS` above). Found by running
    # trace_rest twice and diffing -- see the trace-5 report.
    "location": "LINK",
}

# A `google.iam.v1.Policy`'s `etag` (`Bucket.__iam_etag` in gcs/bucket.py
# returns `uuid.uuid4().hex`) is genuinely random and unrelated to a bucket's
# or object's own `etag` (an MD5 digest of content/metageneration, which is
# deterministic and worth keeping visible to a diff) -- despite the two
# sharing the same JSON/proto field name, "etag". A blanket rule on "etag"
# would erase the useful, deterministic signal too, so this is scoped to
# dicts shaped like an IAM policy: `etag` alongside either `bindings`,
# `version` (the gRPC `Policy` message always sets it), or `kind ==
# "storage#policy"` (the REST rendering). `bindings` alone is not a reliable
# *requirement* -- `testbench/rest_server.py` renders a REST policy through
# `json_format.MessageToDict` at default options, which omits an empty
# repeated field entirely, so a policy with zero bindings has no "bindings"
# key at all and would otherwise read as not-a-policy, leaving
# `__iam_etag()`'s raw random value in the golden every run. Not reachable
# by the traces today (no `setIamPolicy` call, and the default policy always
# has three bindings), but confirmed with a direct `Policy(bindings=[])` ->
# `MessageToDict` probe. The gRPC side cannot have this gap:
# `always_print_fields_with_no_presence=True` always emits `"bindings": []`.
# Found by running trace_rest and trace_grpc twice and diffing -- see the
# trace-5 report.
def _looks_like_iam_policy(node):
    return (
        isinstance(node, dict)
        and "etag" in node
        and (
            "bindings" in node
            or node.get("kind") == "storage#policy"
            or "version" in node
        )
    )


# testbench/error.py's JSON error envelope: `{"error": {"code": ..., "message":
# ...}}`. Unlike an object's or bucket's structured fields, `message` is
# free-form diagnostic text that interpolates raw internal state -- e.g.
# `"ifGenerationMatch validation failed. Expected = 1 vs Actual
# = 1785234485144."`, or a non-empty-bucket deletion error listing every
# object's raw generation. That state is already bound elsewhere in the same
# trace (from the very call that created it), so, like a link or a header,
# `message` gets already-bound values substituted -- never a blanket rewrite,
# and never anything not already known to be non-deterministic. Found by
# running trace_rest twice and diffing -- see the trace-5 report.
def _looks_like_error_envelope(node):
    return (
        isinstance(node, dict)
        and "code" in node
        and isinstance(node.get("message"), str)
    )


_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)
_ORIGIN = re.compile(r"^[a-z][a-z0-9+.-]*://[^/]+", re.IGNORECASE)


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
        # Two whole-tree passes. Binding must complete before emitting,
        # because a link can only have an embedded value substituted once
        # that value is bound, and the two may sit at different depths.
        self._bind_pass(obj)
        return self._emit(obj)

    def headers(self, mapping):
        out = {}
        for raw_name, value in mapping.items():
            name = raw_name.lower()
            if name in DROPPED_HEADERS:
                continue
            kind = HEADER_FIELDS.get(name)
            if kind == "LINK":
                out[name] = self._canonical_link(str(value))
            elif kind is not None:
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
                "saw %d then %d. A real regression aside, this can also mean "
                "a trace object's name sorts out of creation order: GCS lists "
                "objects by name (Database.list_object), and a listing is "
                "only silent about this if every object it contains was "
                "already first-sighted earlier by some other interaction -- "
                "see the naming-convention comments in trace_rest.py/"
                "trace_grpc.py and the trace-5 report." % (earlier, later)
            )
        for value in self._timestamps:
            assert _RFC3339.match(value), "not an RFC 3339 timestamp: %r" % (value,)

    def _bind_pass(self, node):
        """Bind every directly-replaceable field, anywhere in the tree.

        This must finish before `_emit` runs: a link at one depth can embed
        a value (e.g. a generation) bound from a field at a different depth,
        and a single combined walk cannot guarantee that field is bound
        before the link that references it is emitted.
        """
        if isinstance(node, dict):
            is_iam_policy = _looks_like_iam_policy(node)
            for key, value in node.items():
                if key == "etag" and is_iam_policy and isinstance(value, str):
                    self._bind("ETAG", value)
                    continue
                kind = NONDETERMINISTIC_FIELDS.get(key)
                if (
                    kind is not None
                    and kind != "LINK"
                    and isinstance(value, (str, int))
                ):
                    self._bind(kind, value)
                    continue
                self._bind_pass(value)
        elif isinstance(node, list):
            for value in node:
                self._bind_pass(value)

    def _emit(self, node):
        if isinstance(node, dict):
            out = {}
            is_iam_policy = _looks_like_iam_policy(node)
            is_error_envelope = _looks_like_error_envelope(node)
            for key, value in node.items():
                if key == "etag" and is_iam_policy and isinstance(value, str):
                    out[key] = self._symbols.bind("ETAG", value)
                    continue
                if key == "message" and is_error_envelope:
                    out[key] = self._substitute_known_values(value)
                    continue
                kind = NONDETERMINISTIC_FIELDS.get(key)
                if kind == "LINK" and isinstance(value, str):
                    out[key] = self._canonical_link(value)
                elif kind is not None and isinstance(value, (str, int)):
                    # Already bound in the first pass; bind() is idempotent,
                    # and going through the symbol table directly avoids
                    # recording the value twice for the invariant checks.
                    out[key] = self._symbols.bind(kind, value)
                else:
                    out[key] = self._emit(value)
            return out
        if isinstance(node, list):
            return [self._emit(value) for value in node]
        # Strings are left alone. Only links and headers are composite;
        # substituting into arbitrary body strings risks corrupting a
        # string that merely contains a bound value as a substring.
        return node

    def _bind(self, kind, value):
        # Only a value's first sighting is evidence about the server's
        # counter; every later reference to the *same* already-bound value
        # (re-fetching an object created earlier, after something newer has
        # since been created) is not a new data point; recording it again
        # would compare it against whatever unrelated value happened to be
        # bound most recently and fail the ordering check below for a
        # decrease that never actually happened, which is exactly why the
        # message below says "order first seen" rather than "order seen".
        is_new = (kind, str(value)) not in self._symbols.bindings()
        if is_new and kind == "GEN":
            self._generations.append(int(value))
        if is_new and kind == "TIME" and isinstance(value, str):
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
            return self._bind_link_query_params(self._substitute_known_values(text))
        origin = self._symbols.bind("ORIGIN", match.group(0))
        remainder = self._substitute_known_values(text[match.end() :])
        return origin + self._bind_link_query_params(remainder)

    def _bind_link_query_params(self, text):
        """Bind query parameters that only ever appear inside a link.

        `_substitute_known_values` only replaces a value that was already
        bound from some *other*, same-named field elsewhere in the same
        interaction (e.g. a `generation` embedded in `selfLink`, alongside an
        ordinary `generation` field). `LINK_QUERY_FIELDS` covers the
        parameters that have no such sibling field to bind from first.
        """

        def replace(match):
            kind = LINK_QUERY_FIELDS[match.group("key")]
            return "%s=%s" % (
                match.group("key"),
                self._bind(kind, match.group("value")),
            )

        return _LINK_QUERY_PARAM.sub(replace, text)

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
