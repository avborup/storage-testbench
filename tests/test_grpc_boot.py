import os
import unittest

import testbench.rest_server as rs


class _RunSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, port, database, echo_metadata=False):
        self.calls.append((port, echo_metadata))
        return (port or 50051, object())  # (bound_port, fake server)


class TestGrpcBoot(unittest.TestCase):
    def setUp(self):
        self._saved = (rs.grpc_port, rs.grpc_service)
        rs.grpc_port, rs.grpc_service = 0, None
        self._real_run = rs.testbench.grpc_server.run
        self.spy = _RunSpy()
        rs.testbench.grpc_server.run = self.spy
        self.addCleanup(self._restore)

        self._saved_buckets = os.environ.pop("TESTBENCH_BUCKETS", None)
        self._saved_grpc_port = os.environ.pop("TESTBENCH_GRPC_PORT", None)

    def _restore(self):
        rs.testbench.grpc_server.run = self._real_run
        rs.grpc_port, rs.grpc_service = self._saved
        for var, val in (
            ("TESTBENCH_BUCKETS", self._saved_buckets),
            ("TESTBENCH_GRPC_PORT", self._saved_grpc_port),
        ):
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val

    def test_start_grpc_sets_module_globals(self):
        rs._start_grpc(9001)
        self.assertEqual([(9001, False)], self.spy.calls)
        self.assertEqual(9001, rs.grpc_port)  # MODULE global, not a local
        self.assertIsNotNone(rs.grpc_service)

    def test_boot_then_start_grpc_route_is_a_noop(self):
        # boot-start claims the port; a later /start_grpc must NOT start a 2nd server.
        rs._start_grpc(9001)
        rs._start_grpc(0)  # simulates the route's run() call
        self.assertEqual(1, len(self.spy.calls))  # exactly one server started

    def test_seed_and_boot_are_gated(self):
        for var in ("TESTBENCH_BUCKETS", "TESTBENCH_GRPC_PORT"):
            os.environ.pop(var, None)
        rs.grpc_port, rs.grpc_service = 0, None
        rs._bootstrap_from_env()
        self.assertEqual([], self.spy.calls)  # nothing booted when unset
        self.assertEqual(0, rs.grpc_port)

    def test_empty_buckets_var_creates_nothing(self):
        os.environ["TESTBENCH_BUCKETS"] = ""  # gcs-test sets this
        seen = []
        real_seed = rs.db.seed_buckets
        rs.db.seed_buckets = lambda names: seen.extend(names)
        try:
            rs._bootstrap_from_env()
        finally:
            rs.db.seed_buckets = real_seed
        self.assertEqual([], seen)  # "" -> outer `if buckets` guard -> no names

    def test_interior_empty_segments_are_filtered(self):
        # A truthy value with trailing/interior empty segments reaches the
        # comprehension; the `if n` filter (NOT the outer `if buckets` guard) is the
        # load-bearing gate that drops the empties. Kills the empty-filter mutation.
        os.environ["TESTBENCH_BUCKETS"] = "audio,,models,"
        seen = []
        real_seed = rs.db.seed_buckets
        rs.db.seed_buckets = lambda names: seen.extend(names)
        try:
            rs._bootstrap_from_env()
        finally:
            rs.db.seed_buckets = real_seed
        self.assertEqual(["audio", "models"], seen)  # empties filtered by `if n`
