#!/bin/bash
set -Eeuo pipefail
# Upstream permission fixtures must respect mode bits even in the root bootstrap.
# Ruff's panic snapshot requires the default backtrace setting.
export RUST_BACKTRACE=0
exec setpriv --bounding-set=-dac_override,-dac_read_search \
  --inh-caps=-all --ambient-caps=-all --no-new-privs \
  "$(dirname "$0")/cargo-src/target/release/cargo" "$@"
