import hashlib
import unittest

import crc32c

from testbench.media import BytesMedia


class TestBytesMedia(unittest.TestCase):
    def test_len_and_slice_match_bytes(self):
        m = BytesMedia(b"The quick brown fox")
        self.assertEqual(19, len(m))
        self.assertEqual(b"quick", m[4:9])
        self.assertEqual(b"fox", m[-3:])
        self.assertEqual(ord("T"), m[0])

    def test_append_and_iadd_accumulate(self):
        m = BytesMedia(b"hello")
        m.append(b" ")
        m += b"world"
        self.assertEqual(b"hello world", m.to_bytes())
        self.assertEqual(11, len(m))

    def test_concatenation_both_sides_yields_bytes(self):
        # Compose/rewrite do `dst += src_object.media` and `composed += blob.media`.
        m = BytesMedia(b"world")
        self.assertEqual(b"helloworld", b"hello" + m)
        self.assertEqual(b"worldhello", m + b"hello")

    def test_crc32c_matches_whole_buffer_and_chains_on_append(self):
        # The load-bearing property: an incrementally-maintained crc32c must equal
        # crc32c over the whole buffer. Plan 1 pinned crc32c(data, seed) chaining.
        m = BytesMedia(b"hello")
        m.append(b"world")
        self.assertEqual(crc32c.crc32c(b"helloworld"), m.crc32c())

    def test_md5_matches_whole_buffer_and_chains_on_append(self):
        m = BytesMedia(b"hello")
        m.append(b"world")
        self.assertEqual(hashlib.md5(b"helloworld").digest(), m.md5())

    def test_chunks_covers_range_without_gaps_or_overlap(self):
        m = BytesMedia(b"0123456789")
        self.assertEqual([b"012", b"345", b"678", b"9"], list(m.chunks(0, 10, 3)))
        self.assertEqual([b"23", b"45"], list(m.chunks(2, 6, 2)))

    def test_reader_streams_the_whole_buffer(self):
        m = BytesMedia(b"streamed content")
        self.assertEqual(b"streamed content", m.reader().read())

    def test_empty_media_checksums_match_empty_bytes(self):
        m = BytesMedia()
        self.assertEqual(crc32c.crc32c(b""), m.crc32c())
        self.assertEqual(hashlib.md5(b"").digest(), m.md5())
