import time
import unittest

from testbench.media import BytesMedia


class TestChecksumScaling(unittest.TestCase):
    """The pre-seam BidiWrite path recomputed crc32c over the whole accumulated
    buffer on every flush (gcs/upload.py:590), which is O(n^2) in the upload
    size. BytesMedia's rolling checksum makes N flushes O(n) total. Timing the
    'N flushes of a fixed chunk' pattern at size N and 2N must stay roughly
    linear: quadratic gives ~4x, linear ~2x. The 3x threshold is machine-speed
    independent (a ratio), the robust-detector shape from the spec."""

    def _time_flush_pattern(self, flushes, chunk=b"x" * 65536):
        m = BytesMedia()
        start = time.perf_counter()
        for _ in range(flushes):
            m.append(chunk)
            _ = m.crc32c()  # what :590 does every flush
        return time.perf_counter() - start

    def test_flush_checksum_is_not_quadratic(self):
        t_n = self._time_flush_pattern(500)
        t_2n = self._time_flush_pattern(1000)
        self.assertLess(t_2n / max(t_n, 1e-6), 3.0)
