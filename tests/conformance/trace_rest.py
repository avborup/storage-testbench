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

"""JSON and XML API conformance trace.

Every interaction is recorded under a stable label. Labels are the diff's
vocabulary, so they must describe the operation rather than its ordinal:
`get-object-after-patch`, not `step-14`.
"""

import gzip
import json

import requests
from requests_toolbelt import MultipartEncoder

from tests.conformance.recorder import Recorder

BUCKET = "conformance-bucket"
VERSIONED = "conformance-versioned"
PAYLOAD = b"The quick brown fox jumps over the lazy dog"

# Named, rather than "simple.txt"/"multipart.txt"/"xml.txt"/"resumable.txt", so
# that their alphabetical order -- the order `list-objects` returns them in,
# since GCS lists by name (testbench/database.py's `list_object` sorts on
# `item.name`) -- matches the order they are created in. The recorder's
# generation-monotonicity invariant tracks every generation value seen across
# the whole trace, not per-object, so a single `list-objects` response whose
# name order differs from creation order trips it; see the trace-5 report for
# the full account.
SIMPLE = "01-simple.txt"
MULTIPART = "02-multipart.txt"
XML = "03-xml.txt"
RESUMABLE = "04-resumable.txt"


def run(emulator):
    rec = Recorder("rest")
    base = emulator.rest_url
    session = requests.Session()

    def api(label, method, path, **kwargs):
        response = session.request(method, base + path, timeout=30, **kwargs)
        rec.record_http(label, response)
        return response

    # --- buckets -----------------------------------------------------------
    api(
        "create-bucket",
        "POST",
        "/storage/v1/b",
        params={"project": "test-project"},
        json={"name": BUCKET},
    )
    api(
        "create-bucket-duplicate",
        "POST",
        "/storage/v1/b",
        params={"project": "test-project"},
        json={"name": BUCKET},
    )
    api("get-bucket", "GET", "/storage/v1/b/" + BUCKET)
    api("list-buckets", "GET", "/storage/v1/b", params={"project": "test-project"})
    api(
        "patch-bucket-labels",
        "PATCH",
        "/storage/v1/b/" + BUCKET,
        json={"labels": {"env": "conformance"}},
    )
    api("get-bucket-iam", "GET", "/storage/v1/b/%s/iam" % BUCKET)
    api(
        "test-bucket-iam-permissions",
        "GET",
        "/storage/v1/b/%s/iam/testPermissions" % BUCKET,
        params={"permissions": "storage.objects.get"},
    )
    api(
        "create-versioned-bucket",
        "POST",
        "/storage/v1/b",
        params={"project": "test-project"},
        json={"name": VERSIONED, "versioning": {"enabled": True}},
    )

    # --- simple, multipart and XML uploads ---------------------------------
    api(
        "upload-simple",
        "POST",
        "/upload/storage/v1/b/%s/o" % BUCKET,
        params={"uploadType": "media", "name": SIMPLE},
        data=PAYLOAD,
        headers={"Content-Type": "text/plain"},
    )
    # A multipart upload's body must be `multipart/related`, per GCS's JSON
    # API; `requests`' own `files=` kwarg builds `multipart/form-data`
    # instead, which the emulator rejects with 400 "Content-type header in
    # multipart upload is invalid" (testbench/common.py's `parse_multipart`
    # checks for the `multipart/related` prefix explicitly). `MultipartEncoder`
    # is already a pinned emulator dependency (`requests-toolbelt` in
    # setup.py), so building the body this way adds nothing new.
    multipart_encoder = MultipartEncoder(
        fields={
            "metadata": (None, json.dumps({"name": MULTIPART}), "application/json"),
            "media": (MULTIPART, PAYLOAD, "text/plain"),
        }
    )
    api(
        "upload-multipart",
        "POST",
        "/upload/storage/v1/b/%s/o" % BUCKET,
        params={"uploadType": "multipart"},
        data=multipart_encoder,
        headers={
            # `.content_type` (not `.boundary`, which already carries the
            # leading "--" used to delimit body parts) is what must appear in
            # the header; reconstructing it from `.boundary` would double the
            # dashes and make the header's boundary not match the body's.
            "Content-Type": multipart_encoder.content_type.replace(
                "multipart/form-data", "multipart/related"
            )
        },
    )
    api(
        "upload-xml",
        "PUT",
        "/%s/%s" % (BUCKET, XML),
        data=PAYLOAD,
        headers={"Content-Type": "text/plain"},
    )
    # Read back right away, not deferred into the "reads" section below: an
    # XML PUT's own response carries no generation (empty body, no
    # `x-goog-generation` header -- see `xml_put_object` in
    # testbench/rest_server.py), so this GET is XML's object's *first*
    # exposure of its generation to the canonicalizer. Recording it here
    # keeps first-sighting order matching creation order, which the
    # generation-monotonicity invariant in `finish()` requires; deferring it
    # until after the resumable upload below would expose a chronologically
    # older generation after a newer one and trip that invariant. Interaction
    # order does not affect what Task 6 diffs -- labels are the golden's
    # keys -- so this only changes *when* the read happens, not what it
    # tests.
    api("download-xml", "GET", "/%s/%s" % (BUCKET, XML))

    # --- resumable upload --------------------------------------------------
    start = api(
        "start-resumable",
        "POST",
        "/upload/storage/v1/b/%s/o" % BUCKET,
        params={"uploadType": "resumable", "name": RESUMABLE},
        json={"name": RESUMABLE},
    )
    location = start.headers["Location"]
    upload_path = location[len(base) :] if location.startswith(base) else location
    api(
        "resumable-chunk-1",
        "PUT",
        upload_path,
        data=PAYLOAD[:20],
        headers={"Content-Range": "bytes 0-19/%d" % len(PAYLOAD)},
    )
    api(
        "resumable-query-status",
        "PUT",
        upload_path,
        headers={"Content-Range": "bytes */%d" % len(PAYLOAD)},
    )
    api(
        "resumable-chunk-2",
        "PUT",
        upload_path,
        data=PAYLOAD[20:],
        headers={"Content-Range": "bytes 20-%d/%d" % (len(PAYLOAD) - 1, len(PAYLOAD))},
    )

    # --- reads -------------------------------------------------------------
    api("get-object-metadata", "GET", "/storage/v1/b/%s/o/%s" % (BUCKET, SIMPLE))
    api(
        "download-full",
        "GET",
        "/storage/v1/b/%s/o/%s" % (BUCKET, SIMPLE),
        params={"alt": "media"},
    )
    api(
        "download-range-middle",
        "GET",
        "/storage/v1/b/%s/o/%s" % (BUCKET, SIMPLE),
        params={"alt": "media"},
        headers={"Range": "bytes=10-19"},
    )
    api(
        "download-range-open-ended",
        "GET",
        "/storage/v1/b/%s/o/%s" % (BUCKET, SIMPLE),
        params={"alt": "media"},
        headers={"Range": "bytes=10-"},
    )
    api(
        "download-range-suffix",
        "GET",
        "/storage/v1/b/%s/o/%s" % (BUCKET, SIMPLE),
        params={"alt": "media"},
        headers={"Range": "bytes=-10"},
    )
    api(
        "download-range-unsatisfiable",
        "GET",
        "/storage/v1/b/%s/o/%s" % (BUCKET, SIMPLE),
        params={"alt": "media"},
        headers={"Range": "bytes=9999-10000"},
    )
    api("list-objects", "GET", "/storage/v1/b/%s/o" % BUCKET)
    api(
        "list-objects-with-delimiter",
        "GET",
        "/storage/v1/b/%s/o" % BUCKET,
        params={"delimiter": "/"},
    )

    # --- decompressive transcoding ----------------------------------------
    api(
        "upload-gzipped",
        "POST",
        "/upload/storage/v1/b/%s/o" % BUCKET,
        params={"uploadType": "media", "name": "gz.txt", "contentEncoding": "gzip"},
        # `mtime=0` pins the gzip header's embedded timestamp. Without it,
        # `gzip.compress` defaults to the current wall-clock time, so the
        # *compressed* bytes -- and therefore the stored object's crc32c and
        # md5Hash -- differ on every run even though PAYLOAD never changes.
        # Found by running trace_rest twice and diffing.
        data=gzip.compress(PAYLOAD, mtime=0),
        headers={"Content-Type": "text/plain"},
    )
    api(
        "download-transcoded",
        "GET",
        "/storage/v1/b/%s/o/gz.txt" % BUCKET,
        params={"alt": "media"},
        headers={"Accept-Encoding": "identity"},
    )
    api(
        "download-not-transcoded",
        "GET",
        "/storage/v1/b/%s/o/gz.txt" % BUCKET,
        params={"alt": "media"},
        headers={"Accept-Encoding": "gzip"},
    )

    # --- metadata mutation, ACLs, preconditions ---------------------------
    api(
        "patch-object",
        "PATCH",
        "/storage/v1/b/%s/o/%s" % (BUCKET, SIMPLE),
        json={"metadata": {"colour": "blue"}},
    )
    api(
        "update-object",
        "PUT",
        "/storage/v1/b/%s/o/%s" % (BUCKET, SIMPLE),
        json={"contentType": "text/plain", "metadata": {"colour": "green"}},
    )
    api("list-object-acl", "GET", "/storage/v1/b/%s/o/%s/acl" % (BUCKET, SIMPLE))
    api(
        "insert-object-acl",
        "POST",
        "/storage/v1/b/%s/o/%s/acl" % (BUCKET, SIMPLE),
        json={"entity": "allUsers", "role": "READER"},
    )
    api(
        "precondition-generation-mismatch",
        "GET",
        "/storage/v1/b/%s/o/%s" % (BUCKET, SIMPLE),
        params={"ifGenerationMatch": "1"},
    )
    api("get-missing-object", "GET", "/storage/v1/b/%s/o/absent.txt" % BUCKET)
    api("get-missing-bucket", "GET", "/storage/v1/b/absent-bucket")

    # --- CSEK --------------------------------------------------------------
    # Fixed key material so the trace is deterministic. Not a secret.
    key_b64 = "aVhrOWVYVmJEd0hVeDJEZzZKNWJUOEE3T3lVenBudUdxWk9CVFVvS0NnTT0="
    sha_b64 = "Nzc0MDU5RTUxNDIzQzQ0NEUzOEEwRDZBRDlBQzQzMTA5NTdCNkE2ODU3RkY4RUZE"
    csek = {
        "x-goog-encryption-algorithm": "AES256",
        "x-goog-encryption-key": key_b64,
        "x-goog-encryption-key-sha256": sha_b64,
    }
    api(
        "upload-csek",
        "POST",
        "/upload/storage/v1/b/%s/o" % BUCKET,
        params={"uploadType": "media", "name": "csek.txt"},
        data=PAYLOAD,
        headers=dict(csek, **{"Content-Type": "text/plain"}),
    )
    api(
        "download-csek",
        "GET",
        "/storage/v1/b/%s/o/csek.txt" % BUCKET,
        params={"alt": "media"},
        headers=csek,
    )
    api(
        "download-csek-without-key",
        "GET",
        "/storage/v1/b/%s/o/csek.txt" % BUCKET,
        params={"alt": "media"},
    )

    # --- compose, rewrite, move -------------------------------------------
    api(
        "compose",
        "POST",
        "/storage/v1/b/%s/o/composed.txt/compose" % BUCKET,
        json={
            "sourceObjects": [{"name": SIMPLE}, {"name": MULTIPART}],
            "destination": {"contentType": "text/plain"},
        },
    )
    api(
        "rewrite-single-call",
        "POST",
        "/storage/v1/b/%s/o/%s/rewriteTo/b/%s/o/rewritten.txt"
        % (BUCKET, SIMPLE, BUCKET),
    )
    api(
        "move-object",
        "POST",
        "/storage/v1/b/%s/o/rewritten.txt/moveTo/o/moved.txt" % BUCKET,
    )

    # --- versioning, delete, soft delete ----------------------------------
    api(
        "versioned-upload-v1",
        "POST",
        "/upload/storage/v1/b/%s/o" % VERSIONED,
        params={"uploadType": "media", "name": "v.txt"},
        data=b"v1",
        headers={"Content-Type": "text/plain"},
    )
    api(
        "versioned-upload-v2",
        "POST",
        "/upload/storage/v1/b/%s/o" % VERSIONED,
        params={"uploadType": "media", "name": "v.txt"},
        data=b"v2",
        headers={"Content-Type": "text/plain"},
    )
    api(
        "versioned-list-all",
        "GET",
        "/storage/v1/b/%s/o" % VERSIONED,
        params={"versions": "true"},
    )
    api("delete-object", "DELETE", "/storage/v1/b/%s/o/moved.txt" % BUCKET)
    api("delete-missing-object", "DELETE", "/storage/v1/b/%s/o/moved.txt" % BUCKET)
    api("delete-non-empty-bucket", "DELETE", "/storage/v1/b/" + BUCKET)

    return rec.finish()
