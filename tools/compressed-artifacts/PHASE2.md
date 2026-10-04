# Compression profiles, faster reads, and incremental compilation

The [large-project follow-up](LARGE-PROJECTS.md) extends these measurements to
Bevy, Polars, and Nushell. It provides a broader check on target-size reductions
and the absolute cost of relinking after a small edit.

Scope: one branch, native Apple Silicon, debug information disabled. This extends
the [first compiler prototype](RESULTS.md). Measurements compare actual target
directories produced by the patched compiler, with metadata stored once in every
primary arm. Ordinary executables and loadable libraries keep their native formats.

**Result:** keep Zstandard, offer explicit profiles, and make incremental-cache
compression independently selectable. Across four development consumers, `fast`
artifact compression saves **35–44%** of retained allocation with **1.3–3.4%** median
clean-build overhead. Compressing artifacts and incremental caches together with
`balanced` saves **52–56%**, with **6–13%** clean-build and **16–22%** semantic-edit
overhead. The speed gap is small for clean nonincremental builds but remains in
short edit cycles.

For frequent incremental development, **cache-only balanced compression** is an
attractive first setting: **37–42%** smaller targets, **5–11%** clean-build overhead
and **1–7%** edit overhead here. Use both modes with `fast` when the additional
storage saving matters. Use `small` when capacity outweighs compilation time.
These are local measurements on small consumers, not defaults suitable for every
Rust project.

## Compression choices

The implementation keeps Zstandard and exposes three profiles:

| Profile | Zstd level | Independent chunk size | Intended tradeoff |
|---|---:|---:|---|
| `fast` | -1 | 256 KiB | Lower encoding cost for frequent builds |
| `balanced` (default) | 3 | 256 KiB | Smaller artifacts with moderate encoding cost |
| `small` | 9 | 1 MiB | More encoding CPU and memory for additional savings |
| `legacy` | 3 | 64 KiB | Same geometry as the original prototype |

`-Zartifact-compression-level=N` and `-Zartifact-compression-chunk-size=N` override
the profile. Levels range from -5 through 19; chunk sizes are powers of two from
16 KiB through 4 MiB. The shared settings apply to both artifact and incremental
compression. Neither compression mode is enabled by default.

The [codec research and raw sweep](../../docs/target-directory-size/compression-methods.md)
cover Zstd, LZ4/HC, Snappy, zlib, libdeflate, Brotli, LZMA2 and Apple LZFSE, including
different levels, geometries, encode/decode costs, memory and range amplification.
These are lossless trials over real Rust artifacts. The sweep includes container
overhead and raw fallback, but its Python/native-library timing model is separate
from compiler build measurements.

## Faster implementation

- Reuse one Zstd context and scratch allocation across a file's chunks.
- Decode full reads directly into the final contiguous allocation.
- Read sequential payloads without seeking for each chunk.
- Track writer offsets rather than querying the file position for every chunk.
- Skip tiny files that cannot recover the container overhead.
- Use runtime-selected AArch64 IEEE CRC instructions, with a portable slicing-by-8
  fallback. These preserve the existing checksum; CRC32C instructions would not.

Five alternating runs over the same legacy packed files measured `read_all` at
**837.6 → 1206.9 MiB/s (+44.1% throughput, about 30.6% less elapsed time)**. The old
reader already used slicing-by-8 CRC. Context reuse alone improved the isolated
compression component by only 2.0%; checksum acceleration and avoiding extra
buffer work matter more. These are warm-cache reader measurements, not a claim
that builds improve by 44%.
[Detailed performance evidence](../../docs/target-directory-size/compression-performance.md).

## Incremental implementation

`-Zcompress-incremental` is independent of `-Zcompress-artifacts`. It packs finished
dependency graphs, query caches, work-product indexes, cached codegen outputs,
pre-LTO bitcode and ThinLTO key maps. Files retain their existing names. Readers
accept ordinary and compressed files; codegen reuse decisions stay in rustc.

Packed work products are decoded to ordinary files for codegen/linking. Already
packed reused cache files stay packed. Persistent records are decoded eagerly
because the existing query/graph interfaces require contiguous slices. This can
increase private memory versus mapping a raw file; chunk size does not bound the
total decoded cache allocation.

Two lifecycle details are essential:

- Pack the dependency graph after its encoder writes the footer, immediately
  before publishing the completed session.
- Break old session hardlinks before writing pre-LTO bitcode or ThinLTO keys.
  Packing by atomic replacement cannot repair an inode already truncated earlier.

The regression checks an unchanged module's packed object is reused, a changed
module is regenerated, runtime results are correct, damaged records trigger
recovery, and previous LTO session aliases remain unchanged.

## Whole-build measurements

All comparisons below keep debug information disabled and metadata stored once.
Numbers are whole-target allocated file bytes after two checked semantic edits;
they include required ordinary executables. Each development cell has five fresh
target repetitions. Percentages are medians of paired per-repetition ratios;
ranges span workload medians. They are not confidence intervals or ratios of
the displayed time medians.

### Development builds without incremental compilation

| Consumer | Uncompressed | Fast | Balanced | Small |
|---|---:|---:|---:|---:|
| Regex | 29.38 MiB | 16.37 MiB | 15.62 MiB | 15.08 MiB |
| Serde | 30.41 MiB | 19.84 MiB | 19.29 MiB | 18.93 MiB |
| Clap | 29.58 MiB | 17.83 MiB | 17.25 MiB | 16.79 MiB |
| Syn | 17.80 MiB | 11.48 MiB | 11.19 MiB | 10.97 MiB |

| Profile | Retained allocation saved | Clean-build overhead | Second semantic-edit overhead |
|---|---:|---:|---:|
| Legacy | 36.0–46.1% | 2.0–5.4% | 6.8–15.8% |
| Fast | 34.8–44.3% | 1.3–3.4% | 8.0–14.7% |
| Balanced | 36.6–46.8% | 1.9–3.7% | 8.8–21.7% |
| Small | 37.7–48.7% | 3.4–8.1% | 5.9–14.1% |

Fast and balanced meet the proposed 5% clean-build overhead gate for the four
workload medians. Edit cycles do not meet that gate. Fast adds about **15–23 ms**
to the displayed semantic-edit medians (baseline 154–274 ms). This is a meaningful
remaining cost even when the absolute difference is short.

The `legacy` arm uses the optimized reader with the old level and chunk size.
It isolates profile geometry within this compiler. Comparing these results to
the first prototype's older matrix does not isolate the implementation speedup;
the matched reader experiment above provides that evidence.

### Incremental development builds

`Artifacts` enables only artifact compression at balanced. `Caches` enables only
incremental compression at balanced. The other columns enable both modes.

| Consumer | Uncompressed | Artifacts | Caches | Both fast | Both balanced | Both small |
|---|---:|---:|---:|---:|---:|---:|
| Regex | 108.34 MiB | 93.92 MiB | 65.44 MiB | 51.81 MiB | 48.14 MiB | 45.52 MiB |
| Serde | 105.16 MiB | 93.92 MiB | 61.22 MiB | 51.51 MiB | 48.45 MiB | 46.43 MiB |
| Clap | 87.84 MiB | 75.12 MiB | 55.49 MiB | 44.89 MiB | 42.05 MiB | 40.05 MiB |
| Syn | 65.69 MiB | 58.89 MiB | 39.24 MiB | 33.56 MiB | 31.39 MiB | 30.04 MiB |

| Mode | Retained allocation saved | Clean-build overhead | Second semantic-edit overhead |
|---|---:|---:|---:|
| Artifacts only, balanced | 10.3–14.4% | 0.8–4.9% | 7.4–19.8% |
| Caches only, balanced | 36.8–41.8% | 4.7–10.9% | 1.2–6.7% |
| Both, fast | 48.9–52.2% | 6.5–9.1% | 11.0–20.7% |
| Both, balanced | 52.1–55.6% | 6.5–12.9% | 16.0–22.1% |
| Both, small | 54.3–58.0% | 13.0–28.6% | 26.7–37.8% |

For Regex, balanced compression changes clean-build medians from **2.294 to
2.622 s** and semantic-edit medians from **189 to 228 ms**. Cache-only compression
takes **2.524 s** and **189 ms**, respectively. Small saves another **2.62 MiB**
over balanced but takes **2.950 s** clean and **255 ms** for that edit. Its modest
extra savings do not justify making it the development default.

Dependency graphs and query caches explain most of the incremental improvement.
For Regex, balanced reduces the allocated dependency graph from **44.50 to
21.98 MiB** and query caches from **25.86 to 7.24 MiB**; cached objects fall from
**7.73 to 3.05 MiB**. These are disjoint classes. Metadata shared by hardlinks
between `deps/` and `incremental/` is reported separately, avoiding double counts.

### Release and native-library workloads

Release builds have three repetitions per cell and no incremental cache:

| Consumer | Uncompressed | Fast | Balanced | Small |
|---|---:|---:|---:|---:|
| Regex | 20.56 MiB | 10.79 MiB | 10.00 MiB | 9.59 MiB |
| Syn | 15.21 MiB | 10.30 MiB | 9.97 MiB | 9.74 MiB |

Retained savings span **32.3–53.3%**. Fast clean-build paired medians range from
**1.2% faster to 1.2% slower**, and balanced from **1.5% faster to 0.1% faster**.
Those small differences on a shared machine do not establish a speedup. Small
adds **2.6–4.0%** clean-build time. Semantic-edit overhead spans **1.3–9.2%**
across all three profiles and both workloads.

The native compatibility fixture combines a proc macro, bundled C, one unit test
and two integration-test executables. Its nonincremental target is **7.14 MiB**
under every profile: tiny packed Rust files save logical bytes but no additional
allocated blocks, and ordinary native outputs dominate. With incremental
compilation, the target falls from **12.83 MiB** to **10.14 MiB** fast or
**10.05 MiB** balanced (**20.9–21.6%**). This workload shows why codec ratios
cannot predict whole-target savings. Its three-repetition clean-build overhead
is **1.6%** fast and **2.6%** balanced with incremental compression; semantic-edit
medians are within **1.7%** of the control.

### Memory and temporary storage

Paired child-CPU measurements also show a cost. Without incremental compilation,
fast adds **0.0–1.9%** clean-build CPU and **3.8–6.9%** semantic-edit CPU. With
incremental compilation, both balanced adds **3.7–8.1%** clean-build CPU and
**5.1–10.4%** edit CPU; both small adds **12.7–24.0%** and **10.3–19.6%**.
These measure reaped child-process user plus system time, which can differ from
wall time because Cargo schedules concurrent jobs. The
[resource summary](../../docs/target-directory-size/measurements/2026-09-26-compression/resource-summary.json)
retains paired samples.

Retained savings are not peak-storage savings. Syn's incremental balanced target
settles at **31.39 MiB**, while its median observed clean-build high-water is
**49.9 MiB**. Regex's nonincremental balanced target settles at **15.62 MiB**, while
the semantic edit reaches **19.4 MiB**. Ordinary linker inputs and a raw file
coexisting with its compressed replacement account for part of the difference.
Sampling can miss short-lived peaks.

Memory is also a tradeoff. Incremental clean-build median summed RSS for Serde
is **639 MiB** uncompressed, **668 MiB** with balanced and **678 MiB** with small.
Short edit samples are noisy: nonincremental Regex reports **133 MiB** without
compression, **234 MiB** fast and **164 MiB** balanced. Shared pages can be counted
twice and a 50 ms sampler can miss process peaks. These observations do not
establish a memory-neutral implementation or a reliable profile ranking by RSS.

## What the measurements imply

1. **Expose profiles, keep the codec.** The codec replay saves 68.6% of artifact
   bytes with fast, 72.3% balanced and 74.8% small. Small costs about five times
   balanced's pack time in that replay. LZ4 decodes faster but saves only
   59.9%; LZMA2 saves 79.5% with much higher encode and decode costs. These are
   useful tradeoffs, but none justifies adding another codec to this prototype
   before a full-build comparison.
2. **The implemented CPU work helps, but cannot remove all materialization.**
   Hardware CRC, direct decoding, context reuse and sequential I/O improve the
   matched reader by 44.1%. The linker still needs ordinary archives, and metadata
   and persistent records still need contiguous decoded bytes. Codec tuning
   alone does not eliminate those operations.
3. **Make cache compression a separate switch.** It captures much of the
   incremental space saving with less measured edit overhead. Combining it with
   artifact compression maximizes savings, while packing newly created cache
   state visibly increases clean-build time.

The next performance experiment should measure which eager reads and archive
materializations actually dominate a large application's edit. Selective metadata
reads or linker consumption of compressed members could remove work outright.
Those need consumer-interface changes; the current range API alone does not
deliver them. Dependency graphs already pack edge widths in this checkout, so
basic integer packing is not a new unimplemented fix. Dictionary compression,
parallel chunk processing and a structural graph redesign remain unmeasured.

## Validation and reproduction

The optimized stage1 compiler and standard library built successfully from the
final frozen source. [Build 7](../../docs/target-directory-size/measurements/2026-09-26-compression/build-7/build.json)
records the revision, source hashes and actual compiler/driver identity.

- Both compiler integration regressions passed, including profile overrides,
  ordinary linker inputs, Thin/Fat LTO, proc macros and the incremental lifecycle
  checks. FatLTO verifies loaded cache/packed bitcode/runtime and hardlink
  preservation; it does not claim per-CGU reuse, which the raw control lacks too.
- All **12 codec unit tests** and the unstable-option hash test passed.
- **265 fresh-target runs**, **1,430 Cargo commands** and **1,325 runtime oracles**
  passed. The runner audited **4,109 rustc invocations**. All **265 no-op phases**
  invoked no rustc and retained the same aggregate byte/count census.
- **181 native codec trials** passed complete decoded-hash checks. The failed
  x86 Python screen and timeout-quantized smoke timings are excluded.
- Formatting, Python syntax and whitespace checks are recorded with the final
  source snapshot. Build outputs are outside the tracked implementation.

See the [independent record audit](../../docs/target-directory-size/measurements/2026-09-26-compression/audit.md),
[test commands and logs](../../docs/target-directory-size/measurements/2026-09-26-compression/final-validation/records.json),
and [measurement index](../../docs/target-directory-size/measurements/2026-09-26-compression/README.md).
The matrices demonstrate actual incremental compilation, successful edited
outputs and stable no-op aggregate accounting. The focused regression separately
asserts unchanged-CGU reuse. No-op aggregate equality does not prove every path
or file content remained identical.

The [usage guide](README.md) gives build commands and flags. To repeat the main
incremental matrix with the compiled prototype:

```sh
rtk proxy uv run --no-project --python /opt/homebrew/bin/python3 --with psutil \
  tools/compressed-artifacts/benchmark.py \
  --rustc /tmp/rust-compressed-artifacts-bootstrap/aarch64-apple-darwin/stage1/bin/rustc \
  --cargo /tmp/rust-compressed-artifacts-bootstrap/aarch64-apple-darwin/stage0/bin/cargo \
  --out /absolute/new-measurement-directory \
  --repetitions 5 --incremental --semantic-edits 2 \
  --workloads regex serde clap syn \
  --arms dedup_off balanced incremental_only both_fast both_balanced both_small

rtk proxy uv run --python /opt/homebrew/bin/python3 \
  tools/compressed-artifacts/analyze-phase2.py /absolute/new-measurement-directory
```

The benchmark expects the same local workload checkouts recorded in its manifest.
Use a fresh output directory and analyze before removing its target files.
After analysis and audit, this run's disposable targets and temporary directories
were removed: **10.17 GiB of allocated file bytes** across 578 roots, including
the corpus-generation and smoke runs. This does not measure physically recovered
APFS space. Raw results, corpora, source snapshots and the compiled prototype
remain available.

## Method and limits

The compiler and standard library are optimized stage1 builds from revision
`b7b856c888bfc50d65a0f53f1188cf32cc75ed1d` plus this local patch. Build records pin
the source hashes, compiler driver, launcher, command and logs. The host is an
Apple M5 Pro with 48 GiB RAM; Python and the compiler run natively on ARM64.

Each arm/repetition uses a fresh target and copied consumer. Arm order rotates.
The runner measures clean, no-op, comment-edit and two semantic-edit phases.
Semantic edits change a printed value and the runtime oracle verifies it.
Compiler invocations are checked for the selected executable, zero debug info,
metadata mode, compression settings and actual incremental codegen where required.
No-op phases must invoke no rustc and leave logical and allocated bytes unchanged.

Allocation is `st_blocks × 512`, counting each inode once across the target and
its explicit temporary directory. This does not resolve shared APFS clone extents
or filesystem metadata. Separate incremental-directory counts disclose overlap
with other paths, so hardlinks are not silently counted twice. CPU uses reaped
child-process usage; memory and temporary-storage peaks are sampled lower bounds.
Summed process RSS can double-count shared pages.

The machine has ordinary desktop activity and warm OS caches. Small local
consumers and a compatibility fixture do not establish large-application,
cross-platform, cold-cache or C/C++ parity. This remains an opt-in prototype with
a system libzstd dependency, eager decoding and ordinary temporary linker inputs.
Interrupted-process cleanup and portable dependency packaging remain unfinished.
