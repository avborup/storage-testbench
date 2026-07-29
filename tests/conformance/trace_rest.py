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
TO_DELETE = "conformance-bucket-to-delete"
PAYLOAD = b"The quick brown fox jumps over the lazy dog"

# Literal rather than gzip.compress(...): Python <=3.10 writes OS=0xff in the
# gzip header while 3.11+ delegates to zlib, which writes its build-time
# OS_CODE (0x13 on Darwin, 0x03 on Linux). That one byte changes the stored
# object's crc32c/md5Hash and the wire sha256, so a runtime-compressed
# payload makes the goldens valid only on the machine that captured them.
GZIPPED_PAYLOAD = bytes.fromhex(
    "1f8b08000000000002ff0bc94855282ccd4cce56482aca2fcf5348cbaf50c8"
    "2acd2d2856c82f4b2d5228014ae72456552aa4e4a7030039a34f412b000000"
)
assert gzip.decompress(GZIPPED_PAYLOAD) == PAYLOAD

# Named, rather than "simple.txt"/"multipart.txt"/"xml.txt"/"resumable.txt", so
# that their alphabetical order -- the order `list-objects` returns them in,
# since GCS lists by name (testbench/database.py's `list_object` sorts on
# `item.name`) -- matches the order they are created in. The recorder's
# generation-monotonicity invariant (`canonicalize.py`'s `_bind`) only
# compares a value's *first* sighting anywhere in the trace to whatever was
# first-sighted immediately before it; a listing that merely re-reports
# already-bound generations appends nothing and stays silent. It only fires
# if a listing (or any other interaction) is itself the *first* place an
# out-of-order generation is exposed -- `download-xml`'s placement right
# after `upload-xml` below is the concrete example of avoiding exactly that.
# This naming keeps the trace robust against that same failure mode showing
# up in a listing instead, without depending on exactly which interaction
# happens to expose a generation first. See the trace-5 report for the full
# account.
SIMPLE = "01-simple.txt"
MULTIPART = "02-multipart.txt"
XML = "03-xml.txt"
RESUMABLE = "04-resumable.txt"
NESTED = "05-dir/nested.txt"


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
    # The golden's "upload-xml" response carries `x-goog-hash: "None"` -- a
    # real emulator bug, not a recorder artifact: `xml_put_object` in
    # testbench/rest_server.py stringifies a `None` when it builds this
    # header for the XML PUT path. Pinned here (as the RenameFolder bugs are
    # pinned in trace_grpc.py) rather than silently dropped or worked around.
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
    # A name containing "/" so `list-objects-with-delimiter` actually groups
    # something: with no such name, delimiter listing was unmonitored --
    # both it and the plain listing returned the same items and an empty
    # "prefixes", so a regression in prefix/delimiter handling would not
    # move the golden at all. Uploaded last (after NESTED sorts last
    # alphabetically too, keeping first-sighting order intact).
    api(
        "upload-nested",
        "POST",
        "/upload/storage/v1/b/%s/o" % BUCKET,
        params={"uploadType": "media", "name": NESTED},
        data=PAYLOAD,
        headers={"Content-Type": "text/plain"},
    )
    list_response = api("list-objects", "GET", "/storage/v1/b/%s/o" % BUCKET)
    listed_names = {item["name"] for item in list_response.json().get("items", [])}
    assert NESTED in listed_names, "list-objects did not include %r" % NESTED
    delimiter_response = api(
        "list-objects-with-delimiter",
        "GET",
        "/storage/v1/b/%s/o" % BUCKET,
        params={"delimiter": "/"},
    )
    prefixes = delimiter_response.json().get("prefixes", [])
    assert prefixes, "list-objects-with-delimiter produced no prefixes"

    # --- decompressive transcoding ----------------------------------------
    api(
        "upload-gzipped",
        "POST",
        "/upload/storage/v1/b/%s/o" % BUCKET,
        params={"uploadType": "media", "name": "gz.txt", "contentEncoding": "gzip"},
        # GZIPPED_PAYLOAD is a fixed literal (see module top) rather than a
        # freshly-computed `gzip.compress(...)`: a runtime call carries the
        # capturing machine's zlib OS byte into the stored object's crc32c/
        # md5Hash, making the goldens machine-dependent.
        data=GZIPPED_PAYLOAD,
        headers={"Content-Type": "text/plain"},
    )
    api(
        "download-transcoded",
        "GET",
        "/storage/v1/b/%s/o/gz.txt" % BUCKET,
        params={"alt": "media"},
        headers={"Accept-Encoding": "identity"},
    )
    # Not routed through `api()`: `requests` transparently gunzips
    # `response.content` whenever the response carries a `Content-Encoding:
    # gzip` header, which is exactly the header the *non*-transcoding path
    # sends (the transcoding path above never sets it, since the server
    # already decompressed). Left alone, `Recorder._body` would digest the
    # decompressed bytes in both cases -- confirmed empirically:
    # `download-transcoded` and `download-not-transcoded` recorded identical
    # `length`/`sha256`, so a regression that returned malformed or
    # wrongly-compressed bytes on the gzip path would never move the golden.
    # `stream=True` plus reading `response.raw` with `decode_content=False`
    # gets the wire bytes before urllib3's transparent decompression; setting
    # `_content`/`_content_consumed` makes `response.content` (what
    # `record_http` reads) return those raw bytes instead of triggering a
    # fresh, decoding read.
    not_transcoded = session.request(
        "GET",
        base + "/storage/v1/b/%s/o/gz.txt" % BUCKET,
        params={"alt": "media"},
        headers={"Accept-Encoding": "gzip"},
        timeout=30,
        stream=True,
    )
    not_transcoded._content = not_transcoded.raw.read(decode_content=False)
    not_transcoded._content_consumed = True
    rec.record_http("download-not-transcoded", not_transcoded)

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
    # Corrected after review: the original pair was malformed two ways -- the
    # "key" decoded to the 44-byte ASCII string
    # "iXk9eXVbDwHUx2Dg6J5bT8A7OyUzpnuGqZOBTUoKCgM=" (base64-*of*-base64, not
    # a 32-byte AES-256 key), and the "sha256" decoded to the ASCII hex text
    # "774059E51423C444E38A0D6AD9AC4310957B6A68" rather than to a digest. The
    # emulator rejected the upload with customerEncryptionKeySha256IsInvalid,
    # and the two download steps 404'd because the object was never created
    # -- three stable, meaningless recordings that read as CSEK coverage. The
    # pair below is verified: the key decodes to exactly 32 bytes, and the
    # sha is the base64 SHA-256 digest of those bytes.
    key_b64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
    sha_b64 = "Yw3NKWbEM2aRElRIu7JbT/QSpJxzLbLIq8G4WBvXEN0="
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

    # A dedicated, empty, never-referenced-again bucket so a successful
    # delete can be recorded: `delete-non-empty-bucket` above only ever
    # exercises the 400 rejection path, leaving a successful
    # `bucket_deleted` notification with no baseline at all. Placed last,
    # after everything else: the canonicalizer's `<GEN:n>`/`<ID:n>`/
    # `<TIME:n>` placeholders number values in first-sighted order across
    # the whole trace, so adding these two interactions anywhere earlier
    # would renumber every symbol first sighted afterward -- turning a
    # two-interaction addition into a diff touching most of the golden.
    # Appending here means nothing else moves.
    api(
        "create-bucket-for-delete",
        "POST",
        "/storage/v1/b",
        params={"project": "test-project"},
        json={"name": TO_DELETE},
    )
    api("delete-bucket-success", "DELETE", "/storage/v1/b/" + TO_DELETE)

    return rec.finish()
