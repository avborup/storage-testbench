# tests/test_grpc_threads.py
import os
import unittest

from testbench import grpc_server


class TestGrpcThreadCount(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("TESTBENCH_GRPC_THREADS", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("TESTBENCH_GRPC_THREADS", None)
        else:
            os.environ["TESTBENCH_GRPC_THREADS"] = self._saved

    def test_default_is_32(self):
        os.environ.pop("TESTBENCH_GRPC_THREADS", None)
        self.assertEqual(32, grpc_server._grpc_thread_count())

    def test_env_overrides(self):
        os.environ["TESTBENCH_GRPC_THREADS"] = "7"
        self.assertEqual(7, grpc_server._grpc_thread_count())

    def test_run_sizes_executor_from_env(self):
        # run() must actually use the count. Bind an ephemeral port on loopback,
        # then read the ThreadPoolExecutor's configured max_workers off the server.
        # NOTE: server._state.thread_pool._max_workers is a grpcio PRIVATE attribute
        # chain, stable under the pinned grpcio==1.70.0 (setup.py:50). If a version
        # bump moves it, re-pin the observable by passing a futures.ThreadPoolExecutor
        # via a seam and asserting on that instead.
        os.environ["TESTBENCH_GRPC_THREADS"] = "5"
        import testbench.database

        db = testbench.database.Database.init()
        port, server = grpc_server.run(0, db)
        try:
            self.assertEqual(5, server._state.thread_pool._max_workers)
        finally:
            server.stop(None)
