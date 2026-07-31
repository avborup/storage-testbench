import gc
import io
import os
import tempfile
import unittest

from hypothesis import given
from hypothesis import strategies as st

from testbench.filemedia import FileMedia
from testbench.media import BytesMedia


def _file_media(data):
    d = tempfile.mkdtemp()
    fd = os.open(d, os.O_RDONLY)
    with open(os.path.join(d, "m"), "wb") as fh:
        fh.write(data)
    fm = FileMedia.from_path(fd, "m")  # test-only convenience ctor over a dir_fd
    os.close(fd)
    return fm


class TestFileMediaReadParity(unittest.TestCase):
    @given(st.binary(max_size=4096), st.integers(1, 512))
    def test_chunks_boundaries_match_bytesmedia_from_zero(self, data, size):
        fm, bm = _file_media(data), BytesMedia(data)
        self.assertEqual(
            list(bm.chunks(0, len(data), size)), list(fm.chunks(0, len(data), size))
        )

    @given(
        st.binary(min_size=1, max_size=4096),
        st.integers(1, 512),
        st.integers(0, 4096),
        st.integers(0, 4096),
    )
    def test_chunks_boundaries_match_bytesmedia_midoffset(self, data, size, a, b):
        begin, end = min(a, b) % (len(data) + 1), max(a, b) % (len(data) + 1)
        fm, bm = _file_media(data), BytesMedia(data)
        self.assertEqual(
            list(bm.chunks(begin, end, size)), list(fm.chunks(begin, end, size))
        )

    def test_midoffset_pinned_case(self):
        fm, bm = _file_media(b"0123456789abcdefghij" * 3), BytesMedia(
            b"0123456789abcdefghij" * 3
        )
        self.assertEqual(list(bm.chunks(10, 43, 16)), list(fm.chunks(10, 43, 16)))

    def test_empty_slice_yields_nothing(self):
        for data in (b"", b"abc"):
            fm = _file_media(data)
            self.assertEqual([], list(fm.chunks(0, 0, 4)))
            self.assertEqual([], list(fm.chunks(2, 2, 4)))

    def test_zero_length_reads_do_not_crash(self):
        fm = _file_media(b"")
        self.assertEqual(0, len(fm))
        self.assertEqual(b"", fm[0:0])
        self.assertEqual(b"", fm.reader().read())
        self.assertEqual([], list(fm.chunks(0, 0, 8)))

    def test_getitem_int_and_slice_match_bytes(self):
        fm = _file_media(b"hello world")
        self.assertEqual(ord("h"), fm[0])
        self.assertEqual(b"ello", fm[1:5])
        self.assertEqual(b"world", fm[-5:])

    def test_reader_is_fresh_each_call(self):
        fm = _file_media(b"abcdef")
        r1, r2 = fm.reader(), fm.reader()
        self.assertEqual(b"abc", r1.read(3))
        self.assertEqual(b"abcdef", r2.read())

    @given(st.binary(max_size=8192))
    def test_checksums_equal_whole_buffer(self, data):
        fm, bm = _file_media(data), BytesMedia(data)
        self.assertEqual(bm.crc32c(), fm.crc32c())
        self.assertEqual(bm.md5(), fm.md5())

    def test_checksums_equal_whole_buffer_multichunk(self):
        # The hypothesis parity above tops out at 8192 bytes < READ_CHUNK (1 MiB),
        # so _from_open_fd always streams in a single pread there and cannot tell a
        # chained crc32c seed from a per-chunk reseed. This pins the multi-chunk
        # crc32c seed-chain (and streamed md5) explicitly against the whole-buffer
        # BytesMedia so the chaining guard is killable.
        import testbench.filemedia as fmmod

        data = (b"abcdefghij" * 7 + b"\x00\xff") * 40000  # > 2 * READ_CHUNK
        self.assertGreater(len(data), 2 * fmmod.READ_CHUNK)
        fm, bm = _file_media(data), BytesMedia(data)
        self.assertEqual(bm.crc32c(), fm.crc32c())
        self.assertEqual(bm.md5(), fm.md5())

    def test_eq_and_to_bytes_compat(self):
        fm = _file_media(b"xyz")
        self.assertEqual(b"xyz", fm.to_bytes())
        self.assertTrue(fm == b"xyz")
        self.assertEqual(b"pre" + b"xyz", b"pre" + fm)

    def test_close_is_idempotent_and_releases_fd(self):
        fm = _file_media(b"data")
        rfd = fm._read_fd
        fm.close()
        fm.close()  # idempotent
        with self.assertRaises(OSError):
            os.fstat(rfd)  # fd closed


if __name__ == "__main__":
    unittest.main()
