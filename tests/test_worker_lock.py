import os
import tempfile
import unittest

from testbench import containment
from testbench.filestore import FileStore


class TestWorkerLock(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.lock = os.path.join(self.root, ".gcs-worker.lock")

    def test_first_claim_succeeds_and_writes_pid(self):
        path = containment.claim_worker_lock(self.root)
        self.assertEqual(self.lock, path)
        self.assertEqual(str(os.getpid()), open(self.lock).read().strip())

    def test_second_live_holder_fails_loudly(self):
        # A different, definitely-ALIVE pid holds the lock -> refuse.
        with open(self.lock, "w") as fh:
            fh.write(str(os.getpid()))  # our own pid: guaranteed alive
        with self.assertRaises(RuntimeError):
            containment.claim_worker_lock(self.root)

    def test_stale_lock_from_dead_pid_is_reclaimed(self):
        dead = self._a_dead_pid()
        with open(self.lock, "w") as fh:
            fh.write(str(dead))
        path = containment.claim_worker_lock(self.root)  # must reclaim, not raise
        self.assertEqual(str(os.getpid()), open(path).read().strip())

    def test_unreadable_marker_is_refused_fail_safe(self):
        # An empty/garbage marker cannot be proven stale -> fail safe, refuse.
        with open(self.lock, "w") as fh:
            fh.write("")  # nascent-race sentinel
        with self.assertRaises(RuntimeError):
            containment.claim_worker_lock(self.root)

    def _a_dead_pid(self):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)  # reap -> pid is now dead
        return pid


class TestClearedSkipsLockMarker(unittest.TestCase):
    # Regression: the sibling lock FILE must not reach constrained_rmtree via
    # cleared() -> _index_names (containment.constrained_rmtree raises on a
    # non-directory). _index_names must filter to directories only.
    def test_cleared_succeeds_with_lock_marker_present(self):
        root = tempfile.mkdtemp()
        os.mkdir(os.path.join(root, "abucket"))  # a real bucket dir
        with open(os.path.join(root, ".gcs-worker.lock"), "w") as fh:
            fh.write(str(os.getpid()))  # the sibling marker
        store = FileStore(root)
        store.cleared()  # must NOT raise
        self.assertFalse(os.path.exists(os.path.join(root, "abucket")))
        # The marker file itself is untouched by cleared() (not a bucket).
        self.assertTrue(os.path.exists(os.path.join(root, ".gcs-worker.lock")))
