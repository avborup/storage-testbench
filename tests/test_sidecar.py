# tests/test_sidecar.py
import os
import tempfile
import unittest

from google.storage.v2 import storage_pb2
from testbench import sidecar


class TestSidecar(unittest.TestCase):
    def test_object_round_trip_preserves_true_name(self):
        obj = storage_pb2.Object(name="audio/clip.wav.gcsmeta", size=42, generation=17)
        text = sidecar.dump(obj, true_name="audio/clip.wav.gcsmeta")
        kind, name, restored = sidecar.load(text)
        self.assertEqual("Object", kind)
        self.assertEqual("audio/clip.wav.gcsmeta", name)
        self.assertEqual(42, restored.size)
        self.assertEqual(17, restored.generation)

    def test_bucket_round_trip(self):
        b = storage_pb2.Bucket(name="projects/_/buckets/my-bucket", metageneration=4)
        _, name, restored = sidecar.load(sidecar.dump(b, true_name="my-bucket"))
        self.assertEqual("my-bucket", name)
        self.assertEqual(4, restored.metageneration)

    def test_write_atomic_is_fd_based(self):
        d = tempfile.mkdtemp()
        fd = os.open(d, os.O_RDONLY)
        try:
            sidecar.write_atomic(
                fd, "bucket.json", sidecar.dump(storage_pb2.Bucket(name="x"), "x")
            )
        finally:
            os.close(fd)
        _, name, _ = sidecar.read(os.path.join(d, "bucket.json"))
        self.assertEqual("x", name)

    def test_corrupt_sidecar_raises_loudly(self):
        with self.assertRaises(ValueError):
            sidecar.load('{"schema_version": 1, "name": "x", "proto"')  # truncated


if __name__ == "__main__":
    unittest.main()
