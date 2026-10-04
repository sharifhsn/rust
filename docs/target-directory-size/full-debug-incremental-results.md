# Target size with debug info and incremental compilation enabled

Date: September 27, 2026. These are local rustc and Wild experiments, not
upstream or stable Rust behavior. **Every build below kept incremental
compilation on and used `-Cdebuginfo=2`.**

## Results at a glance

- Compressing the redesigned incremental graph reduced allocated `target/`
  size by **17.4%–26.4%** in Bevy, Polars, and Nushell. Average semantic-edit
  time changed by **+0.1% to +12.3%**; peak sampled process-tree RSS changed by
  about **−4.3% to +5.2%**. This is the clearest low-tradeoff large-project
  result so far.
- On Bevy, compressed artifacts plus incremental compression retained
  **5.489 GiB rather than 14.728 GiB** with ordinary artifact storage, a
  **62.7% reduction**. Direct compact objects in Wild retained **5.052 GiB**,
  a **65.7% reduction**. The direct format was about 8% smaller than the
  compressed `.rlib` path.
- Direct linking still has material cost versus ordinary artifacts: the paired
  Bevy touch-only relink was **32.0 s versus 18.3 s**, and peak RSS was
  **9.11 versus 8.13 GiB**. In a final-code run, a 256 MiB decoded-page budget
  kept the same 5.052 GiB target and sampled **6.23–6.66 GiB** RSS. Its
  touch-only phase took 32.6 s in one run, close to the 33.5 s uncapped run;
  repeated controlled timing is needed before drawing a speed conclusion.
- The Zstd `fast` profile did not speed this Bevy direct-link workload. It
  increased retained target size by **5.2%** versus balanced and had similar
  build and edit times.

## Conditions and scope

The graph comparison ran on the Apple M5 Pro, macOS ARM64, with the same local
stage1 rustc and four jobs. Each project used a fresh target directory and five
phases: clean build, no-op source touch, and three semantic edits. The example
executable's runtime oracle passed after every phase. The `raw_pages` and
`compressed_pages` arms use the same paged graph representation; they differ
only in `-Zcompress-incremental`. Artifact compression was off for this
comparison.

The artifact/Wild comparison ran in a Debian 12 ARM64 container on the same
Apple Silicon host. It built Bevy's full-default-feature example and ran a
headless ECS workload after every measured phase. All arms used the same local
stage1 rustc, Wild linker, dev profile, `CARGO_INCREMENTAL=1`,
`CARGO_PROFILE_DEV_DEBUG=2`, `-Cdebuginfo=2`, four jobs, no LTO, no embedded
bitcode, and `-Zembed-metadata=no`. That last option was held constant and its
size reduction is not credited to compression. The build uses a local Bevy
checkout, so Bevy workspace crates also have incremental state. It is not the
exact footprint of an application consuming a registry release.

Target size is allocated unique-inode bytes (`st_blocks * 512`), not logical
file length, APFS space, or host-wide physical storage. RSS is sampled every
200–250 ms by summing the benchmark process tree; brief peaks may be missed and
shared pages may be counted more than once. Timings are individual runs, not a
statistical comparison. Bevy's macOS graph build and Linux Wild build are
separate experiments and should not be compared directly.

## Incremental graph compression

| Project | Raw clean target | Compressed clean target | Saved | Raw clean time | Compressed clean time | Avg. 3 edits: raw → compressed | Max RSS: raw → compressed |
|---|---:|---:|---:|---:|---:|---:|---:|
| Bevy | 13.431 GiB | 10.232 GiB | 23.8% | 192.7 s | 198.6 s | 39.2 → 42.1 s (+7.6%) | 10.96 → 10.49 GiB (−4.3%) |
| Polars (`lazy,parquet`) | 9.461 GiB | 6.962 GiB | 26.4% | 117.8 s | 109.7 s | 20.3 → 22.9 s (+12.3%) | 6.83 → 7.18 GiB (+5.2%) |
| Nushell | 6.597 GiB | 5.452 GiB | 17.4% | 132.2 s | 143.7 s | 20.6 → 20.6 s (+0.1%) | 5.27 → 5.37 GiB (+2.1%) |

After all three edits, retained sizes were 13.432 → 10.242 GiB for Bevy,
9.472 → 6.973 GiB for Polars, and 6.608 → 5.463 GiB for Nushell. All 30
build/runtime phases passed. No arm disabled incremental compilation or debug
information.

The graph format includes the stable-ID paged graph and sparse reverse index.
This measurement isolates compression of that format; it does **not** compare
the redesigned graph against an unmodified released compiler. The reverse index
is an intentional storage cost of supporting lazy reverse lookups. Earlier
debug-off graph/cache measurements are in
[the historical report](lazy-graph-and-direct-wild-results.md).

## Bevy artifact and linker comparison

All five arms kept debug information and incremental compilation enabled and
used Wild as the linker. Times are clean / touch-only / semantic-edit seconds.

| Artifact layout | Retained target | Clean / touch / edit | Max sampled process-tree RSS | Size saved vs ordinary |
|---|---:|---:|---:|---:|
| Ordinary `.rlib`, raw incremental graph | 14.728 GiB | 175.2 / 18.3 / 19.8 s | 8.13 GiB | — |
| Incremental compression only | 10.425 GiB | 220.4 / 21.2 / 23.2 s | 8.09 GiB | 29.2% |
| Artifact compression only | 9.828 GiB | 204.5 / 29.7 / 32.8 s | 8.50 GiB | 33.3% |
| Both, standard compressed `.rlib`s | 5.489 GiB | 224.7 / 28.2 / 34.5 s | 8.49 GiB | 62.7% |
| Both, compact objects linked directly by Wild | **5.052 GiB** | 207.6 / 32.0 / 29.2 s | 9.11 GiB | **65.7%** |

Against ordinary output, the standard combined path increased clean time by
28%, touch-only time by 54%, and semantic-edit time by 74% in this one run.
Direct Wild reduced the clean-build penalty to 19%, but its touch-only and
semantic-edit times were 74% and 47% higher. Direct linking saved a further
0.437 GiB (8.0%) over standard compressed `.rlib`s, while its measured peak RSS
was 7% higher than that arm. These tradeoffs are too large to call the direct
format a transparent default today.

## Lazy payload reads and decoded-page budget

The compact format stores object metadata separately from independently
compressed payload blocks. Wild reads the compact library manifest and object
sections directly; it does not extract conventional `.o` files. The payload is
decoded when a section is requested. Across the Bevy clean link, Wild read
24,359 of 24,816 payload blocks (98.2%) and decoded 4.55 GB over 13 linker
processes. Thus this workload validates the direct reader but leaves little
payload untouched. It eagerly parses about 0.92 GB of object metadata across
those linker processes; a compact symbol summary and later metadata loading
remain possible improvements.

The optional Unix setting `WILD_COMPACT_CACHE_BYTES` streams decoded blocks to
unlinked temporary files, maps them read-only, and asks the OS to discard old
pages while keeping borrowed section addresses valid. Each accessed block is
still fully decompressed and checksum-verified on first access; the cache bounds
resident mapped pages, not decode-buffer size or the linker’s metadata. In the
Bevy link, 98.2% of payload blocks were requested, so this did little work
avoidance. The cap is mainly a memory-residency control. It also avoids
extracting conventional `.o` files: Wild consumes compact objects directly.

Results from balanced-profile Bevy runs:

| Decoded-page cache setting | Target | Clean / touch / edit | Peak process-tree RSS | Per-linker tracked-byte maximum |
|---|---:|---:|---:|---:|
| Unset (pinned in memory) | 5.052 GiB | 181.5 / 33.5 / 33.7 s | 9.12 / 9.00 / 9.07 GiB | — |
| 1 GiB | 5.052 GiB | 203.5 / 41.8 / 37.0 s | 7.34 / 6.53 / 6.71 GiB | 1,073,727,248 bytes |
| 256 MiB | 5.052 GiB | 183.9 / 32.6 / 38.2 s | 6.66 / 6.23 / 6.57 GiB | 268,429,045 bytes |

Every phase passed the Bevy runtime oracle. The cap applies per Wild process to
the cache's tracked mapped blocks, not to total process RSS or the operating
system's page cache. The RSS reduction is empirical, not a hard memory limit.
The 1 GiB final-code run advised 5.56–5.66 GiB of mappings and recorded about
38,000 evictions per phase. The 256 MiB run advised 6.93–6.99 GiB and recorded
about 48,000 evictions per phase. Both caps lowered sampled RSS. The 256 MiB
touch-only result was close to the uncapped run in this individual measurement;
the single-run timings are too noisy to claim the cap is faster. Temporary
backing files are outside the retained target census; decoded bytes are written
there until the linker releases the mappings, so this trades memory residency
for scratch space and page-fault work.

## Compression profile check

The direct compact Bevy path with the `fast` profile retained 5.315 GiB versus
5.052 GiB with `balanced` (+5.2%). Clean / touch / semantic timings were
183.2 / 34.7 / 34.3 s versus the paired balanced unbounded run's
181.5 / 33.5 / 33.7 s. RSS was effectively unchanged. The faster codec setting
therefore did not improve this direct link. Broader codec, level, and block-size
measurements remain in [compression-methods.md](compression-methods.md),
[compression-performance.md](compression-performance.md), and
[the phase-two profile study](../../tools/compressed-artifacts/PHASE2.md).

## Final executable DWARF compression

The same Bevy direct-link configuration was also run with
`--compress-debug-sections=zstd` passed through to Wild. Debug information
remained enabled at `-Cdebuginfo=2`, incremental compilation stayed on, and the
Bevy runtime oracle passed after clean, touch-only, and semantic-edit phases.
The allocated target was **3.540 GiB**, compared with **5.052 GiB** for the
uncompressed-DWARF direct-link run, a **1.512 GiB (29.9%) reduction**. The
compressed-DWARF run took 202.7 / 28.4 / 29.9 s and sampled 8.46 / 8.56 /
8.01 GiB peak process-tree RSS across those phases. These are individual runs,
not a paired timing study; the evidence supports a substantial target-size
gain while retaining debug information, but does not yet establish a reliable
compile/link-time effect. Raw records are in
the DWARF-compressed Bevy results (private receipt archive).

## Practical reading

The incremental graph compression is the best current size/speed balance:
17%–26% smaller `target/` directories with small or mixed timing changes in
these runs. Compressing `.rlib`/`.rmeta` adds more savings but slows compiler
and link work. Direct Wild linking removes expanded object/archive copies and
wins the most space, but clean-build time, relink time, memory, and temporary
scratch still need work. The final-code 256 MiB run reduced sampled RSS to
6.23–6.66 GiB without a large touch-only penalty in that run, though repeated
paired timing is needed. No measured profile currently delivers the maximum
size reduction with negligible performance and memory cost.

## Raw evidence

- Graph benchmark script (private receipt archive)
- Bevy graph raw records (private receipt archive)
- Polars graph raw records (private receipt archive)
- Nushell graph raw records (private receipt archive)
- Wild Bevy benchmark script (private receipt archive)
- Wild artifact comparison raw records (private receipt archive)
- Wild cache/profile comparison raw records (private receipt archive)
- Final-code 1 GiB cache run (private receipt archive)
- Final-code 256 MiB cache run (private receipt archive)
- [Compact artifact reader and cache implementation](https://github.com/sharifhsn/wild/blob/codex/target-size-research/compact-artifact/src/lib.rs)
- [Wild compact ELF reader](https://github.com/sharifhsn/wild/blob/codex/target-size-research/libwild/src/elf.rs)
- [Direct compact artifact design](../../tools/compressed-artifacts/COMPACT-WILD.md)
