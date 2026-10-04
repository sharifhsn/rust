# Direct compact Rust artifacts in Wild

This experiment replaces the library's archive of copied object bytes with an
ordered manifest and a shared object store. Wild reads that representation
directly. Rust metadata remains a separate authoritative file, compressed in
indexed chunks and read on demand.

**Result:** the full-feature Bevy workspace retained **1.94 GiB instead of
5.59 GiB**, a **65.3% reduction** with debug information already disabled.
The earlier archive-compression implementation retained 1.99 GiB, so the new
representation adds **2.5% further savings**. It removes expanded Rust linker
inputs and reduces sampled peak disk use substantially, but the speed gap
remains unresolved. The final direct-mode clean build had a severe slowdown;
this prototype is not ready to recommend as a default.

Those numbers are from the historical debug-info-off campaign. The current
`-Cdebuginfo=2`, incremental-on results are in the [full-debug report](../../docs/target-directory-size/full-debug-incremental-results.md).
They measure standard compressed `.rlib`s, direct Wild linking, and an optional
bounded decoded-page cache. The cache reduces sampled process-tree peak RSS but
makes Bevy touch-only relinks slower; it is not enabled by default.

Scope: one branch, ARM64 Linux in an OrbStack container on the Apple Silicon
host. Wild's ELF reader is the implementation target. Mach-O support and stable
Rust integration are outside the tested implementation.

## Representation

| Artifact | Stored representation | Consumer |
|---|---|---|
| `.rlib` | `RCLIB001` manifest: ordered member names, kinds and paths; reference to `.rmeta`; small late link metadata | rustc and modified Wild |
| Object/work product | `RCOBJ001`: compressed ELF metadata, section directory, independently compressed payload blocks | Wild reads metadata eagerly and decodes payload blocks on demand; an optional Unix cache advises old file-backed pages away |
| `.rmeta` | Existing `RUSTZRL1` indexed chunk format, one authoritative copy | New rustc byte reader resolves logical offsets without decoding the whole file |
| Incremental object | Hardlink to the same immutable compact object referenced by the manifest | rustc reuses it directly; Wild consumes it directly |
| Other incremental state | Earlier indexed compression container | Existing compiler cache loader |
| Final debug information | Standard ELF compressed DWARF, Zlib or Zstd | Existing Wild output support and compatible debuggers |

The object metadata preserves ELF symbol definitions, section identities,
visibility, weak/strong binding, COMDAT/group records, notes and architecture
attributes. Code, data, relocation streams and DWARF are placed in payload
blocks. Logical section offsets are tagged references, so a reader that has not
been updated fails instead of treating compact bytes as an ELF payload.

This follows the useful part of Clang's indexed persistence model. Rust already
has lazy values, arrays and tables; replacing their record encoding is not
necessary to give compression the same random access. The new `MemDecoder`
keeps a contiguous window for ordinary reads and refills it when a logical read
crosses a compressed chunk boundary. Cross-chunk borrowed ranges are retained
in stable buffers.

## What costs disappear

- Linking compact Rust libraries creates no expanded `.rlib` or Rust object copy.
- An unused library member needs its symbol/section metadata, but its payload
  stays compressed. Selected members can also leave unused payload blocks alone.
- Incremental work products and library entries share their encoded bytes.
- Metadata queries do not automatically inflate an entire `.rmeta` file.
- Reusing green incremental metadata keeps the compressed inode shared with the
  emitted metadata, without an unpack/repack pass.
- Generating a metadata stub no longer eagerly evaluates the full-metadata
  fallback in `EncodedMetadata::stub_or_full`.

Decoding used code/data still costs CPU and memory. By default, decoded payload
blocks stay pinned until the process finishes, which keeps Wild's borrowed
section slices valid. On Unix, `WILD_COMPACT_CACHE_BYTES` enables an optional
file-backed LRU page-advice mode. It keeps virtual addresses valid while asking
the OS to discard cold mapped pages; the limit covers tracked mapped payload,
not full process RSS or the OS page cache. This reduced sampled Bevy process-tree
RSS by 20%–30% at 1 GiB and 256 MiB settings, respectively, but slowed
touch-only relinks by 35% and 86%. The mappings use unlinked temporary backing
files, so scratch use can approach decoded payload volume. See the report for
the paired measurements and limits.

Blocks group small sections to keep compression effective. A selected small
section can therefore decode nearby discarded sections. A section larger than
the chosen block size remains a single block. Both policies can be improved
independently of the manifest.

The shared store incorporates the duplicate-object elimination part of David
Lattimore's compiler/linker cache proposal. Deferred code generation, MIR-only
rlibs, and incremental linking are separate changes and are not implemented by
this experiment.

Packed foreign-library bundles currently use rustc's existing extraction path;
ordinary native members stored directly in the manifest use Wild's compact
reader. Eliminating the packed-bundle extraction path needs a further change to
how rustc passes native-library selection and whole-archive options to Wild.

## Controls and reproduction

Build the Rust compiler using `bootstrap-linux.toml`; build Wild and `wild-pack`
from the `codex/compact-rust-artifacts` branch in the sibling Wild checkout.
The Rust dependency points to that checkout's `compact-artifact` crate so the
producer and consumer use one format implementation.

Compiler flags for the direct mode:

```text
-Zcompact-artifact-store=/absolute/path/to/target/compact-store
-Zcompress-incremental=yes
-Zartifact-compression-profile=balanced
-Zembed-metadata=no
-Cdebuginfo=0 -Clto=no -Cembed-bitcode=no
-Clinker=clang -Clink-arg=-fuse-ld=/absolute/path/to/wild
```

`fast`, `balanced`, and `small` retain the earlier compression level/block-size
controls: Zstd -1/256 KiB, 3/256 KiB, and 9/1 MiB respectively. The current Bevy
comparison uses `balanced`; it does not retune profiles for the new format.
Full debug information can be used with `-Cdebuginfo=2` and
`-Clink-arg=-Wl,--compress-debug-sections=zstd`. The final DWARF encoding is
standard; compressed native input objects use this experiment's private format.

Run `test_compact_wild.py`, `test_compact_rust.py`, and
`benchmark_compact_wild.py` inside the documented Linux container. The benchmark
counts target, object store and temporary storage together, deduplicated by
inode. It checks the Bevy runtime after every measured build.

The tested Linux layout uses sibling `/work/rust` and `/work/wild` checkouts.
Build commands, from their respective checkout roots:

```sh
CARGO_TARGET_DIR=/tmp/wild-target cargo build --release -p wild-linker -p compact-artifact
python3 x.py build --config tools/compressed-artifacts/bootstrap-linux.toml --stage 1 compiler/rustc library
```

For a fresh benchmark, choose unused output and build directories:

```sh
COMPACT_BENCH_BASE=/tmp/compact-bench-new \
COMPACT_LAB_OUTPUT=/work/rust/docs/target-directory-size/measurements/compact-new \
python3 tools/compressed-artifacts/benchmark_compact_wild.py
```

The harness expects the pinned Bevy checkout at `/work/sources/bevy`, the built
compiler at the path set by `bootstrap-linux.toml`, and the normal Linux Bevy
system dependencies. The tested image is `rust:1.98.1-bookworm` on ARM64, with
Clang/LLD 14, libzstd development headers, Python/psutil and GDB 13 installed.
Exact source snapshots, fixture hashes, lockfile hash, compiler driver hash and
linker hashes are saved beside the measurements.

## Store lifetime

Content addresses identify encoded bytes, not semantic compiler-cache keys.
Compiler query/work-product fingerprints continue to decide whether an object
can be reused. The format doesn't assume independently generated objects are
identical merely because their symbols match.

`compact_store.py TARGET STORE` reports unreachable store entries. It validates
manifests through `wild-pack`, marks all referenced objects, and protects entries
with other hardlinks (including retained incremental generations).
`--delete-quiescent` removes only unreferenced entries with a single link. All
builds and linkers using this target must have finished first. Online collection
needs a producer/collector lease protocol. Measurements include obsolete store
objects before optional collection.

Compiler-produced manifests currently record absolute paths so Cargo's copied
or hardlinked library aliases stay usable. Exporting or moving a target needs
reference rebasing. General staticlib export, cross-crate LTO, thin native
archives, and other object formats require more reader/producer work. This is
an opt-in prototype, not an upstream-ready artifact contract.

## Research decisions

- Separate metadata became the default when both Cargo and rustc are
  nightly/dev in August 2026. Stable behavior is still being evaluated because
  manual `.rlib` users may need the separate `.rmeta`, and Cargo does not uplift
  that file alongside leaf libraries. See the [Cargo change](https://github.com/rust-lang/cargo/pull/17267)
  and [official explanation](https://blog.rust-lang.org/inside-rust/2026/08/18/reducing-target-dir-size-on-nightly/).
- Btrfs/ZFS are useful precedents, not prerequisites for an application-level
  solution. We have no population measurement supporting an adoption claim.
  Filesystem compression is an independent optional optimization. Nested
  compression normally remains correct, but already compressed data offers
  little additional redundancy and may cost more CPU. Logical and allocated
  sizes must be reported separately.
- The [Luna Max research](../../docs/target-directory-size/wild-integration-research.md)
  recommended standard ELF section compression as the smallest initial change.
  This prototype deliberately goes further: compressed metadata and relocations,
  grouped payload blocks, a new library manifest, and shared incremental objects.

## Validation and measurements

Raw records are in
[`2026-09-27-compact-wild`](../../docs/target-directory-size/measurements/2026-09-27-compact-wild).
The `final/` subdirectory records the final compiler build, exact source and
binary hashes, compiler integration tests, and repeated Bevy campaign. The
measurement root preserves the initial campaign and the unchanged Wild tests.

### Correctness

- The ARM64 Linux stage1 compiler and complete sysroot build succeeded.
- All 167 `rustc_data_structures`/`rustc_serialize` unit tests passed, along with
  five doc tests; one existing doc test is ignored. The new tests cover windows
  of 1–32 bytes, cross-window borrows, cursor restoration and rejection of an
  invalid empty decoder window before EOF.
- The tracked-option fingerprint test, four compact format/store tests, and
  159 Wild unit tests passed. The unchanged C/C++ source formatting test was
  excluded because this container has no clang-format. Modified Rust and TOML
  files passed their format checks. The legacy codec's 12 standalone tests also
  passed.
- Direct Wild integration covers TLS, weak/strong symbols, whole-archive,
  section GC, C++ COMDAT and exception unwinding, and corrupt payload handling.
  A corrupt unused archive member is skipped; forcing its selection fails.
- Compiler/Cargo integration covers a manual `.rlib`-only `--extern`, generics
  and nongeneric dependency calls, semantic incremental edits, copied manifests,
  shared object and metadata inodes, native FFI, procedural macros, LTO rejection
  and quiescent store collection. The scripts save 33 and 17 records respectively;
  these include data assertions as well as subprocess checks.

### Debug information

Both Zlib and Zstd compressed final DWARF were linked and read by GDB 13, and a
standard compressed-DWARF object was packed inside the new format and linked
successfully. A Cargo FFI/procedural-macro fixture built with `-Cdebuginfo=2`
and Zstd output compression retained working Rust function line information.
These are functional checks. The large Bevy storage/timing comparison below
keeps debug info disabled and does not establish a large-project DWARF ratio.

### Bevy methodology

The Bevy workspace, version 0.19.1, is pinned at `b56fc29d3016e641754765244b5ba3f9cc504671`.
The probe constructs the full default plugin graph (including 2D/3D, UI and
audio), then runs a headless ECS workload with 1,000 entities and 30 updates.
Every measured build must execute and produce the expected position sum,
559,500. It does not initialize GPU or audio devices. Bevy workspace crates
are local dependencies and get incremental caches; an application consuming
Bevy from the registry would have a different incremental-storage breakdown.

The three configurations use the same final compiler and Wild binaries,
separate target directories, the unoptimized dev profile, debug info off, incremental
compilation on, one metadata copy per library, and cross-crate LTO/embedded
bitcode disabled. Both compression configurations use the balanced profile.
The wrapper configuration also uses the new lazy metadata reader, so the
comparison does not credit the native format for that common reader change.

Runs use native ARM64 Linux in OrbStack on an Apple M5 Pro host, with a container
quota of four CPUs and 12 GiB RAM. Each configuration gets a fresh target, one
clean build, one no-op build and three semantic leaf edits. Disk and process
RSS are sampled at 250 ms. Eight additional edits per configuration rotate
execution order and omit the sampler. Clean-build timings are single samples
per campaign on a shared host with warm source caches, not cold-machine claims.

Retained allocation counts target, object store and scratch together using
unique `(device, inode)` pairs and `st_blocks * 512`. It includes obsolete store
entries before collection and counts final executables and native outputs.
This measures guest filesystem allocation, not physical APFS extents or the
total host VM image. Sampled peaks can miss short events, and summed process
RSS can double-count shared pages. The ordinary/wrapper/compact source and
output graph are otherwise the same.

### Retained storage and timing

| Mode | Allocated GiB | Logical GiB | Clean seconds | No-op seconds | Edit median seconds | Edit range seconds |
|---|---:|---:|---:|---:|---:|---:|
| Ordinary artifacts | 5.592 | 5.561 | 226.42 | 0.289 | 5.067 | 3.869–10.353 |
| Earlier archive compression | 1.991 | 1.957 | 319.80 | 1.343 | 11.326 | 9.086–14.848 |
| Direct compact artifacts | 1.942 | 1.907 | **1949.77** | 1.649 | 9.524 | 6.272–16.187 |

Storage is the retained allocation after the first three edits, before store
collection. Edit statistics use the eight additional runs in rotated order.
All **39 measured builds and runtime checks passed**. Each clean build invoked
rustc 457 times, each no-op build invoked it zero times, and each of the three
sampled edits per mode rebuilt one compilation unit.

The storage result repeated in both campaigns. Most of the total reduction
comes from the earlier compression of metadata, archives and incremental state.
The direct format reduces retained allocation a further 2.47% relative to that
baseline; it is not responsible for the entire 65.27% reduction.

**The timing results do not establish a reliable speed improvement.** In the
final rotated runs, direct mode's median was 15.9% faster than the wrapper and
88.0% slower than ordinary artifacts. It was slower than the wrapper in two of
eight paired edits. The direct clean build took 32.5 minutes, compared with
4.2 minutes in the initial campaign. Ordinary edit times also deteriorated:
the final sampled edits took about 1.1–1.2 seconds, while the later rotated
edits took 3.9–10.4 seconds.

The shared host became less responsive during this campaign. A saved host
snapshot shows about 15.0 GiB of allocated swap; later inspection found the
host running on battery. Neither observation establishes the cause of the
slowdown, and there is no usable stack profile of the slow compilation.
**A code regression has not been ruled out.** Repeat on an otherwise idle,
plugged-in host and profile rustc and Wild separately before making a speed
claim or choosing defaults. The raw slow result is retained rather than
discarded as an outlier.

For comparison, the initial campaign's clean times were 178.50, 216.37 and
252.15 seconds; its four rotated edit medians were 2.645, 3.132 and 1.896
seconds. Those measurements used the preceding compiler binary, before the
decoder-window check and green-metadata reuse fix. They are separate evidence,
not additional samples of the final binary. Source snapshots and binary hashes
for both campaigns are preserved.

### Where the bytes remain

Allocation after the first three edits, in MiB, counting each inode once:

| Category | Ordinary | Archive compression | Direct compact |
|---|---:|---:|---:|
| Libraries and incremental objects, or shared store plus manifests | 2086.43 | 246.21 | 195.82 |
| Other incremental state | 2606.20 | 1115.70 | 1115.69 |
| Rust metadata | 528.56 | 171.61 | 171.61 |
| Executables, build scripts, native outputs and other files | 505.46 | 505.46 | 505.46 |

The object/library category shrinks **90.6%** relative to ordinary artifacts
and **20.5%** relative to archive compression. The direct mode stores 191.66 MiB
of shared objects and 4.16 MiB of manifests. Incremental aliases of these
objects are already included in the shared-store row.

The largest remaining category is incremental state. Compressed
`dep-graph.bin` files alone occupy **906.33 MiB**, about 46% of the retained
target. Query caches occupy another 208.79 MiB. These are stronger candidates
for the next substantial reduction than further shrinking manifests. A new
dependency-graph representation or a cache-retention budget needs its own
implementation and measurements; neither is demonstrated here.

### Temporary disk space and memory

| Sampled peak | Ordinary | Archive compression | Direct compact |
|---|---:|---:|---:|
| Edit allocation, target plus scratch, GiB | 5.742 | 3.540 | 2.280 |
| Clean summed process RSS, GiB | 3.595 | 4.115 | 3.933 |
| Edit summed process RSS, GiB | 1.906 | 2.532 | 2.739 |

Direct consumption reduced the observed edit disk peak **35.6%** relative to
archive compression. It did **not** demonstrate a RAM saving. These sampled
peaks are lower bounds on transient peaks, and summed RSS can count shared
pages repeatedly. The ordinary no-op completed too quickly to produce an RSS
sample; its recorded zero must not be interpreted as zero memory use.

### How much lazy decoding helped

An additional, untimed attribution edit recorded the following:

- Wild opened 10,723 compact objects representing 1,077,836,064 original
  bytes, stored in 159,721,712 bytes. It eagerly decoded 746,729,289 bytes of
  object metadata.
- It decoded 318,930,441 of 333,542,336 available payload bytes: **95.6%**.
  Bevy uses nearly all the available payload, so skipped blocks explain little
  of its result. The unused-member regression test demonstrates that the reader
  can skip payload when the program actually leaves it unused.
- The wrapper mode created 318 metadata-reader instances and decoded
  324,655,653 bytes. Direct mode created 636 instances and decoded
  414,168,138 bytes. Repeated opens doubled the logical-byte total in the direct
  counters; that total is not a measurement of unique metadata. Direct mode
  decoded **more metadata bytes** in this run despite reading a smaller fraction
  per instance. Sharing readers or avoiding duplicate manifest metadata lookups
  is a concrete performance opportunity.

An initial-campaign store census also found 39,235,696 bytes of section
directories and 809,088 bytes of block descriptors across 12,326 objects.
Those directories could be encoded more compactly, but their size puts a low
ceiling on the resulting whole-target saving. Keeping large decoded metadata
and used payload in pinned buffers remains a memory cost of this prototype.

### Collection and optional executable stripping

After all final edits, quiescent collection checked 376 manifests, retained
11,284 referenced objects, protected 11,126 objects with other hardlinks, and
removed 11 unreachable objects: **90,112 allocated bytes**. Restoring and
rebuilding the original probe rebuilt one compilation unit; the following
no-op rebuilt none. The runtime oracle passed. Garbage collection therefore
works on this fixture, but it does not explain the reported size reduction.

Debug information is not the only optional executable data. A temporary copy
of the final Bevy executable shrank from **345.72 MiB to 148.58 MiB** under
`objcopy --strip-all`, saving **197.14 MiB**, and still passed the runtime
oracle. Its original `.strtab` alone contained 186,930,244 bytes, and `.symtab`
contained 19,786,896 bytes. Stripping removes static symbol names and weakens
diagnostics/symbolication. The measured target remains unstripped; this saving
is separate from compression and is not included in the main table.

### Evidence and next decisions

The final compiler driver SHA-256 is
`2dd857a6a009372f141114fd87d14051df4e4a2a04ff5f11ca5f14518c13f3da`;
Wild is `4663aa186825f77eb759dae69f48b24858cb4da3dca850d282cb2a471b06de04`.
The compiler reports `1.99.0-dev`, LLVM 22.1.8, ARM64 Linux. Repository revisions,
dirty source snapshots and all binary hashes are in
[`final/provenance.json`](../../docs/target-directory-size/measurements/2026-09-27-compact-wild/final/provenance.json).
The corresponding records are
[`bevy-analysis.json`](../../docs/target-directory-size/measurements/2026-09-27-compact-wild/final/bevy-analysis.json),
[`bevy-summary.json`](../../docs/target-directory-size/measurements/2026-09-27-compact-wild/final/bevy-summary.json),
[`bevy-confirmation.json`](../../docs/target-directory-size/measurements/2026-09-27-compact-wild/final/bevy-confirmation.json),
and [`gc-and-strip.json`](../../docs/target-directory-size/measurements/2026-09-27-compact-wild/final/gc-and-strip.json).

The architectural change works: Rust and Wild share compressed objects directly,
metadata has an indexed byte reader, incremental objects avoid duplicate stored
copies, and the debugger reads compressed DWARF. To turn it into a practical
default, first resolve the timing regression and duplicate metadata loading;
then attack dependency-graph storage for another large size reduction. Bounded
decoded caches, relocation of exported manifests, and packed foreign-library
handling remain engineering work, alongside the explicitly unsupported LTO
and platform paths.
