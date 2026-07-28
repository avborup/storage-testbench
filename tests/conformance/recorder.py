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

"""Append-only record of canonicalized API interactions."""

import hashlib
import json

import requests
from google.protobuf import json_format

from tests.conformance.canonicalize import Canonicalizer

_JSON_CONTENT_TYPES = ("application/json", "text/json")


class Recorder:
    """Accumulates canonicalized interactions for one trace.

    Bodies that are JSON are recorded structurally so that a diff points at a
    field. Bodies that are not JSON are recorded as a length and a SHA-256, so
    that media payloads are verified byte-exactly without committing them to
    git.
    """

    def __init__(self, name):
        self.name = name
        self._canon = Canonicalizer()
        self._interactions = []
        self._labels = set()

    def record_http(self, label, response):
        self._claim(label)
        content_type = ""
        for key, value in response.headers.items():
            if key.lower() == "content-type":
                content_type = value
                break
        self._interactions.append(
            {
                "label": label,
                "kind": "http",
                "status": response.status_code,
                "headers": self._canon.headers(response.headers),
                "body": self._body(response.content, content_type),
            }
        )

    def record_grpc(self, label, message):
        self._claim(label)
        # `always_print_fields_with_no_presence` is the real keyword in the
        # pinned protobuf==5.29.3: `MessageToDict` does not accept
        # `including_default_value_fields` in this version at all (it raises
        # TypeError), despite that being the name an earlier protobuf used
        # for the same option. If a future protobuf renames this again, the
        # harness must be updated in lockstep with the pin, and the golden
        # files regenerated -- record that as an allow-list entry with the
        # protobuf version as justification.
        as_dict = json_format.MessageToDict(
            message,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        )
        self._interactions.append(
            {
                "label": label,
                "kind": "grpc",
                "type": message.DESCRIPTOR.full_name,
                "body": self._canon.body(as_dict),
            }
        )

    def record_stream(self, label, chunks):
        """Record a streamed payload as boundaries plus a digest.

        Chunk boundaries are externally visible to a client, and the Media
        seam is exactly what could change them, so they belong in the golden
        alongside the bytes.
        """
        self._claim(label)
        offsets, offset = [], 0
        digest = hashlib.sha256()
        for chunk in chunks:
            offsets.append(offset)
            offset += len(chunk)
            digest.update(chunk)
        self._interactions.append(
            {
                "label": label,
                "kind": "stream",
                "offsets": offsets,
                "length": offset,
                "sha256": digest.hexdigest(),
            }
        )

    def record_error(self, label, exception):
        """Record a failure as data, so error taxonomies are diffed too.

        Transport-level `requests` failures collapse to a single token. Which
        urllib3 subclass surfaces for a broken stream is a property of the
        client OS and socket timing, not of the emulator: macOS reports
        `ReadTimeout` where Linux reports `ConnectionError` for the same
        injected fault. Recording the subclass would make the goldens valid
        only on the machine that captured them, so the CI conformance job
        would fail on a clean checkout. The interaction's label still says
        which instruction was injected, so "this fault broke the transfer"
        stays distinguishable from "this fault returned an HTTP status".
        gRPC errors are not normalized -- their status codes are chosen by
        the emulator and are therefore deterministic.
        """
        self._claim(label)
        if isinstance(exception, requests.exceptions.RequestException):
            kind_name = "<TRANSPORT_ERROR>"
        else:
            kind_name = type(exception).__name__
        entry = {"label": label, "kind": "error", "type": kind_name}
        code = getattr(exception, "code", None)
        if callable(code):
            entry["grpc_code"] = code().name
        details = getattr(exception, "details", None)
        if callable(details):
            entry["has_details"] = bool(details())
        self._interactions.append(entry)

    def finish(self):
        self._canon.assert_invariants()
        return {"name": self.name, "interactions": self._interactions}

    def _claim(self, label):
        assert label not in self._labels, "duplicate interaction label %r" % (label,)
        self._labels.add(label)

    def _body(self, content, content_type):
        if not content:
            return None
        if any(content_type.startswith(ct) for ct in _JSON_CONTENT_TYPES):
            return self._canon.body(json.loads(content))
        return {
            "length": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
