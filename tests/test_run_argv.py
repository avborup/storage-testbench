import subprocess
import sys
import unittest
from unittest import mock

import testbench_run


class TestRunArgv(unittest.TestCase):
    def test_gunicorn_argv_forces_single_worker(self):
        with mock.patch.object(subprocess, "run") as run, mock.patch(
            "platform.system", return_value="Linux"
        ):
            sys.argv[:] = ["testbench_run.py", "127.0.0.1", "9000", "4"]
            testbench_run.start_server()
            argv = run.call_args[0][0]
            self.assertIn("--workers=1", argv)
