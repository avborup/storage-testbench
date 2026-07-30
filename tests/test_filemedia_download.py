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

"""Task-6 REST download parity + bounded memory.

Two things the conformance trace cannot see, guarded here:

1. Overflow ranges. The trace only exercises ``bytes=10-19``, ``bytes=10-``,
   ``bytes=-10`` and a 416 case; it is BLIND to a forward-partial overflow
   (``bytes=40-100`` on a 43-byte object) and a suffix overflow (``bytes=-N``
   with ``N>length``). The new arithmetic ``_download_range`` must reproduce
   the pre-refactor slicing code's ``Content-Length``/``Content-Range`` byte
   for byte on every axis -- including the negative ``begin`` a suffix overflow
   produces, where ``Content-Length`` is ``end - max(0, begin)`` (the whole
   buffer), NOT ``end - begin``.

2. Bounded memory. Dropping the line-536 ``to_bytes()`` hot path means an
   unranged full GET over the memory backend must no longer materialise a whole
   second copy of the object, and a ranged GET over a large FileMedia must
   stream the slice without loading the whole file.

Driven IN-PROCESS against ``gcs.object`` so ``resource.getrusage`` observes the
process that actually streams the bytes.
"""

import gc
import gzip
import json
import os
import resource
import shutil
import sys
import tempfile
import unittest

from werkzeug.test import create_environ
from werkzeug.wrappers import Request

import gcs.bucket
import gcs.object
import testbench.common
from testbench.filemedia import FileMedia
from testbench.media import BytesMedia

MiB = 1024 * 1024

# A 43-byte object: 40 bytes of "0123456789" repeated four times, then "abc".
PAYLOAD = b"0123456789" * 4 + b"abc"


def rss_bytes():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r if sys.platform == "darwin" else r * 1024  # macOS bytes, Linux KiB


def _range_request(range_header=None):
    headers = {} if range_header is None else {"range": range_header}
    return Request(
        create_environ(
            base_url="http://localhost:8080",
            headers=headers,
            data=json.dumps({}),
        )
    )


def _drain(response):
    """Consume a streaming response body lazily, returning (total_len, body).

    Accumulates the body -- only use on the small parity objects."""
    total = 0
    body = bytearray()
    for chunk in response.response:
        total += len(chunk)
        body.extend(chunk)
    return total, bytes(body)


def _drain_count(response):
    """Consume a streaming response body lazily, returning only the total length.

    Keeps at most one chunk live so the drain itself never materialises a large
    object -- required by the bounded-RSS assertions."""
    total = 0
    for chunk in response.response:
        total += len(chunk)
    return total


class TestDownloadOverflowParity(unittest.TestCase):
    """The same range axes over BOTH media backends must agree, byte for byte,
    with the documented pre-refactor clamps."""

    def setUp(self):
        request = testbench.common.FakeRequest(
            args={}, data=json.dumps({"name": "bucket"})
        )
        self.bucket, _ = gcs.bucket.Bucket.init(request, None)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _bytes_blob(self):
        blob, _ = gcs.object.Object.init_dict(
            testbench.common.FakeRequest(args={}, headers={}, environ={}),
            {"name": "o"},
            BytesMedia(PAYLOAD),
            self.bucket.metadata,
            False,
        )
        return blob

    def _file_blob(self):
        dfd = os.open(self.tmp, os.O_RDONLY)
        try:
            fm = FileMedia.new_staging(dfd, "o-%d" % id(self))
        finally:
            os.close(dfd)
        fm.append(PAYLOAD)
        blob, _ = gcs.object.Object.init_dict(
            testbench.common.FakeRequest(args={}, headers={}, environ={}),
            {"name": "o"},
            fm,
            self.bucket.metadata,
            False,
        )
        self.addCleanup(fm.close)
        return blob

    # (range_header, status, content_length, content_range, body)
    CASES = [
        (None, 200, "43", None, PAYLOAD),
        ("bytes=10-19", 206, "10", "bytes 10-19/43", PAYLOAD[10:20]),
        ("bytes=10-", 206, "33", "bytes 10-42/43", PAYLOAD[10:]),
        ("bytes=-10", 206, "10", "bytes 33-42/43", PAYLOAD[33:]),
        # Trace-blind overflow axes:
        ("bytes=40-100", 206, "3", "bytes 40-42/43", PAYLOAD[40:]),
        ("bytes=-100", 206, "43", "bytes -57-42/43", PAYLOAD),
    ]

    def _check(self, blob):
        for range_header, status, cl, cr, body in self.CASES:
            response = blob.rest_media(_range_request(range_header))
            total, got = _drain(response)
            self.assertEqual(status, response.status_code, msg=str(range_header))
            self.assertEqual(
                cl,
                str(response.headers["Content-Length"]),
                msg="Content-Length for %s" % range_header,
            )
            # The body length must match the declared Content-Length exactly
            # (content_length_matches_body), else Werkzeug re-frames the wire.
            self.assertEqual(int(cl), total, msg="body length for %s" % range_header)
            self.assertEqual(body, got, msg="body bytes for %s" % range_header)
            if cr is not None:
                self.assertEqual(
                    cr,
                    response.headers["Content-Range"],
                    msg="Content-Range for %s" % range_header,
                )

    def test_bytesmedia_download_axes(self):
        self._check(self._bytes_blob())

    def test_filemedia_download_axes(self):
        self._check(self._file_blob())

    def test_bytesmedia_and_filemedia_agree(self):
        # Belt-and-braces: the two backends produce identical headers+body.
        bm, fm = self._bytes_blob(), self._file_blob()
        for range_header, _, _, _, _ in self.CASES:
            rb = bm.rest_media(_range_request(range_header))
            rf = fm.rest_media(_range_request(range_header))
            _, body_b = _drain(rb)
            _, body_f = _drain(rf)
            self.assertEqual(rb.status_code, rf.status_code, msg=str(range_header))
            self.assertEqual(
                str(rb.headers["Content-Length"]),
                str(rf.headers["Content-Length"]),
                msg=str(range_header),
            )
            self.assertEqual(
                rb.headers.get("Content-Range"),
                rf.headers.get("Content-Range"),
                msg=str(range_header),
            )
            self.assertEqual(body_b, body_f, msg=str(range_header))

    def test_416_when_begin_past_end(self):
        for blob in (self._bytes_blob(), self._file_blob()):
            with self.assertRaises(testbench.error.RestException) as rest:
                blob.rest_media(_range_request("bytes=43-"))
            self.assertEqual(416, rest.exception.code)


class TestDownloadBoundedMemory(unittest.TestCase):
    def setUp(self):
        request = testbench.common.FakeRequest(
            args={}, data=json.dumps({"name": "bucket"})
        )
        self.bucket, _ = gcs.bucket.Bucket.init(request, None)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_memory_unranged_download_does_not_double_the_buffer(self):
        # The dropped line-536 to_bytes() hot path: an unranged full GET over a
        # large BytesMedia must NOT allocate a whole second copy of the object.
        # (Old code called to_bytes() only to set Content-Length = len(...);
        # the new arithmetic Content-Length streams without the copy.)
        size = 256 * MiB
        blob, _ = gcs.object.Object.init_dict(
            testbench.common.FakeRequest(args={}, headers={}, environ={}),
            {"name": "big"},
            BytesMedia(b"\0" * size),
            self.bucket.metadata,
            False,
        )
        gc.collect()
        base = rss_bytes()
        response = blob.rest_media(_range_request(None))
        total = _drain_count(response)
        peak = rss_bytes()
        self.assertEqual(size, total)
        self.assertEqual(str(size), str(response.headers["Content-Length"]))
        delta = peak - base
        self.assertLess(
            delta,
            128 * MiB,
            "peak RSS delta %d MiB: unranged GET materialised a second buffer"
            % (delta // MiB),
        )

    def test_file_ranged_download_streams_without_loading_whole_file(self):
        size = 256 * MiB
        dfd = os.open(self.tmp, os.O_RDONLY)
        try:
            fm = FileMedia.new_staging(dfd, "big")
        finally:
            os.close(dfd)
        chunk = b"a" * MiB
        for _ in range(size // MiB):
            fm.append(chunk)
        self.addCleanup(fm.close)
        blob, _ = gcs.object.Object.init_dict(
            testbench.common.FakeRequest(args={}, headers={}, environ={}),
            {"name": "big"},
            fm,
            self.bucket.metadata,
            False,
        )
        begin, end = 100 * MiB, 100 * MiB + 1000
        gc.collect()
        base = rss_bytes()
        response = blob.rest_media(_range_request("bytes=%d-%d" % (begin, end - 1)))
        total = _drain_count(response)
        peak = rss_bytes()
        self.assertEqual(206, response.status_code)
        self.assertEqual(end - begin, total)
        self.assertEqual(str(end - begin), str(response.headers["Content-Length"]))
        delta = peak - base
        self.assertLess(
            delta,
            128 * MiB,
            "peak RSS delta %d MiB: ranged GET loaded the whole file" % (delta // MiB),
        )


class TestTranscodeStreaming(unittest.TestCase):
    """Task-7 decompressive-transcode streaming.

    A ``content_encoding=gzip`` object downloaded WITHOUT ``gzip`` in
    ``accept-encoding`` is decompressively transcoded: the server gunzips the
    stored bytes and serves the decompressed body with a ``Content-Length`` equal
    to the decompressed size. The transcoded length is not known a priori, so a
    bounded two-pass counting-then-streaming approach supplies it -- the whole
    decompressed object must never be held in memory.
    """

    def setUp(self):
        request = testbench.common.FakeRequest(
            args={}, data=json.dumps({"name": "bucket"})
        )
        self.bucket, _ = gcs.bucket.Bucket.init(request, None)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _blob(self, media, raw):
        # ``media`` is a Media holding gzip-compressed bytes; ``raw`` is the
        # decompressed payload the transcode must reproduce.
        blob, _ = gcs.object.Object.init_dict(
            testbench.common.FakeRequest(
                args={"contentEncoding": "gzip"}, headers={}, environ={}
            ),
            {"name": "o", "contentEncoding": "gzip"},
            media,
            self.bucket.metadata,
            False,
        )
        return blob

    def _bytes_blob(self, raw):
        return self._blob(BytesMedia(gzip.compress(raw)), raw)

    def _file_blob(self, raw):
        dfd = os.open(self.tmp, os.O_RDONLY)
        try:
            fm = FileMedia.new_staging(dfd, "gz-%d" % id(raw))
        finally:
            os.close(dfd)
        fm.append(gzip.compress(raw))
        self.addCleanup(fm.close)
        return self._blob(fm, raw)

    def _transcode_request(self):
        # No ``gzip`` in accept-encoding -> decompressive transcoding fires.
        return Request(
            create_environ(
                base_url="http://localhost:8080",
                headers={"accept-encoding": "identity"},
                data=json.dumps({}),
            )
        )

    def _check_correctness(self, blob, raw):
        response = blob.rest_media(self._transcode_request())
        total, body = _drain(response)
        self.assertEqual(200, response.status_code)
        self.assertEqual(raw, body)
        # Content-Length is the DECOMPRESSED length and must match the body.
        self.assertEqual(str(len(raw)), str(response.headers["Content-Length"]))
        self.assertEqual(len(raw), total)
        self.assertEqual(
            "gunzipped",
            response.headers.get("x-guploader-response-body-transformations"),
        )

    def test_bytesmedia_transcode_correctness(self):
        raw = b"How vexingly quick daft zebras jump!" * 100
        self._check_correctness(self._bytes_blob(raw), raw)

    def test_filemedia_transcode_correctness(self):
        raw = b"How vexingly quick daft zebras jump!" * 100
        self._check_correctness(self._file_blob(raw), raw)

    def test_backends_agree_on_transcode(self):
        raw = b"the quick brown fox" * 500
        rb = self._bytes_blob(raw).rest_media(self._transcode_request())
        rf = self._file_blob(raw).rest_media(self._transcode_request())
        _, body_b = _drain(rb)
        _, body_f = _drain(rf)
        self.assertEqual(body_b, body_f)
        self.assertEqual(
            str(rb.headers["Content-Length"]), str(rf.headers["Content-Length"])
        )

    def test_file_transcode_streams_without_materialising_whole_object(self):
        # A highly compressible 256 MiB payload: the compressed staging file is
        # tiny, but the transcoded body is 256 MiB. The pre-Task-7 gz.read()
        # materialised the whole decompressed object; the streaming transcode
        # must keep peak RSS bounded.
        size = 256 * MiB
        raw = b"\0" * size
        dfd = os.open(self.tmp, os.O_RDONLY)
        try:
            fm = FileMedia.new_staging(dfd, "big-gz")
        finally:
            os.close(dfd)
        fm.append(gzip.compress(raw))
        self.addCleanup(fm.close)
        blob = self._blob(fm, raw)
        del raw
        gc.collect()
        base = rss_bytes()
        response = blob.rest_media(self._transcode_request())
        total = _drain_count(response)
        peak = rss_bytes()
        self.assertEqual(200, response.status_code)
        self.assertEqual(size, total)
        self.assertEqual(str(size), str(response.headers["Content-Length"]))
        delta = peak - base
        self.assertLess(
            delta,
            128 * MiB,
            "peak RSS delta %d MiB: transcode materialised the whole object"
            % (delta // MiB),
        )


if __name__ == "__main__":
    unittest.main()
