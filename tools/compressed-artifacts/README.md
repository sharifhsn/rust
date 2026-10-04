# Native compressed Rust artifacts prototype

**September 27 update:** [Direct compact artifacts in Wild](COMPACT-WILD.md)
implements a new `.rlib` manifest, shared compressed object storage, a native
Wild reader and demand-read `.rmeta` bytes. It removes expanded Rust linker
inputs and shares objects with incremental compilation. That report contains
the Linux Bevy comparison, debug-info checks and current implementation limits.

The earlier prototype and its original benchmark campaigns follow below.

Implements the native prototype from the
[no-debug design](../../docs/target-directory-size/no-debug-decision.md).
`-Zcompress-artifacts` writes compressed `.rmeta` and `.rlib` files; patched rustc
reads them and prepares conventional temporary archives for the system linker.
The comparison baseline uses the same patched compiler with compression disabled
and metadata stored once. No compiler wrapper archives the target after a build.

See [the profiles, performance and incremental results](PHASE2.md) and
[large-project validation on Bevy, Polars and Nushell](LARGE-PROJECTS.md).
The prototype now includes independent artifact/cache switches and configurable
compression profiles, with repeated whole-build and codec measurements.

## Implementation boundary

- Versioned indexed format with independently compressed Zstandard chunks,
  raw fallback, corruption checks, validated lengths, and atomic replacement.
- Shared `fast`, `balanced`, and `small` profiles, plus level/chunk overrides.
  Contexts and buffers are reused; AArch64 uses hardware IEEE CRC when available.
- `-Zcompress-incremental` separately compresses dependency graphs, query caches,
  cached codegen outputs and LTO data, retaining the compiler's reuse decisions.
- The original campaigns used a contiguous decoded metadata buffer. The current
  checkout adds a demand-read metadata interface; the Wild report measures that
  implementation with a new compiler binary.
- Metadata is compressed before the producer publishes its ready notification.
- Archive metadata and LLVM LTO readers accept the format. Existing system
  linkers receive temporary conventional archives, preserving archive-member
  selection. These temporary bytes count in peak-storage measurement.
- Executables, proc-macro dynamic libraries, other dynamic libraries and final
  static libraries keep their ordinary formats.
- Normal and error-path cleanup owns temporary files. Abrupt process termination
  can leave temporary files; automatic recovery and garbage collection are not
  implemented.

The codec uses the installed system libzstd through its C API. The initial
configuration targets native Apple Silicon; this is not an upstream portability
or dependency-integration proposal. The new flag participates in configuration
hashes, so switching it changes artifact identity. Normal uncompressed inputs
continue to work.

## Build

The checked-in configuration builds an optimized stage1 compiler and its standard
library, using the checkout's pinned bootstrap compiler and CI LLVM. It writes
only its own compiler build tree under `/tmp/rust-compressed-artifacts-bootstrap`.

```sh
rtk proxy uv run --python /opt/homebrew/bin/python3 \
  tools/compressed-artifacts/build.py --out /absolute/new-build-record-directory
```

The helper records the Git revision, changed source hashes, command, complete log,
result, built compiler identity and driver hashes. Source edits during compilation
make its provenance check fail; rerun before benchmarking that binary.

The actual bootstrap command is:

```sh
rtk proxy uv run --python /opt/homebrew/bin/python3 x.py build \
  --config tools/compressed-artifacts/bootstrap.toml \
  --stage 1 compiler/rustc library/std
```

The source flag is local to this patched compiler. Point Cargo at the resulting
`stage1/bin/rustc`; use matching standard-library output and a fresh target.
The bootstrap Cargo is beta and currently spells its metadata-separation option
`-Zno-embed-metadata`. Its use requires an explicit experimental subprocess
configuration. This is separate from rustc's `-Zcompress-artifacts`.

For a separate project, use this compiler and a fresh target directory:

```sh
RUSTC=/tmp/rust-compressed-artifacts-bootstrap/aarch64-apple-darwin/stage1/bin/rustc \
RUSTC_BOOTSTRAP=1 CARGO_INCREMENTAL=0 CARGO_PROFILE_DEV_DEBUG=0 \
RUSTFLAGS='-Cdebuginfo=0 -Zcompress-artifacts=yes' \
  /tmp/rust-compressed-artifacts-bootstrap/aarch64-apple-darwin/stage0/bin/cargo \
  -Zno-embed-metadata build --target-dir target-compressed
```

The reader accepts old and new artifacts. Enabling the flag in a used target does
not remove artifacts from earlier configurations; the fresh target keeps those
bytes out of this comparison. The prototype does not change global Cargo settings.

For incremental development builds, set `CARGO_INCREMENTAL=1` and use
`RUSTFLAGS='-Cdebuginfo=0 -Zcompress-artifacts=yes -Zcompress-incremental=yes -Zartifact-compression-profile=fast'`.
The shared profile does not enable compression on its own. Profile overrides are
`-Zartifact-compression-level=-5..19` and
`-Zartifact-compression-chunk-size=16384..4194304` (power-of-two bytes).
`legacy` retains level 3 / 64 KiB for comparison. Compiler readers accept all
supported geometries; old prototype readers only understand their 64 KiB format.

For the lower-overhead cache-only setting, keep `CARGO_INCREMENTAL=1` and use
`RUSTFLAGS='-Cdebuginfo=0 -Zcompress-artifacts=no -Zcompress-incremental=yes -Zartifact-compression-profile=balanced'`.
The recorded four-consumer measurements save 37–42% of retained target allocation
with 1–7% semantic-edit overhead; this remains an opt-in local compiler experiment.
The [larger Bevy, Polars and Nushell runs](LARGE-PROJECTS.md) save 24.5–35.2%
with this cache-only setting. Combined balanced saves 51.5–56.7% there, with
roughly 1–2.3 seconds added to the measured leaf edits. Clean timings on the
shared host vary; see the full repeated results before choosing a profile.

## Validation and measurement

The codec has standalone unit tests in
[`artifact_compression.rs`](../../compiler/rustc_data_structures/src/artifact_compression.rs).
The focused compiler regression suite lives at
[`tests/run-make/compressed-artifacts`](../../tests/run-make/compressed-artifacts).
The benchmark harness records fresh, no-op and edited builds, runtime output,
exact commands, logical and allocated byte counts, sampled peak bytes, wall/CPU
time, and sampled process memory.

```sh
rtk proxy uv run --python /opt/homebrew/bin/python3 x.py test \
  --config tools/compressed-artifacts/bootstrap.toml \
  --stage 1 tests/run-make/compressed-artifacts tests/run-make/compressed-incremental

rtk proxy uv run --no-project --python /opt/homebrew/bin/python3 --with psutil \
  tools/compressed-artifacts/benchmark.py \
  --rustc /tmp/rust-compressed-artifacts-bootstrap/aarch64-apple-darwin/stage1/bin/rustc \
  --cargo /tmp/rust-compressed-artifacts-bootstrap/aarch64-apple-darwin/stage0/bin/cargo \
  --out /absolute/new-measurement-directory \
  --repetitions 3 --sample-interval 0.05 --include-embedded-baseline
```

Use `--profile release` or `--workloads regex syn` to select the optimized profile
or a subset. Each measured run has fresh copied source and its own target. The
edit appends a comment to the consumer's main source; it forces recompilation
while leaving its runtime result fixed. The small synthetic fixture includes a
proc macro, bundled C code, a unit test, and two integration-test executables.
It has no doctests; rustdoc behavior is outside this benchmark.

The profile matrix uses `--arms dedup_off legacy fast balanced small`. Incremental
matrices use `--incremental --semantic-edits 2` with
`--arms dedup_off balanced incremental_only both_fast both_balanced both_small`.
Each semantic edit changes a printed value; the runner verifies the new output.
`analyze-phase2.py /absolute/measurement-directory` reports paired build costs,
retained allocation and packed cache classes. Run it before deleting targets if
artifact-header inspection is needed. The codec screen and research live in
[compression-methods.md](../../docs/target-directory-size/compression-methods.md).

The harness checks recorded compiler/driver hashes, captures dependency revisions
before and after each run, audits compiler flags, and requires zero compiler
invocations for no-op builds. Its per-run JSON, raw logs, process/disk samples, and
file censuses are retained. Child CPU uses `getrusage(RUSAGE_CHILDREN)` deltas;
summed process RSS and storage peaks are sampled lower bounds.

Allocation accounting counts each inode once. It does not resolve APFS shared
extents; sampled peaks are lower bounds. Build monitoring adds overhead to both
arms, and the machine is not isolated from other activity. Interpret repeated
paired results for these workloads, not as universal compiler performance.

See [the original implementation results](RESULTS.md). That version passed its
compiler and codec tests and 63 benchmark runs. Four development consumers retained
36–46% fewer allocated file bytes beyond metadata deduplication, with 8–23% clean
build and 8–21% edit rebuild overhead. It misses the proposed 5% time-overhead gate
and remains opt-in. The earlier capacity-only measurements remain separate.
