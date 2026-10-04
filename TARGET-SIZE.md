# Rust target size: prototypes and measured results

Sharif Haason, October 4, 2026.

I built two separate prototypes to reduce Rust build storage.
Both preserve full debug information and incremental compilation in the comparisons below.

## 1. Cargo: remove artifacts for obsolete configurations

Cargo retains the complete graphs that active sessions, published outputs, and registered executions still need.
After a successful configuration change, it removes recorded units that have no live owner.
A compilation failure preserves the previous successful session roots.

| Project | Configurations | Ordinary target | Active target | Saved | First return, ordinary → active |
|---|---:|---:|---:|---:|---:|
| Bevy | 6 | 100.48 GiB | 34.62 GiB | 65.5% | 31.97 → 263.32 s |
| Polars | 3 | 26.63 GiB | 9.83 GiB | 63.1% | 24.61 → 157.31 s |
| Nushell | 3 | 30.13 GiB | 11.32 GiB | 62.4% | 22.71 → 172.81 s |
| Ruff | 3 | 14.70 GiB | 5.64 GiB | 61.6% | 5.07 → 61.72 s |
| uv | 3 | 28.55 GiB | 11.11 GiB | 61.1% | 6.33 → 123.93 s |

Each paired history used identical sources, source edits, configurations, compiler, Cargo binary, and CPU.
The targets started empty, so these results do not establish cleanup of arbitrary old targets.
The scopes cover selected applications, library tests, and transitive dependencies, rather than every workspace target.

The storage tradeoff is explicit: a return to an evicted configuration recompiles its graph.
The next build reused all artifacts in both arms for all five projects.
Five interleaved warm samples per operation and arm also reused all compiler artifacts for check, build, test, and Clippy.
Warm median differences were approximately zero to 50 ms, with approximately 50 ms measurement steps.
These measurements do not establish a statistical speed improvement.

The Bevy/Polars/Nushell run completed 326 commands and 45 exact runtime oracles.
The Ruff/uv run completed 201 commands and 26 exact runtime oracles.
The reports retain deliberate compile failures, upstream test limitations, and repaired environment failures.

- [Cargo implementation](https://github.com/sharifhsn/cargo/tree/codex/active-artifacts).
- [Retention module](https://github.com/sharifhsn/cargo/blob/codex/active-artifacts/src/compiler/active_artifacts.rs).
- [29 focused Cargo regressions](https://github.com/sharifhsn/cargo/blob/codex/active-artifacts/tests/testsuite/active_artifacts.rs).
- [Small offline reproduction](tools/artifact-retention/reviewer-repro.py).
- [Bevy, Polars, and Nushell report](docs/target-directory-size/config-change-tests-2026-10-03.md).
- [Ruff and uv report](docs/target-directory-size/astral-retention-results-2026-10-04.md).
- [Machine-readable metrics and warm samples](docs/target-directory-size/public-results/retention-metrics.json).

The Cargo code exactly matches the previously tested patch.
It uses provisional nightly flags, `-Zactive-artifacts` and `-Zbuild-dir-new-layout`.
It needs design review, a platform lifetime contract, and a port to an agreed Cargo revision before upstream submission.
Native Windows lifetime and full-workspace concurrency coverage remain incomplete.
Temporary graph replacement needs space for both configurations before collection.

## 2. rustc and Wild: compact compiler artifacts

This prototype combines compressed objects and metadata, shared object/debug storage, and smaller incremental compiler state.
Wild reads the compact library representation directly, which avoids an expanded archive before the link.
The compiler rebuilds a dependency graph lookup index in memory rather than store that index on disk.

| Project | Ordinary target | Compact target | Saved |
|---|---:|---:|---:|
| Bevy | 12.514 GiB | 2.582 GiB | 79.4% |
| Polars | 8.145 GiB | 1.489 GiB | 81.7% |
| Nushell | 5.850 GiB | 1.498 GiB | 74.4% |

These are separate September 28 experiments, with different workloads and source snapshots from the retention table.
Do not add the savings or infer a measured combined result.
Both arms used Zstd final DWARF with full debug information and incremental compilation enabled.
Clean builds took 9–22% longer in single comparisons on a shared host.
Some edited builds and relinks were faster, but those timings do not establish a general speed improvement.

- [Measured compiler snapshot](https://github.com/sharifhsn/rust/tree/codex/compact-artifacts-2026-09-28).
- [Measured Wild snapshot](https://github.com/sharifhsn/wild/tree/codex/compact-artifacts-2026-09-28).
- [Current compiler prototype](https://github.com/sharifhsn/rust/tree/codex/target-size-research).
- [Current Wild prototype](https://github.com/sharifhsn/wild/tree/codex/target-size-research).
- [Detailed redesign report](docs/target-directory-size/redesign-2026-09-28.md).
- [Direct format and build instructions](tools/compressed-artifacts/COMPACT-WILD.md).
- [Compiler metrics](docs/target-directory-size/public-results/compact-metrics.json).

The current compiler snapshot includes later linker-neutral experiments beyond the measured compact format.
Those later experiments do not yet have equivalent end-to-end validation.
The publication retains the measured snapshots separately.
The producer and consumer share a private experimental format through Rust and Wild checkouts in adjacent directories.
This is a prototype for review, with source pins in [publication provenance](docs/target-directory-size/public-results/provenance.json).

## Reproduction entry points

Clone the Cargo fork and the Rust fork for the small retention reproduction.
The Cargo guide includes its build and test commands.

```sh
git clone --single-branch --branch codex/active-artifacts https://github.com/sharifhsn/cargo.git cargo
git clone --single-branch --branch codex/target-size-research https://github.com/sharifhsn/rust.git rust
```

For the compact compiler experiment, use the two measured snapshot branches in adjacent directories.
The [snapshot guide](https://github.com/sharifhsn/rust/blob/codex/compact-artifacts-2026-09-28/TARGET-SIZE.md) gives clone commands and the build procedure.
Large measurements need separate compute and substantial disk space.

## Relation to Charlie Marsh's work

Charlie reported a Rust toolchain fork with 33% faster session replay and 40–60% less disk use.
His [October 3 post](https://x.com/charliermarsh/status/2106464412630475223) did not disclose its exact projects, revisions, or replay trace.
His earlier [Rust PR #162240](https://github.com/rust-lang/rust/pull/162240) names Ruff and uv.
That earlier PR led me to include both projects in the retention experiment.
These measurements do not reproduce his unpublished fork experiment.

The useful comparison is the mechanism: session retention, artifact representation, and their interaction with Cargo's cache work.
The next discussion can identify overlap and which smaller change belongs upstream first.

## Publication and provenance

This is an LLM-assisted research prototype in personal forks, with LLM-assisted documentation.
It is not an upstream proposal or PR, and no upstream reviewer agreed to review it.
The Rust and Cargo contribution policies require separate authorship and reviewer steps before upstream submission.

The public metrics are excerpts from preserved command receipts and file censuses.
The complete raw archives remain private and passed SHA-256 readback and restore checks.
The forks contain code, small metrics, source identities, and methods rather than private infrastructure records or build payloads.
No compiler or application binary changed for this publication, and this publication ran no new large benchmark.
