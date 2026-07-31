import unittest

import testbench.database


class TestSeedBuckets(unittest.TestCase):
    def _db(self):
        return testbench.database.Database.init()  # memory backend

    def test_seeds_each_named_bucket(self):
        db = self._db()
        db.seed_buckets(["audio", "transcripts", "models"])
        for name in ("audio", "transcripts", "models"):
            self.assertIn("projects/_/buckets/%s" % name, db._buckets)

    def test_is_idempotent_across_calls(self):
        db = self._db()
        db.seed_buckets(["audio"])
        db.seed_buckets(["audio", "transcripts"])  # 'audio' already present -> skip
        self.assertIn("projects/_/buckets/audio", db._buckets)
        self.assertIn("projects/_/buckets/transcripts", db._buckets)

    def test_empty_list_creates_nothing(self):
        db = self._db()
        before = dict(db._buckets)
        db.seed_buckets([])
        self.assertEqual(before, db._buckets)
