#!/bin/bash
# Checks that compact libraries survive moving the directory that holds them and that
# Cargo-style hard-linked aliases resolve. Runs in the Linux benchmark container.
set -euo pipefail
RUSTC=${RUSTC:-/tmp/rustc-linux-build/aarch64-unknown-linux-gnu/stage1/bin/rustc}
WILD_BIN=${WILD_BIN:-/tmp/bench/wild-plain}
root=$(mktemp -d /tmp/compact-relocation.XXXXXX)
trap 'rm -rf "$root" "$root-moved"' EXIT
mkdir -p "$root/target/debug/deps"
cd "$root"
printf '#[inline(never)] pub fn base() -> u64 { 2 }\npub fn answer<T: Into<u64>>(x: T) -> u64 { x.into() + base() }\n' > lib.rs
printf 'fn main() { assert_eq!(dep::answer(40u32), 42); println!("ok"); }\n' > main.rs
flags=(-Zcompress-artifacts=yes -Zcompress-incremental=yes -Zembed-metadata=no -Cdebuginfo=2
       -Clinker=clang -Clink-arg=-fuse-ld=wild)
export PATH="$WILD_BIN:$PATH" RUSTC_BOOTSTRAP=1
deps="$root/target/debug/deps"
"$RUSTC" lib.rs --crate-name dep --crate-type rlib --emit=link,metadata -C extra-filename=-h \
  --out-dir "$deps" "-Zcompact-artifact-store=$root/target/compact-store" "${flags[@]}"
head -c 8 "$deps/libdep-h.rlib" | grep -q RCLIB001
if grep -aq "$root" "$deps/libdep-h.rlib"; then
  echo "manifest still contains an absolute path" >&2
  exit 1
fi
# Cargo's uplifted alias.
ln "$deps/libdep-h.rlib" "$root/target/debug/libdep.rlib"
link() { # target-dir extern-path output
  "$RUSTC" main.rs --extern "dep=$2" -L "dependency=$1/debug/deps" -o "$3" \
    "-Zcompact-artifact-store=$1/compact-store" "${flags[@]}"
  test "$("$3")" = ok
}
link "$root/target" "$root/target/debug/deps/libdep-h.rlib" "$root/app"
link "$root/target" "$root/target/debug/libdep.rlib" "$root/app-alias"
mv "$root" "$root-moved"
cd "$root-moved"
link "$root-moved/target" "$root-moved/target/debug/deps/libdep-h.rlib" "$root-moved/app-moved"
link "$root-moved/target" "$root-moved/target/debug/libdep.rlib" "$root-moved/app-moved-alias"
mv "$root-moved" "$root"
echo "relocation checks passed"
