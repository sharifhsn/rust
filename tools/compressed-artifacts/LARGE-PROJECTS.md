# Large-project validation of compressed Rust artifacts

Scope: one branch, native Apple Silicon, development builds with Rust debug
information disabled. This extends [the profile and incremental-cache experiments](PHASE2.md)
to Bevy, Polars, and Nushell. Measurements finished September 26, 2026 local time.

**Combined artifact and incremental compression at balanced reduced retained
target allocation by 51.5–56.7% across these three workloads.** Cache-only
compression saved 24.5–35.2% with edit times much closer to the baseline. The
combined modes still add roughly 1–2.3 seconds to these small edited builds.
The storage benefit scales to these larger graphs; the edit-speed gap remains.

All **36 fresh-target runs, 144 runtime checks, and 36 no-op checks passed**.
The same successfully built compiler was used throughout, with no compiler or
fixture changes during the final matrices. These are local experiments with an
opt-in compiler prototype, not upstream Rust results.

## Results

### Retained targets

Medians of three repetitions after the second semantic edit, in allocated GiB.
Each final census confirms that the controlled temporary directory is empty.
Hardlinks are counted once; APFS clone-shared physical extents are not resolved.
Every baseline already stores metadata once and disables Rust debug information.

| Project | Compression off | Cache-only balanced | Both fast | Both balanced | Balanced saving |
|---|---:|---:|---:|---:|---:|
| Bevy | 5.529 | 3.584 | 2.569 | 2.392 | 56.74% |
| Polars | 3.742 | 2.564 | 1.787 | 1.688 | 54.88% |
| Nushell | 3.062 | 2.312 | 1.576 | 1.485 | 51.53% |

Savings are the median of paired per-repetition ratios. Fast saved 53.53%,
52.25%, and 48.54% respectively. Balanced saved another 0.09–0.18 GiB per target.
The `small` profile was not included in this large-project matrix, so balanced
is the smallest of the tested large-project modes, not a proven optimum.

### Build and edit times

Wall seconds, medians of three repetitions. Each cell is **clean build / second
semantic edit**. The edit changes the leaf program and checks its changed output;
it does not represent a deep change to Bevy's renderer or Polars' query engine.

| Project | Compression off | Cache-only balanced | Both fast | Both balanced |
|---|---:|---:|---:|---:|
| Bevy | 127.80 / 2.94 | 132.53 / 2.95 | 131.17 / 4.71 | 140.55 / 5.28 |
| Polars | 72.17 / 2.25 | 68.38 / 2.32 | 69.81 / 3.45 | 73.79 / 3.45 |
| Nushell | 83.99 / 2.16 | 86.98 / 2.35 | 86.54 / 3.23 | 96.98 / 3.85 |

Clean-build timings are noisy on this shared host. For example, uncompressed
Bevy took 125.27–170.90 seconds, and its second edit took 2.69–5.55 seconds.
Three repetitions do not justify interpreting a negative paired overhead as a
speedup or claiming the fast profile is free. The [generated tables](../../docs/target-directory-size/measurements/2026-09-26-large-projects/summary/tables.md)
retain all phases, CPU time, and paired timing ranges; the [JSON summary](../../docs/target-directory-size/measurements/2026-09-26-large-projects/summary/large-summary.json)
also includes every sample. Those ranges are sample extrema, not confidence
intervals.

The larger graphs make the remaining link/edit penalty more visible than the
earlier four-consumer campaign. For frequent leaf edits, cache-only is the
practical choice from these measurements. Combined balanced is the choice when
retained storage matters most. Fast recovers part of the Bevy and Nushell edit
latency while retaining most of the size reduction.

### Memory and temporary storage

Medians of sampled process-tree and disk peaks, in GiB. Each cell is
**compression off → both balanced**. Disk includes the target and controlled
temporary directory throughout the build.

| Project | Clean RSS peak | Second-edit RSS peak | Second-edit disk peak |
|---|---:|---:|---:|
| Bevy | 3.596 → 4.083 | 2.437 → 3.251 | 5.547 → 3.677 |
| Polars | 3.335 → 3.829 | 1.706 → 2.001 | 3.742 → 2.394 |
| Nushell | 3.111 → 3.661 | 1.382 → 1.870 | 3.064 → 1.961 |

The current adapter trades extra CPU and private decoded buffers for retained
storage. Temporary expansion is material: Bevy's balanced second-edit disk peak
was 3.677 GiB versus its 2.392 GiB retained target. This difference includes all
transient build output, not just archive staging. Sampling every 250 ms can miss
shorter peaks, and summed RSS can double-count shared pages. These measurements
do not isolate how many seconds belong to metadata decoding versus archive
materialization; the [source audit](../../docs/target-directory-size/decoding-and-linker-costs.md)
identifies the relevant paths and the instrumentation needed for that attribution.

### Compatibility and build coverage

Clean builds logged 422 rustc compilation commands for Bevy, 364 for Polars,
and 728 for Nushell, identical across modes and repetitions. Respectively 64,
35, and 42 commands enabled incremental codegen. Every no-op logged zero
recompilations. All edited programs produced their expected changed output;
source inputs were restored and checked after each run.

The fixtures exercise meaningful runtime paths, but this campaign does not run
the projects' complete test suites or verify GPU rendering. Incremental codegen
is enabled and its files are measured; the large leaf-edit runs do not directly
assert which query results or codegen units were reused. The earlier focused
compiler regression suite supplies direct incremental reuse checks.

## What is being measured

| Project | Pinned source | Build and runtime coverage |
|---|---|---|
| Bevy 0.19.1 | `b56fc29d3016e641754765244b5ba3f9cc504671` | Default `2d`, `3d`, `ui`, and `audio` features. The example retains the default plugin graph, then checks a finite headless ECS simulation: 1,000 entities, 30 updates, position sum 559,500. It does not initialize a window or exercise GPU rendering. |
| Polars 0.55.2 | `d7488c71ecfbc77790292ff5b365b991c08380ce` | Default features plus `lazy,parquet`. The example reads CSV, groups and sums through the lazy query engine, and verifies a Parquet round trip. |
| Nushell 0.116.0 | `2459fdd134ea4fdbae42efd6924e2b41201cf363` | The real `nu` application with default features. It evaluates a pipeline that squares 1 through 4 and checks the sum is 30. Workspace plugin binaries are outside this build. |

The [Luna Max workload research](../../docs/target-directory-size/large-project-selection.md)
records project-specific complaints, including Bevy and Polars, and additional
candidates such as Zed and Tauri. Public reports often include debug information,
multiple targets, or months of accumulated outputs. They motivate workload
selection; they are not baselines for this experiment. Nushell adds an application
with a different dependency graph; the search did not establish a first-party
target-size measurement for it.

## Method

- Same source-stable, successfully built stage1 compiler as the previous campaign:
  Rust 1.99.0-dev, LLVM 22.1.8, native `aarch64-apple-darwin`. The base revision is
  `b7b856c888bfc50d65a0f53f1188cf32cc75ed1d`; the patched source and binary hashes are
  pinned by [build 7](../../docs/target-directory-size/measurements/2026-09-26-compression/build-7/build.json).
- Apple M5 Pro, 48 GiB memory, APFS; four Cargo jobs. Project development
  optimization settings are retained. Rust debug information is forced to zero;
  incremental compilation is enabled. Encoded Rust flags replace repository
  target-specific Rust flags identically in every arm.
- Every arm stores metadata once using this Cargo's `-Zno-embed-metadata` option.
  Thus the baseline already receives metadata deduplication; the reported
  difference isolates compression on top of it.
- Four arms: compression disabled, incremental caches only at `balanced`, both
  artifacts and caches at `fast`, and both at `balanced`. Fast uses Zstd level -1;
  balanced uses level 3; both use independent 256 KiB chunks.
- Three fresh-target repetitions per arm, rotating arm order. Each run performs
  a clean build, a no-op, and two controlled leaf edits with checked runtime
  output. Registry dependencies follow Cargo's normal incremental policy;
  workspace path dependencies can retain incremental state.
- Sources and lockfiles are frozen before timing. Downloads are excluded; builds
  use `--offline --locked`. Bevy's generated lockfile is saved alongside the
  upstream Nu and Polars lockfiles. Each copied source tree is hashed and restored
  after the controlled edits; the original source trees must remain unchanged.
  A separate Git status/diff check confirms the original tracked files match
  their pinned commits; Bevy's generated lockfile is the only ignored addition.
- Each arm uses its own absolute source and output paths. Embedded paths can
  cause small byte/work differences beyond compression. Reported ranges include
  the observed variation; these are not byte-identical-output comparisons.
  Source fingerprints cover the supplied workspace trees, not Cargo's separate
  registry and Git caches.
- Every recorded rustc command is audited for the expected compiler, compression
  flags, debug setting, and metadata separation. A no-op must log zero rustc
  compilation commands. All four phases run the workload's output oracle.
- File bytes include the target and its controlled temporary directory. Hardlinks
  are counted once. Logical length and `st_blocks` allocation are recorded
  separately. APFS clone-shared physical extents are not measured by `st_blocks`.
- Process-tree RSS and target/temporary allocation are sampled every 250 ms;
  peaks are lower bounds. Summed RSS can count shared pages more than once. CPU
  time is reported separately from wall time. These are local warm-system-cache
  measurements, not cold-boot or isolated-machine results.
- Cargo can replace output aliases and rewrite identical dependency-info files
  on a fresh no-op. The harness verifies the contents of every such changed file,
  unchanged path sets, unchanged logical byte counts, and unchanged metadata for
  other files. It reports block-allocation drift instead of treating it as a
  compiler rebuild. The excluded Bevy preflight documents why this check exists.
- Successful runs save their file census and compressed headers before removing
  their own scratch target and temporary files. This prevents the experiment from
  retaining dozens of multi-gigabyte targets. Raw commands, logs, samples, source
  hashes, oracles, and accounting remain available.

## Are eager decoding and temporary linker inputs necessary?

The current compatibility adapter imposes these costs; compression itself does
not require them. The [source audit](../../docs/target-directory-size/decoding-and-linker-costs.md)
traces each reader and linker path.

| Change | Cost it can avoid | Remaining work or tradeoff |
|---|---|---|
| Skip metadata pack-then-read-back where no later consumer needs its bytes | A producer's redundant full decode and allocation | Prove the path does not embed or serialize the metadata later; test metadata-only, codegen, proc-macro, and rlink consumers. |
| Indexed metadata and query-cache readers | Decode and retain only blocks actually read | Existing borrowed-slice interfaces require stable ownership. Readers need offset/range access and a cache that cannot evict live borrows. The dep graph may still need most of its data eagerly. |
| Compression-aware archive reader/linker | Decode selected archive members and eliminate the complete temporary raw archive | An ordinary archive index plus GNU BFD plugin is a plausible route to selected temporary objects. Fully in-memory decoding needs linker support; Apple's published linker interfaces do not offer that generic plugin route. The format/index writer must also understand compressed members. |
| Transparent filesystem compression | Preserve normal mmap/linker paths without a user-space raw archive copy | Platform-specific support and physical-byte accounting; decoding and page-cache memory still cost resources. This campaign does not benchmark it. |
| Keep decoded files or buffers cached | Avoid repeated decoding across uses | Spends disk or memory and needs eviction. Include the decoded cache in total storage; moving it outside `target/` does not remove its bytes. |
| Use incremental-cache-only compression | Avoid materializing compressed dependency rlibs | Gives up compression of normal rlibs/rmeta. Reused compressed incremental objects still need restoration. This option is measured in the campaign. |

Some decoding of the data actually consumed remains necessary. The current linker
adapter already streams through bounded buffers; it writes a full ordinary
archive to disk but does not allocate the whole archive in RAM. Standalone packed
metadata and query caches currently decode into full private buffers, while raw
files use demand-paged mappings. The primary Cargo path here uses separate rmeta
files; the audit's worst case of retaining an entire decoded rlib for metadata
must not be assigned to every compiler process in these measurements.
Decoding selected native members directly into the linker would remove their
temporary files, but could increase private memory compared with mapping raw
inputs. That tradeoff needs a measured implementation; eliminating a disk copy
does not automatically reduce RAM use.

My next architectural experiment would pair an ordinary archive symbol index
with independently compressed members and a linker that decodes only selected
members. In parallel, metadata and query readers can use the container's existing
range-read primitive through interfaces that preserve safe ownership. Those
changes target the repeated edit/link work that remains expensive here. They
are proposed follow-ups; this campaign benchmarks the existing eager adapter.

## Prior art

The broad compression proposal predates this implementation. [Rust issue #66348](https://github.com/rust-lang/rust/issues/66348)
raised target compression in 2019. [Cargo issue #16462](https://github.com/rust-lang/cargo/issues/16462)
explicitly proposed compressing intermediates and decompressing them during use
in January 2026. Their manual archive/gzip ratios are not successful builds with
compressed active inputs.

The [Luna Max prior-art report](../../docs/target-directory-size/compressed-artifact-prior-art.md)
also found two stronger implemented precedents:

- [GCC's LTO pipeline](https://gcc.gnu.org/onlinedocs/gcc/Optimize-Options.html)
  compresses persisted GIMPLE intermediate code with configurable zstd/zlib
  levels. Its linker integration can select needed archive members. This covers
  IR sections, not general native archive members or Rust incremental data.
- [Rust 1.72](https://github.com/rust-lang/rust/blob/1.72.0/compiler/rustc_codegen_ssa/src/back/metadata.rs)
  Snappy-compressed metadata embedded in dynamic libraries. That narrow feature
  [was removed in July 2023](https://github.com/rust-lang/rust/commit/52853c2694309353353a4ecba1a09a87791a7fd6);
  a [maintainer explanation](https://internals.rust-lang.org/t/librustc-driver-so-not-reproducible/19639/10)
  cites avoiding a performance regression. It did not cover ordinary standalone
  rmeta, native rlib members, or incremental files.

The report also covers cargo-archive, ccache/sccache, filesystem compression,
seekable Zstd, Clang's compact lazy AST persistence, rustc's existing lazy metadata
decoding, and a Rust-aware linker/cache proposal. The bounded search found no
shipped rustc/Cargo integration matching this prototype's active artifact and
incremental-cache coverage. Compiler-owned compression is established prior art;
that search result is not proof of novelty. Zulip archive search coverage was
limited by indexing.

## Reproduction and evidence

The [large-workload runner](large-benchmark.py) uses the existing measurement
helpers. Its manifest records source, lockfile, compiler, driver, helper, runner,
and fixture hashes. The [analyzer](analyze-phase2.py) reconciles each file census
against final allocation and reads saved packed headers after scratch cleanup.

Raw evidence is under
[`2026-09-26-large-projects`](../../docs/target-directory-size/measurements/2026-09-26-large-projects/).
The earlier failed Bevy preflight is excluded from timing/size summaries. It
successfully compiled and ran; its strict no-op block equality assertion exposed
Cargo's identical-content output rewrites, which the final runner validates
explicitly.

With the pinned source clones and recorded compiler available, reproduce one
matrix as follows; use `polars` or `nushell` and a fresh output directory for the
other workloads:

```sh
rtk proxy uv run --no-project --python /opt/homebrew/bin/python3 --with psutil \
  tools/compressed-artifacts/large-benchmark.py \
  --workloads bevy --out /tmp/compressed-bevy-reproduction \
  --repetitions 3 --arms dedup_off incremental_only both_fast both_balanced \
  --jobs 4 --sample-interval 0.25 --cleanup-targets

rtk proxy uv run --python /opt/homebrew/bin/python3 python \
  tools/compressed-artifacts/analyze-phase2.py /tmp/compressed-bevy-reproduction
```

The final matrix directories are `bevy-matrix-v2`, `polars-matrix-v1`, and
`nushell-matrix-v1`. The [preparation records](../../docs/target-directory-size/measurements/2026-09-26-large-projects/preparation/)
save lockfiles, fetch commands, source status and host/linker identity.
The [methodology audit](../../docs/target-directory-size/measurements/2026-09-26-large-projects/methodology-audit.md)
and [analyzer regression checks](../../docs/target-directory-size/measurements/2026-09-26-large-projects/analyzer-validation.json)
document accounting checks. [Final verification](../../docs/target-directory-size/measurements/2026-09-26-large-projects/final-validation.json)
confirms source/binary/harness hashes, all 36 compression scopes, Python syntax,
fixture formatting and a clean whitespace diff. All completed scratch targets
and verified source copies were removed; raw evidence and pinned source clones
remain. Compiler source is preserved in the earlier
`2026-09-26-compression/implementation-source.tar.gz`; this campaign's
`large-source-v2.tar.gz` preserves the final runner, analysis scripts, fixtures
and reports, with file hashes in `large-source-v2.json`.
