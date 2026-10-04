# Bounded lazy graph loading and direct compact Wild linking

Date: 2026-09-27. These are local rustc and Wild prototypes; neither change is
upstream or part of a released toolchain.

**Historical debug-info-off measurements.** The current results, with
`-Cdebuginfo=2` and incremental compilation enabled, are in
[full-debug-incremental-results.md](full-debug-incremental-results.md). This
report preserves the earlier graph-cache and first direct-link measurements.

## What is implemented

The compiler writes a stable-ID paged dependency graph with a persisted sparse
reverse index. Reads fetch only the page and index bucket needed by a query.
Both decoded caches use LRU eviction. Their defaults are 64 MiB for graph pages
and 8 MiB for index buckets; `RUSTC_INCREMENTAL_PAGE_CACHE_BYTES` and
`RUSTC_INCREMENTAL_KEY_INDEX_CACHE_BYTES` set smaller or larger limits. The
cache now evicts before insertion and skips caching an item larger than its
limit, so cache-owned resident bytes stay within the configured budget. Calls
already using an `Arc` and temporary decode buffers are outside that cache
accounting, so these limits are not a process-wide rustc RSS limit.

The Bevy edit workload also exposed a correctness bug: stable graph IDs can
contain holes after nodes are deleted. `DepGraph` now sizes its color map and
next-ID space from the graph's maximum stable index, not its count of live
nodes. The large-project run exercises that case successfully.

Wild accepts the compiler's `RCOBJ001` compressed object and `RCLIB001`
library manifest in its normal ELF input path. It does not materialize a
conventional temporary `.o` for the linker. ELF/symbol metadata is decoded at
input discovery; payload blocks are decoded only when a section is requested.
Decoded payload blocks stay pinned in per-object `OnceLock`s until the link
finishes in this initial implementation. A later optional Unix mode uses
file-backed mappings and an LRU page-advice budget; see the current report for
its memory and relink measurements.

## Incremental graph results

All three builds used the same local stage1 compiler, dev profile, incremental
compilation, `-Cdebuginfo=0`, four jobs, and compressed incremental artifacts.
They ran on an Apple M5 Pro/macOS ARM64 host. The source edits changed only the
application/example; each resulting executable passed its runtime oracle.

| Project | Clean target | Clean build | Touch-only recheck | Three semantic edits | Default page/index cache peak |
|---|---:|---:|---:|---:|---:|
| Bevy | 4.387 GiB | 89.00 s | 4.00 s | 3.23 / 5.92 / 3.78 s | 15.3 / 2.1 MiB |
| Polars (`lazy,parquet`) | 2.801 GiB | 93.32 s | 3.20 s | 2.93 / 2.93 / 2.67 s | 10.9 / 1.5 MiB |
| Nushell | 2.479 GiB | 90.06 s | 3.21 s | 2.94 / 3.02 / 2.96 s | 31.5 / 4.2 MiB |

“Touch-only recheck” rewrites the fixture with the same bytes, causing Cargo to
invoke rustc for the root crate. It is not the near-zero-cost case where Cargo
invokes no compiler. Under default limits these Bevy, Polars, and Nushell
rechecks had zero page or index evictions. The runtime checks passed for all
five build phases on each project.

A deliberately tight Bevy profile used a 4 MiB page limit and 512 KiB index
limit. An edit completed and the executable still passed. Peak cache residency
was 4,193,940 / 4,194,304 bytes for pages and exactly 524,288 / 524,288 bytes
for the index. The compiler recorded 83,641 page and 141,540 index evictions;
the edit took 34.05 seconds. This confirms bounded cache residency and shows
that limits this small cause severe reload thrashing. Process-tree RSS was
3.0 GiB, so cache caps should be tuned to real memory constraints, not mistaken
for a large reduction in total compiler memory.

On Bevy, persisted graph pages occupy about 0.992 GiB, the reverse-index files
0.346 GiB, and the query cache 0.213 GiB. The reverse index is a real target
size cost of keeping reverse lookups lazy. Against the earlier compressed-page
measurement, the page bytes stayed about the same while the new reverse index
accounts for almost all of the roughly 0.35 GiB increase. The redesign trades
that disk space for bounded on-demand graph reads; it does not itself shrink
the total target directory relative to that earlier format.

## Direct compact linking results

The matched Linux ARM64 Bevy comparison used Wild for both arms, incremental
compilation and incremental compression, `-Cdebuginfo=0`, no LTO, and four
jobs. It built a Bevy example and ran its output after clean, touch-only, and
semantic-edit phases. These target sizes are allocated unique-inode bytes in
the container, not APFS or physical-host disk use.

| Link inputs | Clean target | Clean build | Touch-only recheck | Semantic edit | Clean / edit peak RSS |
|---|---:|---:|---:|---:|---:|
| Ordinary `.rlib`, Wild | 4.264 GiB | 117.04 s | 5.64 s | 5.91 s | 3.58 / 2.39 GiB |
| Compact `.rco` members, direct Wild | 2.524 GiB | 145.75 s | 8.25 s | 8.27 s | 3.87 / 2.95 GiB |

Direct compact linking saved 1.740 GiB (40.8%) at every phase. In this one
clean run it cost 24.5% more clean-build time, 39.8–46.2% more on the two
rechecks, and 8.0% more peak clean-build RSS. These measurements show the size
gain and its current CPU/memory tradeoff; one run per arm is not enough to
claim stable timing percentages.

Wild's counters across the compact clean build counted 13,880 compact-object
reads, 1.322 GB of original object bytes, and 202 MB of compact object bytes.
The reader decoded 0.377 GiB of 0.391 GiB of cold payload and touched 13,822 of
14,267 payload blocks. Thus this Bevy link needed most of the cold payload even
though it stored it compactly. The counter also records 0.843 GiB of eagerly
decoded metadata across input reads, which contributes to the RSS and time
cost. These are aggregate linker-input counters, not unique on-disk bytes.

### Which work can be elided?

- **Temporary conventional linker inputs:** eliminated for the tested Wild
  path. Wild reads compact section data directly through its ELF reader; there
  is no whole-object extraction/staging step.
- **Whole-object eager decode:** eliminated. The payload is block-compressed
  and decoded on section access. This is demand-driven, but the Bevy profile
  shows that most payload blocks are actually used.
- **Eager ELF/symbol metadata:** still present in this prototype. The current
  archive-resolution path needs section and symbol information to decide what
  is linkable, so this prototype decodes that metadata when it discovers each
  compact object. That is an implementation boundary, not a fundamental need
  to decompress every member up front. A larger redesign can put symbol
  summaries in the library manifest, select archive members from that index,
  and load full per-object metadata only for selected members.
- **A bounded Wild payload cache:** not implemented. The linker's borrowed
  section-data interfaces can retain references for the duration of a link;
  evicting a decoded block safely requires an ownership/API change (for example
  owned section buffers or a link-lifetime arena). The current pinning avoids
  repeated decompression but lets decoded blocks accumulate. This is separate
  from the rustc incremental graph caches, which are bounded and tested above.

## Prior art and validation

Luna Max's source review found no existing compressed whole-object `.rlib`
member format consumed directly by Wild. Relevant precedents are Rust's
historical compressed bitcode members (later replaced by a different LTO
layout), Wild's support for compressed sections inside ordinary ELF objects,
and RFC 3993's still-open proposal to stabilize an external-linker-friendly
rlib format. RFC 3993 describes ordinary archive members; it does not specify
compressed members. See the [prior-art review](compressed-artifact-prior-art.md)
and [Wild integration research](wild-integration-research.md), with primary
links to the [rustc library-format guide](https://rustc-dev-guide.rust-lang.org/backend/libs-and-metadata.html),
[RFC 3993](https://github.com/rust-lang/rfcs/pull/3993),
[Rust's LTO size write-up](https://blog.rust-lang.org/inside-rust/2020/06/29/lto-improvements/),
and [Wild's compressed-input-section change](https://github.com/wild-linker/wild/pull/77).

Validation completed:

- Stage1 compiler build passed: `x.py build compiler/rustc`.
- Focused `tests/run-make/compressed-incremental` passed after the strict cache
  change (1 passed).
- The 4 MiB / 512 KiB Bevy eviction run passed its runtime oracle.
- Wild compact-artifact unit tests passed (4); Wild library tests passed (155,
  with tidy tests skipped).
- The Bevy compact/direct-Wild clean, touch-only, and edit outputs all ran and
  passed their runtime oracle.
- The broader Wild linker integration suite had 376 passing tests and five
  failures in unrelated linker-script/RELR behavior with this Debian tool
  environment; those are not compact-format passing evidence.

## Reproduction data

- Graph workload harness (private receipt archive)
- Graph cache pressure harness (private receipt archive)
- Bevy graph workload records (private receipt archive)
- Polars and Nushell graph workload records (private receipt archive)
- Wild Bevy harness (private receipt archive)
- Wild Bevy raw records, incremental compression enabled (private receipt archive)
- Wild Bevy raw records, incremental compression disabled (private receipt archive)
