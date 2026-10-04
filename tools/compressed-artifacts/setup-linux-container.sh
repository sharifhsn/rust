#!/bin/bash
# Recreates the Linux benchmark container used by census_benchmark.py: packages, stage1
# compilers for this branch and its upstream base, a release Wild, and the pinned
# Bevy/Polars/Nushell sources. Run on the macOS host from the repository root:
#
#   tools/compressed-artifacts/setup-linux-container.sh [container-name]
#
# Everything the benchmarks need lives in the container's /tmp; the repository and the
# Wild checkout are bind-mounted, so nothing here writes outside them except the
# lockfiles it saves under docs/target-directory-size/measurements/setup/.
set -euo pipefail

name=${1:-codex-rust-target-size-20260927}
rust_repo=$(cd "$(dirname "$0")/../.." && pwd)
wild_repo=${WILD_REPO:-$rust_repo/../wild}
saved=$rust_repo/docs/target-directory-size/measurements/setup

if ! docker inspect "$name" >/dev/null 2>&1; then
  docker run -d --name "$name" -v "$rust_repo:/rust" -v "$wild_repo:/wild" \
    rust:1.98.1-bookworm sleep infinity
fi

run() { docker exec "$name" bash -c "$1"; }

# Packages: linkers, debuggers, the x86-64 cross toolchain, and amd64 runtime libraries
# so cross-built binaries run under Rosetta.
run 'set -e
dpkg --add-architecture amd64
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
  clang lld mold gdb gdb-multiarch python3 git curl pkg-config libssl-dev cmake ninja-build \
  gcc-x86-64-linux-gnu g++-x86-64-linux-gnu libc6-dev-amd64-cross \
  libc6:amd64 libgcc-s1:amd64 libstdc++6:amd64 \
  libwayland-dev libasound2-dev libudev-dev libxkbcommon-dev \
  libwayland-dev:amd64 libasound2-dev:amd64 libudev-dev:amd64 libxkbcommon-dev:amd64 >/tmp/apt.log
chmod 1777 /tmp'

# Upstream base: a copy outside /rust, because building inside the repository makes Cargo
# treat bootstrap as a member of the parent workspace.
base=$(git -C "$rust_repo/.upstream-base" rev-parse HEAD)
run "set -e
if [ ! -d /tmp/upstream-src ]; then
  git clone -q --no-checkout /rust /tmp/upstream-src
  git -C /tmp/upstream-src checkout -q $base
fi"

# Stage1 compilers with aarch64 and x86-64 std. The branch builds from /rust directly.
run 'cd /rust && python3 x.py build library --stage 1 \
  --config tools/compressed-artifacts/bootstrap-linux.toml --build-dir /tmp/rustc-linux-build -j 7 \
  >/tmp/rustc-build.log 2>&1 || { tail -30 /tmp/rustc-build.log; exit 1; }'
run 'cd /tmp/upstream-src && python3 x.py build library --stage 1 \
  --config /rust/tools/compressed-artifacts/bootstrap-linux.toml --build-dir /tmp/rustc-linux-upstream -j 7 \
  >/tmp/rustc-upstream-build.log 2>&1 || { tail -30 /tmp/rustc-upstream-build.log; exit 1; }'

# Release Wild from the bind-mounted checkout.
run 'cd /wild && CARGO_TARGET_DIR=/tmp/wild-release cargo build --release -p wild-linker -q 2>&1 | tail -5
ls -la /tmp/wild-release/release/wild'

# Pinned sources with the benchmark fixtures and lockfiles.
mkdir -p "$saved"
run 'set -e
mkdir -p /tmp/bench/sources /tmp/bench/targets
fetch() { # name url revision
  local dir=/tmp/bench/sources/$1
  if [ ! -d $dir ]; then
    git init -q $dir && git -C $dir fetch -q --depth 1 $2 $3 && git -C $dir checkout -q FETCH_HEAD
  fi
  echo $3 > $dir/.benchmark-revision
}
fetch bevy https://github.com/bevyengine/bevy 0f38358f573a7dc6ea961076f6151be662142010
fetch polars https://github.com/pola-rs/polars d7488c71ecfbc77790292ff5b365b991c08380ce
fetch nushell https://github.com/nushell/nushell 2459fdd134ea4fdbae42efd6924e2b41201cf363
cp /rust/tools/compressed-artifacts/large-fixtures/bevy.rs /tmp/bench/sources/bevy/examples/compression_probe.rs
mkdir -p /tmp/bench/sources/polars/crates/polars/examples
cp /rust/tools/compressed-artifacts/large-fixtures/polars.rs /tmp/bench/sources/polars/crates/polars/examples/compression_probe.rs
cd /tmp/bench/sources/bevy && [ -f Cargo.lock ] || cargo generate-lockfile -q
for p in bevy polars nushell; do (cd /tmp/bench/sources/$p && CARGO_HOME=/root/.cargo cargo fetch -q --locked); done'
for p in bevy polars nushell; do
  docker cp "$name:/tmp/bench/sources/$p/Cargo.lock" "$saved/$p.Cargo.lock"
done
echo "container $name ready"
