import os
import tempfile
import time
import unittest

from testbench import containment
from testbench.filemedia import FileMedia
from testbench.media import BytesMedia

_POSIX_ONLY = unittest.skipIf(
    os.name == "nt", "file backend staging is POSIX-only (openat/O_NOFOLLOW/O_APPEND)"
)


@_POSIX_ONLY
class TestFileMediaStaging(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.dfd = os.open(self.root, os.O_RDONLY)
        self.addCleanup(os.close, self.dfd)
        os.mkdir(os.path.join(self.root, "dst"))
        self.dst_dfd = os.open(os.path.join(self.root, "dst"), os.O_RDONLY)
        self.addCleanup(os.close, self.dst_dfd)

    def test_append_rolls_checksums_incrementally(self):
        fm, bm = FileMedia.new_staging(self.dfd, "u"), BytesMedia(b"")
        for piece in (b"the ", b"quick ", b"brown ", b"fox"):
            fm.append(piece)
            bm.append(piece)
        self.assertEqual(len(bm), len(fm))
        self.assertEqual(bm.crc32c(), fm.crc32c())
        self.assertEqual(bm.md5(), fm.md5())
        self.assertEqual(bm.to_bytes(), fm.to_bytes())

    def test_finalize_replaces_into_destination_and_reads_survive(self):
        fm = FileMedia.new_staging(self.dfd, "u")
        fm.append(b"payload")
        fm.finalize((self.dst_dfd, "final"))
        self.assertTrue(fm.is_finalized)
        self.assertFalse(os.path.exists(os.path.join(self.root, "u")))  # staging gone
        self.assertEqual(
            b"payload", open(os.path.join(self.root, "dst", "final"), "rb").read()
        )
        self.assertEqual(b"payload", fm.to_bytes())  # read fd valid post-rename

    def test_link_into_then_append_then_seal(self):
        # Appendable lifecycle: link the (empty) staging into the destination,
        # keep appending -> the shared inode grows at the destination path, then
        # seal removes the staging name but leaves the destination hardlink.
        fm = FileMedia.new_staging(self.dfd, "u")
        fm.link_into((self.dst_dfd, "live"))
        self.assertFalse(fm.is_finalized)  # still staging
        fm.append(b"aaa")
        fm.append(b"bbb")
        self.assertEqual(
            b"aaabbb", open(os.path.join(self.root, "dst", "live"), "rb").read()
        )
        fm.seal()
        self.assertTrue(fm.is_finalized)
        self.assertFalse(os.path.exists(os.path.join(self.root, "u")))  # staging gone
        self.assertEqual(
            b"aaabbb", open(os.path.join(self.root, "dst", "live"), "rb").read()
        )
        self.assertEqual(b"aaabbb", fm.to_bytes())  # read fd still valid

    def test_append_is_linear_time(self):
        def elapsed(n):
            fm = FileMedia.new_staging(self.dfd, "t%d" % n)
            chunk = b"a" * (1024 * 1024)
            t0 = time.perf_counter()
            for _ in range(n):
                fm.append(chunk)
            return time.perf_counter() - t0

        n = 64
        self.assertLess(elapsed(2 * n) / max(elapsed(n), 1e-6), 3.0)  # O(n), not O(n^2)


if __name__ == "__main__":
    unittest.main()
