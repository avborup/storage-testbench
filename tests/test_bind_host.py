import os
import subprocess
import sys
import unittest
from unittest import mock

import testbench_run
from testbench import grpc_server


class TestBindHost(unittest.TestCase):
    def setUp(self):
        self._store = os.environ.pop("TESTBENCH_STORE", None)
        self._allow = os.environ.pop("TESTBENCH_ALLOW_NONLOOPBACK", None)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in (
            ("TESTBENCH_STORE", self._store),
            ("TESTBENCH_ALLOW_NONLOOPBACK", self._allow),
        ):
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_file_backend_binds_loopback_by_default(self):
        os.environ["TESTBENCH_STORE"] = "file"
        self.assertEqual("127.0.0.1", grpc_server._bind_host())

    def test_memory_backend_binds_all_interfaces(self):
        self.assertEqual("0.0.0.0", grpc_server._bind_host())

    def test_opt_out_allows_all_interfaces_under_file_backend(self):
        os.environ["TESTBENCH_STORE"] = "file"
        os.environ["TESTBENCH_ALLOW_NONLOOPBACK"] = "1"
        self.assertEqual("0.0.0.0", grpc_server._bind_host())

    def test_run_refuses_nonloopback_file_bind_without_optout(self):
        os.environ["TESTBENCH_STORE"] = "file"
        sys.argv[:] = ["testbench_run.py", "0.0.0.0", "9000", "4"]
        with self.assertRaises(SystemExit):
            testbench_run.start_server()

    def test_run_allows_nonloopback_file_bind_with_optout(self):
        os.environ["TESTBENCH_STORE"] = "file"
        os.environ["TESTBENCH_ALLOW_NONLOOPBACK"] = "1"
        with mock.patch.object(subprocess, "run") as run, mock.patch(
            "platform.system", return_value="Linux"
        ):
            sys.argv[:] = ["testbench_run.py", "0.0.0.0", "9000", "4"]
            testbench_run.start_server()  # must NOT SystemExit
            self.assertTrue(run.called)
