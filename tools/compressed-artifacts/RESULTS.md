# Compressed Rust artifacts: implementation and measured results

September 26, 2026. Final compiler: **build 5**, with the optimized CRC32 reader.

This is the first prototype's retained report. See the
[follow-up profiles, speed work and incremental measurements](PHASE2.md) for the
current implementation and build 7 results.

The native prototype confirms the storage estimate: **36–46% fewer allocated file bytes beyond metadata deduplication** for four development-build consumers. With metadata deduplication included, their targets are **48–61% smaller** than the embedded-metadata baseline. These are actual retained files produced and consumed by the patched compiler.

The performance tradeoff is substantial enough to keep this opt-in. Development clean builds were **8–23% slower**, and comment-edit rebuilds **8–21% slower**, in the final three-repetition matrix. The original suggested **under-5% time-overhead gate is not met**. The next implementation work should focus on selective metadata reading and avoiding full archive materialization.

## Implemented behavior

- `-Zcompress-artifacts=yes` writes `.rmeta` and `.rlib` as versioned 64 KiB Zstandard level-3 chunks, with raw fallback when packing does not reduce file length.
- The 64-byte header and 24-byte chunk index entries include checksums and checked lengths. Same-directory atomic replacement preserves modification times and permissions.
- Rustc metadata loading and LLVM LTO accept raw or compressed inputs. The reader currently fully decodes metadata into owned memory; the codec has range reads, but compiler metadata access does not yet use them.
- Link operations temporarily restore conventional archives, keeping their names and ordinary archive-member selection. Successful and failed links clean up these files. Forced-termination recovery is not implemented.
- Executables, dynamic libraries, proc macros, final static libraries, and build-script native outputs retain ordinary formats. There are no permanent decoded copies of compressed intermediates.
- A measured CRC32 bottleneck was replaced with portable slicing-by-8. All checksum values remain compatible with the initial format.

This is a local compiler prototype for Apple Silicon with a system `libzstd` dependency. The default is off. It is not a supported upstream artifact format. [Build and usage instructions](README.md) include the command for a separate project.

## Comparison and environment

Each run used one fresh copied consumer and a fresh target. **All arms used `debug=0` and `incremental=false`.** The primary baseline also stored metadata once using Cargo `-Zno-embed-metadata`. Only compression changed in the primary comparison. A third arm kept embedded metadata and compression off, to measure the combined gain directly.

The same patched compiler served every arm; the baseline is not a separate stock compiler. Cargo build-unit hashes and this tracked compiler option change artifact identity between arms. Both arms execute the same source-level workload and runtime assertions; their compiler inputs are not asserted to be byte-identical.

- Apple M5 Pro, 18 logical CPUs, 48 GiB RAM; macOS 26.6.2; APFS.
- Native `aarch64-apple-darwin` stage1 rustc `1.99.0-dev`, LLVM 22.1.8, optimized compiler with debug information and incremental compilation off.
- Checkout `b7b856c888bfc50d65a0f53f1188cf32cc75ed1d` plus the local source patch and new codec; exact source hashes are in the build record.
- Bootstrap Cargo `1.98.0-beta.2`, commit `864064524a7205e670579b6873d9b18897437eaf`; `RUSTC_BOOTSTRAP=1` enables its metadata option in benchmark subprocesses.
- Zstandard 1.5.7. Four Cargo jobs. Three repetitions per arm, rotating arm order.
- Main matrix: five workloads, three arms, three repetitions = 45 runs. Release checks: Regex and Syn, three arms, three repetitions = 18 runs.

The compiler reports an unknown commit in `-vV`; exact provenance comes from [build-5/build.json](../../docs/target-directory-size/measurements/2026-09-26-native/build-5/build.json), source hashes, and the driver-library hash. Source hashes were unchanged during the successful build and checked again before preserving the implementation. The tests use matching stage1 standard libraries.

## Retained development targets

These are medians of **allocated file bytes**, counted once per inode. “Extra saving” compares compression with metadata stored once. “Combined saving” compares both improvements with embedded metadata. Percentages use exact bytes.

| Consumer | Embedded metadata | Metadata once | Metadata once + compression | Extra saving | Combined saving |
|---|---:|---:|---:|---:|---:|
| Regex | 40.34 MiB | 29.38 MiB | 15.82 MiB | 46.1% | 60.8% |
| Serde | 41.23 MiB | 30.40 MiB | 19.45 MiB | 36.0% | 52.8% |
| Clap | 35.75 MiB | 29.57 MiB | 17.39 MiB | 41.2% | 51.3% |
| Syn | 21.56 MiB | 17.80 MiB | 11.26 MiB | 36.7% | 47.8% |
| Native/test fixture | 7.14 MiB | 7.13 MiB | 7.13 MiB | 0.0% | 0.1% |

Logical file lengths give similar extra savings: Regex 46.4%, Serde 36.7%, Clap 41.9%, Syn 37.3%. The small native/test fixture saves 0.068% logically and **0% in allocated bytes**. Its executables and loadable macro dominate the folder, and shrinking tiny metadata files does not free filesystem blocks. It is a compatibility and artifact-mix check, not a large native-heavy application benchmark.

For example, Regex retains about 5.9 MiB of compressed `.rmeta` and `.rlib` payloads representing about 19.4 MiB decoded, plus its ordinary executable and other files. The entire target, including those uncompressed outputs, is in the table. The full [artifact census](../../docs/target-directory-size/measurements/2026-09-26-native/dev-v2/artifact-census.json) records real packed headers, decoded lengths, and allocation by file type.

## Development build costs

“Off” and “on” both use metadata once. CPU is summed user+system time for reaped child processes, so parallel builds can use more CPU seconds than wall seconds. The edit appends a harmless comment to the consumer main file, forcing recompilation while fixing the expected runtime result. It does not represent every semantic edit.

| Consumer | Clean: off → on | Change | Edit: off → on | Change | Clean CPU: off → on |
|---|---:|---:|---:|---:|---:|
| Regex | 1.786 → 1.932 s | +8.2% | 0.177 → 0.212 s | +19.4% | 3.430 → 3.631 s |
| Serde | 2.949 → 3.245 s | +10.0% | 0.281 → 0.304 s | +8.2% | 6.170 → 6.425 s |
| Clap | 2.590 → 2.817 s | +8.8% | 0.232 → 0.272 s | +17.2% | 5.214 → 5.691 s |
| Syn | 2.038 → 2.499 s | +22.6% | 0.157 → 0.190 s | +20.5% | 2.783 → 2.956 s |
| Native/test fixture | 1.260 → 1.333 s | +5.8% | 0.339 → 0.347 s | +2.2% | 1.382 → 1.389 s |

All 45 no-op phases had **zero rustc invocations and zero allocated-byte growth**. Typical no-op medians were 20–27 ms for these consumers; the native fixture runs both `cargo build` and `cargo test`, so its combined no-op took about 36–37 ms. Small no-op percentage differences at that scale should not be treated as meaningful compiler effects.

## Release checks

Release keeps the same zero-debug, zero-incremental conditions and metadata-once baseline. Two consumers were checked; this is a narrower sample than the development matrix.

| Consumer | Embedded metadata | Metadata once | Metadata once + compression | Extra saving | Combined saving |
|---|---:|---:|---:|---:|---:|
| Regex | 32.64 MiB | 20.56 MiB | 10.21 MiB | 50.3% | 68.7% |
| Syn | 19.40 MiB | 15.21 MiB | 10.07 MiB | 33.8% | 48.1% |

| Consumer | Clean: off → on | Change | Edit: off → on | Change | Clean CPU: off → on |
|---|---:|---:|---:|---:|---:|
| Regex | 4.879 → 4.457 s | -8.7% | 0.304 → 0.317 s | +4.2% | 16.492 → 15.172 s |
| Syn | 6.486 → 6.854 s | +5.7% | 1.013 → 1.025 s | +1.2% | 17.623 → 18.455 s |

The apparent faster Regex release build is an observed result of this small matrix, not evidence of a general speedup from compression. The full per-run ranges and resource data are retained.

## Peak storage and memory

The sampler includes the target and the per-run temporary directory, so visible temporary linker archives are in scope. It requests 50 ms between samples; each scan adds time. Short-lived files and process peaks can be missed. The table also includes before/after snapshots when bounding disk peaks, so a peak cannot be smaller than the retained folder. These are **lower bounds**, not exact peak-allocation measurements.

| Consumer | Clean disk peak lower bound: off → on | Edit disk peak lower bound: off → on | Edit summed RSS peak: off → on |
|---|---:|---:|---:|
| Regex | 29.4 → 15.8 MiB | 29.6 → 19.6 MiB | 147.4 → 163.2 MiB |
| Serde | 30.4 → 22.4 MiB | 30.4 → 21.7 MiB | 172.2 → 192.6 MiB |
| Clap | 29.6 → 20.0 MiB | 30.3 → 20.0 MiB | 187.6 → 183.9 MiB |
| Syn | 17.8 → 14.1 MiB | 17.8 → 14.1 MiB | 154.5 → 214.5 MiB |
| Native/test fixture | 7.1 → 7.1 MiB | 7.1 → 7.1 MiB | 176.8 → 177.2 MiB |

The retained-space win is stronger evidence than the peak-space result. Full decoded archives exist during linking, and whole-file metadata decoding adds private memory. Some edit RSS medians increased materially—for Syn, about 154.5 to 214.5 MiB. Summed RSS can double-count shared pages and the sample can miss true peaks; these results do not establish memory neutrality.

## Validation and evidence

- Optimized stage1 compiler and standard-library build succeeded with unchanged recorded source hashes.
- **8/8 codec tests passed:** round trips; mixed compressed/raw chunks; range reads; corruption/truncation; atomic fallback; timestamp preservation; standard CRC vector and tail/alignment comparisons.
- The focused compiler integration suite passed after the final rebuild: direct `.rmeta`; full-metadata `.rlib`; runtime calls requiring archive code; thin and fat LTO; proc macros; ordinary final staticlib format and a C-linked executable; failed-link cleanup.
- Final benchmark matrices completed **63 runs, 189 main-program runtime checks, and 747 audited rustc invocations**. The synthetic fixture also ran unit and integration tests in each phase. All expected outputs matched, no-op phases did not compile, and source/dependency state remained unchanged within every run.
- All inspected targets were free of leftover codec and linker-materialization temporary files after successful runs.
- Pinned Rust formatting and scoped diff checks passed. Existing Cranelift research changes were preserved.

Evidence:

- [Final development summary](../../docs/target-directory-size/measurements/2026-09-26-native/dev-v2/summary.json) and [full results](../../docs/target-directory-size/measurements/2026-09-26-native/dev-v2/results.json).
- [Final release summary](../../docs/target-directory-size/measurements/2026-09-26-native/release-v2/summary.json) and [full results](../../docs/target-directory-size/measurements/2026-09-26-native/release-v2/results.json).
- [Compiler test log](../../docs/target-directory-size/measurements/2026-09-26-native/compiler-validation-2/test.log) and [codec test log](../../docs/target-directory-size/measurements/2026-09-26-native/codec-validation-2/tests.log).
- [Source archive](../../docs/target-directory-size/measurements/2026-09-26-native/implementation-source.tar.gz), [file hashes](../../docs/target-directory-size/measurements/2026-09-26-native/implementation-source-sha256.json), and [tracked compiler patch](../../docs/target-directory-size/measurements/2026-09-26-native/tracked-implementation.patch). The archive includes the new untracked files omitted by the tracked patch.
- [Initial matrix before CRC optimization](../../docs/target-directory-size/measurements/2026-09-26-native/dev-v1/summary.json) and [CRC microbenchmark](../../docs/target-directory-size/measurements/2026-09-26-native/codec-performance-v1/README.md). The microbenchmark isolated roughly a 4.5× CRC improvement; it does not replace whole-build measurements.

Each run directory retains commands, environment overrides, runtime output, compiler invocations, sampled resources, dependency revisions, and file censuses. `analyze.py` regenerates summaries from the raw run records; `--inspect-artifacts` additionally reads retained artifact headers.

After inspection, this task removed its disposable benchmark `target` and `tmp` trees, preserving those records and source workspaces. The [cleanup record](../../docs/target-directory-size/measurements/2026-09-26-native/scratch-cleanup.json) lists the exact paths. The compiled prototype and its bootstrap tree remain under `/tmp/rust-compressed-artifacts-bootstrap`. A fresh harness run preserves its targets until explicitly cleaned; regenerating artifact inspection requires a fresh run.

## Interpretation and next work

**The storage hypothesis passed on the dependency-heavy sample. The implementation is not yet suitable as the default.** The 30% retained-space gate was exceeded on all four development consumers, but the 5% build-time gate failed, and eager decoding has a material memory cost. The small test-heavy fixture demonstrates a real limit: compression of intermediates cannot reduce bytes dominated by conventional executables.

Next, connect range reads to the compiler metadata decoder, with bounded decoded storage, and reduce how much archive data is materialized for linking while preserving archive selection semantics. After that, measure a large application and a substantial test workspace, including dependency edits and repeated edit histories. Packaging the codec dependency, export behavior, and termination recovery also remain before an upstream proposal.

These measurements exclude incremental caches, debugging information, multiple retained configurations/branches, and cross-project sharing. They do not measure whole-machine storage, compressed-filesystem stacking, or C/C++ parity. APFS allocation uses `st_blocks × 512` with hardlink deduplication; shared clone extents, directory metadata, and filesystem bookkeeping are not resolved. Builds used warm machine caches and a shared workstation, not an isolated cold-cache performance lab. Three repetitions and small consumers support a concrete local result, not a universal percentage or statistical confidence interval.
