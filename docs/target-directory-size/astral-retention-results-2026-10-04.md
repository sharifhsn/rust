# Cargo retention results: Ruff and uv

The Ruff and uv comparisons completed on October 4, 2026.
Active retention reduced target allocation by 61.1% to 61.6% across three configuration changes.
Both arms kept full debug information and incremental compilation enabled.

## Target allocation

| Project | Ordinary target | Active target | Saved | Complete unit directories, ordinary → active |
|---|---:|---:|---:|---:|
| Ruff | 14.70 GiB | 5.64 GiB | 61.6% | 2,097 → 699 |
| uv | 28.55 GiB | 11.11 GiB | 61.1% | 3,594 → 1,198 |

The table uses deduplicated allocation from the final file censuses.
Ruff's active census added 4 KiB after its original command inventory.
The other three censuses had no difference.
The original rows remain unchanged.

Incremental allocation fell from 4.43 to 2.32 GiB for Ruff and from 9.56 to 4.92 GiB for uv.
The active targets contained zero known inactive units at the final history boundary.
Retention started with empty targets.
Cleanup of arbitrary old targets remains outside this experiment.

## Time and configuration returns

| Project | History check/build/test timers, ordinary → active | First return, ordinary → active |
|---|---:|---:|
| Ruff | 358.11 → 360.31 s | 5.07 → 61.72 s |
| uv | 681.05 → 700.76 s | 6.33 → 123.93 s |

Each history contains the same default, portable-v2, and frame-pointer configurations and the same source edits.
These totals describe one ordered trace on one host.
They do not establish a statistical speed effect or an isolated clean-build distribution.

On the first return, ordinary retention reused 377/379 Ruff artifacts and 606/607 uv artifacts.
Active retention rebuilt the evicted graph and reused zero artifacts.
The next build reused all artifacts in both arms for both projects.
The larger first return cost is the main tradeoff between storage and time.

## Warm command distributions

Each operation has five interleaved samples per arm after two preparation cycles.
All measured compiler artifacts were fresh.
Here, fresh means that Cargo reused the artifact without compilation.

| Project | Operation | Ordinary median, s | Active median, s |
|---|---|---:|---:|
| Ruff | check | 0.365 | 0.415 |
| Ruff | build | 0.365 | 0.365 |
| Ruff | test, compile only | 0.365 | 0.365 |
| Ruff | clippy | 0.415 | 0.415 |
| uv | check | 0.565 | 0.615 |
| uv | build | 0.565 | 0.565 |
| uv | test, compile only | 0.565 | 0.615 |
| uv | clippy | 0.615 | 0.665 |

The medians differ by approximately zero to 50 milliseconds.
The process wait and monitor produce approximately 50-millisecond steps in these short measurements.
A precise sub-50-millisecond overhead remains unknown.
The [public metrics](public-results/retention-metrics.json) retain all raw warm samples.

## Correctness and preserved failures

The final tree contains 201 command receipts, with 198 zero exits.
All 26 runtime oracles succeeded.
Ruff passed 27 selected library tests per arm, and uv passed 37 per arm.
Both suites had zero ignored tests.

Ruff produced the exact oracle `ruff: clean=0 undefined=F821`.
uv created an offline virtual environment and produced `uv: offline-venv python=42`.
Each oracle receipt records the actual application binary SHA-256.

Two deliberate compile failures preserved the successful session roots and their unit directories.
Both recovery builds and runtime oracles succeeded.
Both next builds reused all artifacts.

The third nonzero command was an earlier Ruff library-test environment failure.
Root permissions bypassed a fixture for an unreadable file, and `RUST_BACKTRACE=1` changed a panic snapshot.
The corrected library-test environment disabled both DAC bypass capabilities and set `RUST_BACKTRACE=0` equally in both arms.
All selected tests then passed without exclusions.
This change did not affect the history, warm, or first-return environments.

The first uv setup attempt used the wrong source entry path and stopped before any uv measurement.
The adapter then used the pinned repository's actual path, `crates/uv/src/bin/uv.rs`.
Both complete failed publications and every original command row remain in the final tree.
The archive also retains a publisher permission failure and local observer failures.

## Scope and provenance

This experiment applies our Cargo retention prototype to Ruff and uv.
Charlie Marsh named these projects in [Rust PR #162240](https://github.com/rust-lang/rust/pull/162240).
His [October 3 post](https://x.com/charliermarsh/status/2106464412630475223) did not disclose the exact crate suite, revisions, or replay trace.
This result does not reproduce that unpublished compiler fork experiment.
Its savings remain separate from Charlie's measurements and our compiler/Wild prototype.

The selection includes each application binary, its transitive dependencies, and its library test target.
It does not cover every workspace package or feature, full-workspace concurrency, or native Windows process lifetime.
The driver did not measure a complete transient peak during graph replacement.
Temporary replacement headroom remains necessary.

- Ruff revision: `1df6db3e463ffa1b587dcf47f25360d40389b0f7`, version 0.16.10.
- uv revision: `46b84fd0bfec23b72f29e8e2185ba68a65052f48`, version 0.12.23.
- Compiler: Rust 1.99.0, revision `b940084d7eb6a299eb4bfeb8e34901bc051e7ac4`, LLVM 23.1.1, x86_64 Linux.
- Cargo base: `4f3fb2428282fb2467c4bb5be0163d2f42a31491`.
- Cargo binary SHA-256: `7f637e19e1e24b8900028422c5b8cb2cd7531c33243fe8a744089538dcbd22fd`.
- Retention patch SHA-256: `c74be69ecb97bf6c78fbf4a36d3782700836d1ed6501aabf62b85e4434103ff4`.
- Frozen driver SHA-256: `d78056f55dd137aecfcce9fa481c57ebea6ea6f42b8128d8c0ca49a78e8658e1`.
- CPU: `Intel(R) Xeon(R) Platinum 8259CL CPU @ 2.50GHz`.
- Boot: `47fbe2ba-ee39-4dd8-9159-a4ee571b3999`.

Both arms used the same patched Cargo binary, compiler, CPU, locked sources, and configuration trace.
The control arm disabled active retention.
The active arm enabled `-Zactive-artifacts` and its artifact session.
The application profiles used debug level 2, incremental compilation, 16 codegen units, assertions, and lld.
Both arms overrode the repositories' default debug profiles equally.

Compiler construction occurred before application measurements.
The native Cargo retention suite passed all 29 tests before those measurements.


The complete raw archives passed SHA-256 readback and restored-tree checks.
They remain private. The public metrics are excerpts from those receipts.
