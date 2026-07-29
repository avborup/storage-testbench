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
{
  description = "Development environment for the GCS storage testbench";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            # 3.12 matches the Dockerfile base image. The CI matrix also
            # covers 3.8-3.11; use nix-shell -p python38 to reproduce those.
            pkgs.python312
            pkgs.docker-client
            pkgs.docker-compose
            pkgs.gnumake
            # `make verify-linux` fetches its Linux image with skopeo on the
            # host, because the Docker VM may have no network egress even when
            # the host shell does.
            pkgs.skopeo
            pkgs.curl
            pkgs.jq
          ];

          shellHook = ''
            # Dependency versions are pinned in setup.py and must match what
            # the Dockerfile and CI install, so they come from pip rather than
            # nixpkgs. Nix supplies the interpreter and the toolchain; the venv
            # supplies exact versions. Re-provision only when setup.py changes.
            export VENV=.venv
            stamp="$VENV/.provisioned"
            want=$(cksum setup.py | cut -d' ' -f1)
            if [ ! -f "$stamp" ] || [ "$(cat "$stamp")" != "$want" ]; then
              echo "provisioning $VENV from setup.py ..."
              ${pkgs.python312}/bin/python3 -m venv "$VENV" \
                && "$VENV/bin/pip" install --quiet --upgrade pip \
                && "$VENV/bin/pip" install --quiet -e . \
                && "$VENV/bin/pip" install --quiet \
                     pytest pytest-cov coverage requests \
                     black==22.3.0 isort==5.12.0 \
                && echo "$want" > "$stamp"
            fi
            source "$VENV/bin/activate"
            export PYTHONPATH="$PWD"
            echo "storage-testbench devShell: python $(python3 --version 2>&1 | cut -d' ' -f2)"
          '';
        };
      });
}
