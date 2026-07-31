# tests/test_fsync.py
import os
import tempfile
import unittest

from testbench import containment
from testbench.filemedia import FileMedia


class _FsyncCounter:
    def __init__(self):
        self.calls = 0

    def __call__(self, fd):
        self.calls += 1


class TestFsyncGate(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.dfd = os.open(self.root, os.O_RDONLY)
        self.addCleanup(os.close, self.dfd)
        self._saved_flag = containment.FSYNC
        self._real_fsync = os.fsync
        self.counter = _FsyncCounter()
        os.fsync = self.counter  # count fsyncs regardless of flag
        self.addCleanup(self._restore)

    def _restore(self):
        os.fsync = self._real_fsync
        containment.FSYNC = self._saved_flag

    def test_metadata_write_does_not_fsync_when_off(self):
        containment.FSYNC = False
        containment.write_bytes_atomic(self.dfd, "m", b"payload")
        self.assertEqual(0, self.counter.calls)
        self.assertEqual(b"payload", open(os.path.join(self.root, "m"), "rb").read())

    def test_metadata_write_fsyncs_file_and_dir_when_on(self):
        containment.FSYNC = True
        containment.write_bytes_atomic(self.dfd, "m", b"payload")
        self.assertEqual(2, self.counter.calls)  # temp-file fd + parent dir_fd
        self.assertEqual(b"payload", open(os.path.join(self.root, "m"), "rb").read())

    def test_finalize_does_not_fsync_when_off(self):
        containment.FSYNC = False
        os.mkdir(os.path.join(self.root, "dst"))
        ddfd = os.open(os.path.join(self.root, "dst"), os.O_RDONLY)
        self.addCleanup(os.close, ddfd)
        fm = FileMedia.new_staging(self.dfd, "u")
        fm.append(b"abc")
        fm.finalize((ddfd, "final"))
        self.assertEqual(0, self.counter.calls)

    def test_finalize_fsyncs_media_when_on(self):
        containment.FSYNC = True
        os.mkdir(os.path.join(self.root, "dst"))
        ddfd = os.open(os.path.join(self.root, "dst"), os.O_RDONLY)
        self.addCleanup(os.close, ddfd)
        fm = FileMedia.new_staging(self.dfd, "u")
        fm.append(b"abc")
        fm.finalize((ddfd, "final"))
        self.assertGreaterEqual(self.counter.calls, 1)  # append_fd fsynced pre-close
