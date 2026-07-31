# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import platform
import subprocess
import sys

import waitress

from testbench_waitress import testbench_create_server

logger = logging.getLogger("waitress")
logger.setLevel(logging.INFO)


def start_server():
    if len(sys.argv) == 4:
        sock_host = sys.argv[1]
        sock_port = int(sys.argv[2])
        num_of_threads = int(sys.argv[3])
        sys.argv.clear()

        if (
            os.environ.get("TESTBENCH_STORE") == "file"
            and os.environ.get("TESTBENCH_ALLOW_NONLOOPBACK") != "1"
            and sock_host not in ("127.0.0.1", "localhost", "::1")
        ):
            raise SystemExit(
                "file backend refuses non-loopback bind host %r "
                "(set TESTBENCH_ALLOW_NONLOOPBACK=1 for the container case)" % sock_host
            )

        if platform.system().lower() == "windows":
            print("Starting waitress server")
            # Imported lazily HERE (not at module top) so the POSIX launcher does
            # NOT import `testbench`. Importing it runs rest_server's module body
            # (`db = _init_db_from_env()`), which under TESTBENCH_STORE=file claims
            # the single-worker lock. On POSIX the launcher then spawns gunicorn,
            # whose worker imports `testbench` afresh and must be the ONE process
            # to claim the lock; a launcher-side claim would collide with its own
            # (grand)child worker. Windows serves in this same process, so the
            # import (and its single lock claim) belongs right here.
            import testbench

            waitress.serve(
                testbench.run(),
                _server=testbench_create_server,
                host=sock_host,
                port=sock_port,
                threads=num_of_threads,
            )
        else:
            print("Starting gunicorn server")
            subprocess.run(
                [
                    "gunicorn",
                    f"--bind={sock_host}:{sock_port}",
                    "--workers=1",  # file backend: one index per root
                    "--worker-class=sync",
                    f"--threads={num_of_threads}",
                    "--reload",
                    "--access-logfile=-",
                    "testbench:run()",
                ]
            )

    else:
        print(
            "Invalid number of arguments. Please provide 'testbench_run.py <hostname> <port> <number of threads>'."
        )


if __name__ == "__main__":
    start_server()
