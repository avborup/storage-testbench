import os
import tempfile
import unittest

from testbench import containment

_POSIX_ONLY = unittest.skipIf(
    os.name == "nt", "file backend containment is POSIX-only (openat/O_NOFOLLOW)"
)


class TestContainment(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    @_POSIX_ONLY
    def test_posix_support_asserted(self):
        containment.assert_posix_support()  # must not raise on Linux/macOS

    def test_assert_within_rejects_escape(self):
        outside = os.path.join(self.root, "..", "escapee")
        with self.assertRaises(PermissionError):
            containment.assert_within(outside, self.root)
        containment.assert_within(os.path.join(self.root, "a", "b"), self.root)

    @_POSIX_ONLY
    def test_safe_open_refuses_symlink_final_component(self):
        target = os.path.join(self.root, "real")
        open(target, "wb").close()
        os.symlink(target, os.path.join(self.root, "link"))
        rfd = os.open(self.root, os.O_RDONLY)
        try:
            with self.assertRaises(OSError):
                containment.safe_open(rfd, "link", os.O_RDONLY)
        finally:
            os.close(rfd)

    @_POSIX_ONLY
    def test_walk_dirs_refuses_symlinked_intermediate_component(self):
        # Plant <root>/audio -> /tmp (an intermediate dir component); walking
        # into it with create=False must be refused by O_NOFOLLOW.
        outside = tempfile.mkdtemp()
        os.symlink(outside, os.path.join(self.root, "audio"))
        rfd = os.open(self.root, os.O_RDONLY)
        try:
            with self.assertRaises(OSError):
                containment.walk_dirs(rfd, ["audio"], create=False)
        finally:
            os.close(rfd)

    @_POSIX_ONLY
    def test_write_bytes_atomic_lands_in_dir(self):
        rfd = os.open(self.root, os.O_RDONLY)
        try:
            containment.write_bytes_atomic(rfd, "m", b"hello")
        finally:
            os.close(rfd)
        self.assertEqual(b"hello", open(os.path.join(self.root, "m"), "rb").read())

    def test_constrained_rmtree(self):
        b = os.path.join(self.root, "bucket-x")
        os.makedirs(os.path.join(b, "audio"))
        containment.constrained_rmtree(b, self.root, {"bucket-x"})
        self.assertFalse(os.path.exists(b))
        containment.constrained_rmtree(b, self.root, {"bucket-x"})  # already gone

    @_POSIX_ONLY
    def test_constrained_rmtree_refuses_symlink_non_child_unindexed(self):
        outside = tempfile.mkdtemp()
        os.symlink(outside, os.path.join(self.root, "evil"))
        with self.assertRaises(PermissionError):
            containment.constrained_rmtree(
                os.path.join(self.root, "evil"), self.root, {"evil"}
            )
        with self.assertRaises(PermissionError):
            containment.constrained_rmtree(
                outside, self.root, {os.path.basename(outside)}
            )
        real = os.path.join(self.root, "bucket-y")
        os.makedirs(real)
        with self.assertRaises(PermissionError):
            containment.constrained_rmtree(real, self.root, set())
