#!/usr/bin/env python3
#
# Copyright 2026 Google LLC
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

"""Black-box conformance harness for the storage testbench.

Nothing in this package may import `testbench` or `gcs` internals, with the
sole exception of `emulator.py`, which needs a module path to launch a
subprocess, and the generated `google.storage.v2` gRPC stubs, which are the
public API surface rather than internals. The harness must measure external
behavior only, so that it stays valid across the refactors it exists to
police.
"""
