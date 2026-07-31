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

# Reproduce the CI conformance gate on Linux, from a non-Linux workstation.
#
# Why this exists: the conformance goldens are captured on a developer's
# machine, but the gate that enforces them runs on Linux. At least one
# recorded value has already differed between the two -- `gzip.compress`
# writes the zlib build's OS byte into the gzip header (0x13 on Darwin, 0x03
# on Linux, 0xff on Python <= 3.10), which changed a stored object's crc32c
# and md5Hash and reddened CI on a golden that passed locally. That specific
# case is now fixed by committing a literal payload, but the class of bug
# recurs, so `make verify-linux` makes the Linux check a routine local step
# rather than something to rediscover by hand.
#
# Run it before committing regenerated goldens.

PYTHON_VERSION ?= 3.12
PYTHON_IMAGE   ?= python:$(PYTHON_VERSION)-slim

# The image and the wheels must match the container's architecture, which is
# the Docker VM's, not necessarily the host's. The value is filtered to known
# architectures so that a daemon error printed on stdout cannot end up
# embedded in a path, and falls back to the host's own arch rather than a
# hardcoded guess.
DOCKER_ARCH ?= $(shell docker version --format '{{.Server.Arch}}' 2>/dev/null \
    | grep -Ex 'arm64|aarch64|amd64|x86_64' || uname -m)
ifneq ($(filter arm64 aarch64,$(DOCKER_ARCH)),)
PLATFORM_TAGS := --platform manylinux_2_17_aarch64 --platform manylinux2014_aarch64
SKOPEO_ARCH   := arm64
else
PLATFORM_TAGS := --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64
SKOPEO_ARCH   := amd64
endif

# The wheel cache must live under $(HOME): colima mounts only the home
# directory into its VM, so a cache in /tmp is invisible to containers.
WHEEL_DIR ?= $(HOME)/.cache/storage-testbench-linux-wheels/$(DOCKER_ARCH)-py$(PYTHON_VERSION)

# Host-side pip and python. The devShell's venv if present, else system pip.
PIP ?= $(shell test -x .venv/bin/pip && echo .venv/bin/pip || echo pip3)
HOST_PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)

.PHONY: verify-linux linux-image linux-wheels clean-linux-cache

## verify-linux: run the conformance gate inside a Linux container
verify-linux: linux-image linux-wheels
	@echo "==> running the conformance gate on linux/$(DOCKER_ARCH), python $(PYTHON_VERSION)"
	@docker run --rm \
	    -v "$(CURDIR)":/src:ro \
	    -v "$(WHEEL_DIR)":/wheels:ro \
	    $(PYTHON_IMAGE) bash -c '\
	        set -e; \
	        mkdir -p /work; \
	        cd /src && tar cf - --exclude=./.venv --exclude=./.git . | (cd /work && tar xf -); \
	        cd /work; \
	        python -c "import sys, platform, zlib; print(\"platform:\", sys.version.split()[0], platform.machine(), platform.system(), \"zlib\", zlib.ZLIB_VERSION)"; \
	        pip install -q --root-user-action=ignore --no-index --find-links=/wheels -r /wheels/requirements.txt; \
	        pip install -q --root-user-action=ignore --no-index --no-deps --no-build-isolation -e .; \
	        PYTHONPATH=. python -m tests.conformance.harness'

# The Docker VM may have no network egress even when the host shell does, so
# the image is fetched host-side with skopeo and loaded over the daemon
# socket rather than pulled by the daemon itself.
linux-image:
	@if docker image inspect $(PYTHON_IMAGE) >/dev/null 2>&1; then \
	    echo "==> $(PYTHON_IMAGE) already loaded"; \
	else \
	    set -e; \
	    echo "==> fetching $(PYTHON_IMAGE) for linux/$(SKOPEO_ARCH) via skopeo"; \
	    tmp=$$(mktemp -d); \
	    skopeo --insecure-policy copy \
	        --override-os linux --override-arch $(SKOPEO_ARCH) \
	        docker://docker.io/library/$(PYTHON_IMAGE) \
	        docker-archive:$$tmp/image.tar:$(PYTHON_IMAGE); \
	    docker load -i $$tmp/image.tar; \
	    rm -rf $$tmp; \
	fi

# Dependencies are downloaded host-side for the same reason, then installed
# with --no-index inside the container. Note grpcio's aarch64 wheel is tagged
# only manylinux_2_17_aarch64 with no manylinux2014 alias, so omitting the
# former makes pip report "no matching distribution" for a version that does
# exist -- hence both tags above.
linux-wheels:
	@if [ -f "$(WHEEL_DIR)/.complete" ]; then \
	    echo "==> wheel cache present at $(WHEEL_DIR)"; \
	else \
	    set -e; \
	    echo "==> downloading linux/$(DOCKER_ARCH) wheels to $(WHEEL_DIR)"; \
	    mkdir -p "$(WHEEL_DIR)"; \
	    $(HOST_PYTHON) -c "import re; s = open('setup.py').read(); \
m = re.search(r'install_requires\s*=\s*\[(.*?)\]', s, re.S); \
deps = re.findall(r'\"([^\"]+)\"', m.group(1)) + ['requests', 'setuptools', 'wheel']; \
open('$(WHEEL_DIR)/requirements.txt', 'w').write('\n'.join(deps) + '\n')"; \
	    $(PIP) download -q -d "$(WHEEL_DIR)" \
	        $(PLATFORM_TAGS) \
	        --implementation cp --python-version $(subst .,,$(PYTHON_VERSION)) \
	        --abi cp$(subst .,,$(PYTHON_VERSION)) \
	        --only-binary :all: -r "$(WHEEL_DIR)/requirements.txt"; \
	    touch "$(WHEEL_DIR)/.complete"; \
	fi

## clean-linux-cache: drop the downloaded wheel cache
clean-linux-cache:
	rm -rf "$(WHEEL_DIR)"
