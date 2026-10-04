# Lossless compression choices for Rust build artifacts

Prepared 2026-09-26. This note compares codecs and chunk geometry for the local
compressed-artifact prototype. The codec replay and compiler integration/full-
build matrices are complete. The integrated implementation, runtime checks, and
whole-target results are summarized in the
[phase-two report](../../tools/compressed-artifacts/PHASE2.md); the profile
matrix summaries are available for nonincremental dev builds (private receipt archive)
and incremental builds (private receipt archive).

## Profile recommendations from compiler measurements

The matrices cover four consumers (`regex`, `clap`, `serde`, and `syn`) with five
repetitions per arm: 100 nonincremental runs and 120 incremental runs. All arms
use `-Zno-embed-metadata` and disable debug info. `dedup_off` is the runner's
name for its compression-off control; metadata is still stored once in every
arm. The dev matrix sets `CARGO_INCREMENTAL=0`; the incremental matrix sets it
to `1` and enables both artifact and incremental compression in its `both_*`
arms. All runtime-output and no-op checks passed. These profile matrices are
part of the broader 265-run campaign in the phase-two report, which also covers
release builds and a native-library compatibility fixture.

The table reports the median across the four workload-level paired medians;
parentheses give the range across those workloads. Allocation is measured for
the retained target and explicit temporary directory after two checked semantic
edits. Wall-time overhead is for the clean build relative to the matching
compression-off arm.

| Profile | Use | Dev: retained allocation saved after edits; clean-build wall overhead | Incremental, both modes: retained allocation saved after edits; clean-build wall overhead |
|---|---|---|---|
| Fast: Zstd -1 / 256 KiB | Frequent developer builds where encode cost matters | 37.6% (34.8–44.3); +1.6% (+1.3–3.4) | 50.0% (48.9–52.2); +7.7% (+6.5–9.1) |
| Balanced: Zstd 3 / 256 KiB | Compiler implementation default | 39.4% (36.6–46.8); +2.9% (+1.9–3.7) | 53.1% (52.1–55.6); +8.8% (+6.5–12.9) |
| Small: Zstd 9 / 1 MiB | Capacity priority | 40.8% (37.7–48.7); +5.1% (+3.4–8.1) | 55.1% (54.3–58.0); +19.6% (+13.0–28.6) |

Among these three profiles, fast had the lowest median wall overhead on clean
dev builds and on both semantic-edit rebuilds: +8.6% and +9.2%, compared with
+10.2% and +10.5% for balanced. With incremental compilation and both
compression modes enabled, semantic-edit overheads were +18.4% and +13.7% for
fast, +21.6% and +21.1% for balanced, and +29.9% and +31.9% for small. Balanced
buys 1.8 percentage points of retained dev allocation savings and 3.1 points with
incremental compression over fast, for 1.3 and 1.1 points of additional clean-build wall overhead, respectively. Small saves only 1.4 additional percentage points of retained allocation in dev builds and
2.0 points with incremental compression over
balanced; its clean-build overhead rises from 2.9% to 5.1% for dev and from
8.8% to 19.6% for incremental builds. These results
make fast useful for frequent development builds, keep balanced as the general
default, and reserve small for users who prioritize disk capacity.

The codec-only replay below remains a separate measurement. It compresses
selected retained `.rlib`, `.rmeta`, and incremental files in memory; its saved
bytes and codec timings do not predict the total target-directory reduction or
Cargo build overhead. The whole-target matrix includes uncompressed executables,
other files, and the explicit temporary directory.

Zstandard remains the reference codec: it has a direct C API already used by the
prototype, spans fast negative levels through high-ratio levels, supports
reusable contexts, and emits independent frames for externally indexed chunks.

The v1 prototype accepts power-of-two chunk sizes from 16 KiB through 4 MiB.
Thus 4 MiB is a valid large-chunk candidate, while `whole`-artifact compression
is only a theoretical upper bound: it is not representable for ordinary large
artifacts under the current chunk-size validation. Zstd 19, LZ4 HC 9, Brotli 7,
libdeflate 12, and LZMA 0/3 are controls for the size/CPU frontier, not proposed
defaults. The integration table reflects four small Rust consumers; it does not
establish behavior for larger workspaces, other platforms, or C/C++ builds.

In the 70-artifact codec replay, zstd3/1 MiB saves only 0.94 percentage points more than
zstd3/256 KiB, with nearly identical codec time. It is a defensible capacity
choice while metadata consumers remain eager. The 256 KiB setting keeps the
modeled three-range decode work much lower and leaves room for future range
readers. Zstd3/4 MiB gains just 0.42 points over 1 MiB but decodes substantially
more for a small range and has higher process RSS. The measurements below make
the tradeoff explicit.

## What the format asks the codec to do

The current local format has a 64-byte header and a 24-byte index record per
chunk. It stores each chunk as a standalone frame/block, keeps a chunk raw if its
compressed payload is not smaller, and keeps the entire original file when the
header/index and candidate payload would not save bytes. The chunk size is in the
header. The source is
[`artifact_compression.rs`](../../compiler/rustc_data_structures/src/artifact_compression.rs#L20).

Independent chunks make bounded reads possible, but a small read still decodes
the complete chunk it intersects. A 4 KiB lookup into a 64 KiB chunk can decode
up to 16 times the requested bytes; at 256 KiB the upper bound is 64 times, and
at 1 MiB it is 256 times. A whole-file block can require decoding the whole
artifact. Larger chunks reduce index bytes and often improve compression, while
raising memory use and read amplification. Those are geometry bounds, not
observed workloads: the current metadata consumer calls the eager loader, and
the rlib path materializes a conventional archive before linking. The local
implementation paths are
[metadata packing](../../compiler/rustc_metadata/src/fs.rs#L57),
[rlib packing](../../compiler/rustc_codegen_ssa/src/back/link.rs#L223),
[rlib materialization](../../compiler/rustc_codegen_ssa/src/back/link.rs#L382), and
[incremental work-product restoration](../../compiler/rustc_codegen_ssa/src/back/write.rs#L930).
The range API is not evidence of a production random-read benefit until callers
use it.

The corpus runner measures codec work and a modeled container using CPython:
most codecs are called through `ctypes`, while zlib, Brotli, and LZMA use Python
wrappers. It excludes file I/O, filesystem/APFS allocation, Cargo/rustc
scheduling, and a complete build. Python-side allocation and call overhead can
change small-block timings, so the sweep screens candidates; it does not predict
the Rust implementation's overhead or end-to-end build time.
It reports full sequential decode and three 4 KiB range probes separately. Its
range rows count the bytes in independent chunks that need decoding; they do not
model file opens, index parsing, disk seeks, or cache state. A whole-file run is
an upper-bound codec comparison only.

## Codec comparison

| Method | Useful properties | Costs and fit for compiler artifacts | In this sweep |
|---|---|---|---|
| **Zstandard** | Negative levels offer a fast end; levels 1–19 cover common tradeoffs; mature frame format; reusable C contexts; optional trained dictionaries | Higher levels consume more time and memory. The base frame has no random-access index, so the Rust container must carry chunk offsets. | `-1`, `1`, `3`, `9`, `19`; chunks independently compressed with reused context allocation |
| **LZ4 block / HC** | Very fast raw-block path; caller supplies stored and decoded lengths; HC searches harder for a smaller block | Fast mode tends to store more bytes. HC is slower and its level range is 1–12. Linked-frame blocks depend on prior history; use independent blocks for range isolation. | Fast acceleration 1/4 and HC levels 3/9; raw independent blocks |
| **Snappy** | Simple, fast lossless codec with a stable format and C API | Optimizes speed more than compression ratio; no quality knob in the tested default C call. | Native C binding default |
| **DEFLATE via zlib** | Very widely supported; level 1 is faster, level 6 is the documented default, level 9 seeks a smaller result | Stream/checksum wrappers are unnecessary inside this checksummed container; raw DEFLATE needs external size and integrity metadata. The stock zlib API is not the fastest implementation. | Python zlib raw DEFLATE levels 1/6/9 |
| **DEFLATE via libdeflate** | Whole-buffer API is optimized for speed; convenient known-output-size decompression; raw blocks fit the outer index | No streaming API; context cost and memory belong in the comparison. Its output can vary across library versions, so compare decoded hashes, not compressed golden bytes. | Native libdeflate raw blocks levels 1/6/9/12 |
| **Brotli** | Often useful for high-ratio distribution and supports streaming APIs | High qualities can spend substantial CPU. Generic build-cache chunks are not web-text responses; only measurements on the actual corpus can justify it. Store external checksum/size in the container. | Python binding qualities 1/4/7, window 22 |
| **LZMA2** | Strong ratio control; available in Python's standard library | Its memory and decode cost can be large relative to developer-cache value. It is a boundary/control, not a likely incremental default. | Raw LZMA2 presets 0/3 |
| **LZFSE (Apple Compression)** | Native Apple framework buffer API and a streaming API; relevant to this Apple Silicon host | An Apple-specific integration would add platform and portability branches. Local timing is not evidence for Linux or Windows. | macOS Compression framework LZFSE buffer API |

All block codecs in the runner are wrapped as independent chunks. This matters:
using one stream for the full artifact could improve ratio by referencing prior
bytes, but it would make arbitrary chunk reads depend on preceding history and
would alter decoder memory/parallelism. A codec's streaming interface does not
automatically provide random access.

### Zstandard details

The [Zstandard 1.5.7 C API manual](https://github.com/facebook/zstd/blob/v1.5.7/doc/zstd_manual.html)
documents compression levels, contexts, and dictionary APIs. Negative levels
favor speed; the default is level 3. Reusing a `CCtx`/`DCtx` can reduce setup and
allocation costs without changing the compression result. Contexts should be
owned per thread. The prototype creates one context pair per artifact and calls
the one-shot context API for each independent frame. Reuse avoids repeated
context setup; it does not create cross-chunk history.

The [frame format](https://github.com/facebook/zstd/blob/v1.5.7/doc/zstd_compression_format.md)
defines independently decodable frames, but it does not define an index for
finding byte ranges in a concatenation. Zstandard's separate
[seekable format](https://github.com/facebook/zstd/blob/v1.5.7/contrib/seekable_format/README.md)
adds independent frames and a seek table. The prototype instead stores offsets
and per-chunk checksums in its own format. The
[IETF Zstandard specification, RFC 8878](https://www.rfc-editor.org/rfc/rfc8878.html),
standardizes the frame format; it is an informational RFC, not an IETF Standards
Track document.

Zstandard's `windowLog` bounds the maximum back-reference distance as a power
of two. A larger window can improve ratio but raises the decoder's memory
budget; with independently compressed chunks, a chunk's source length already
caps how far the encoder can refer within that frame. The initial sweep leaves
window parameters at the library default and varies chunk geometry instead.
Window tuning can follow if 1–4 MiB chunks show a useful compression gain that
does not justify the decoder-memory cost. The C API chooses parameters based on
level and known source size unless advanced parameters are set.

Zstandard dictionaries can help collections of small, similar inputs, but a
shared compiler-artifact dictionary would need a stable version/hash, lifecycle,
availability before decode, and validation against multiple compiler and
dependency versions. Dictionaries are not deduplication: they do not remove a
second copy of bytes or make artifact keys interchangeable. The first sweep
therefore tests no dictionaries. Consider a trained dictionary only if actual
per-class results show many small records that remain poorly compressed and a
representative training set can be pinned and distributed safely.

### SIMD and target-specific implementations

SIMD generally changes the implementation and throughput, not the container
design. The tested Zstd, LZ4, libdeflate, and Apple framework builds are the
native libraries present on this host; the output records their library paths
and version strings where APIs expose them. [zlib-ng's upstream README](https://github.com/zlib-ng/zlib-ng)
documents architecture-specific intrinsics such as ARM NEON, but no zlib-ng
library is in this sweep. Apple Compression's implementation is also host- and
OS-specific. A result from this aarch64 macOS host should not be projected onto
x86-64 or Linux without a second run.

### Other candidates

- [LZ4's raw API](https://github.com/lz4/lz4/blob/v1.10.0/lib/lz4.h) exposes
  `LZ4_compress_fast` with an acceleration parameter and
  `LZ4_decompress_safe` with externally supplied sizes. The
  [HC API](https://github.com/lz4/lz4/blob/v1.10.0/lib/lz4hc.h) trades more search
  time for ratio. The [frame format](https://github.com/lz4/lz4/blob/v1.10.0/doc/lz4_Frame_format.md)
  distinguishes independent blocks from linked blocks; only independent blocks
  fit the range and parallel-decode requirement without prior history. The
  [block format](https://github.com/lz4/lz4/blob/v1.10.0/doc/lz4_Block_format.md)
  relies on external framing and sizes, which this container already carries.
- [Snappy's upstream README](https://github.com/google/snappy/blob/main/README.md)
  describes a fast codec whose compression ratio is secondary. Its old headline
  throughput figures are not used as predictions; local runs use the installed
  C library and the actual corpus.
- The [zlib manual](https://www.zlib.net/manual.html) documents levels 1–9 and
  streaming. [libdeflate](https://github.com/ebiggers/libdeflate) instead offers
  optimized whole-buffer DEFLATE APIs, common compression levels through 12, and
  caller-allocated contexts. [zlib-ng](https://github.com/zlib-ng/zlib-ng)
  documents architecture-specific intrinsics, including ARM NEON; it is a
  possible future implementation, not a codec tested here.
- The [Brotli encoder API](https://github.com/google/brotli/blob/master/c/include/brotli/encode.h)
  exposes quality and window controls; the
  [decoder API](https://github.com/google/brotli/blob/master/c/include/brotli/decode.h)
  supports streaming. The [format RFC 7932](https://datatracker.ietf.org/doc/html/rfc7932)
  describes the stream and its history/window model. Streaming decode still
  needs independent chunks and external offsets to bound a range read.
- Python's [LZMA API](https://docs.python.org/3/library/lzma.html) documents
  presets 0–9 and warns that high presets may use hundreds of MiB. This sweep
  uses raw LZMA2 blocks because the container already provides framing and
  integrity; the compression preset is supplied again at decode.
- Apple's [LZFSE API overview](https://developer.apple.com/documentation/compression/compression_lzfse?language=objc_3)
  documents buffer and streaming operations. The open-source
  [LZFSE reference implementation](https://github.com/lzfse/lzfse) provides a
  second implementation reference. The sweep records the Apple framework
  algorithm identifier and scratch sizes; results apply only to the captured
  host/runtime.

## Corpus and repeatable sweep

The primary manifest is
`corpus/manifest.json` (private receipt archive):
70 logical artifacts, 63,598,721 bytes, consisting of 35 `.rlib` and 35 `.rmeta`
files from four no-debug, nonincremental dev consumers. The profile is
`debug=0, incremental=false, metadata-once`. Common dependencies intentionally
appear once per consumer to represent the measured project mix. They are not
deduplicated for this codec experiment.

The secondary manifest is
`incremental-corpus/manifest.json` (private receipt archive):
1,800 inode-unique files and 284,153,953 logical bytes after two body edits.
It contains 1,720 `.o`, 69 `.bin`, and 11 `.rmeta` files. The captured classes
include dep-graph records, query caches, objects, rmeta and work products. This
corpus should be tested with a smaller candidate set because it is over four
times the artifact bytes of the first manifest.

The script
[`compression-sweep.py`](../../tools/compressed-artifacts/compression-sweep.py)
records its own SHA-256, exact command, source manifest and hashes, runtime
library identities, per-trial logs, and JSON results. Each codec/configuration
runs in an isolated child process. Every source file is verified against the
manifest before and after the sweep; each trial verifies every decompressed
artifact's SHA-256. Results distinguish candidate compressed payload from
modeled stored bytes, including header/index overhead and raw fallbacks. RSS is
the worker process high-water, not a codec-only allocation measure. Zstd and
Apple Compression report persistent context/scratch sizes where their APIs
allow; these exclude temporary buffers and static library tables.

The completed screen ran 19 codec/level choices once at `whole`, 64 KiB,
256 KiB, 1 MiB, and 4 MiB (95 isolated workers). `whole` is a codec bound only.
The slow/size controls (LZMA 0/3, Brotli 7, libdeflate 12) ran separately at
256 KiB and 1 MiB (8 workers). Zstd levels -1, 3, and 9 at all four supported
geometries received five repetitions in randomized order (60 workers). The
incremental corpus received three repetitions of fast, balanced, small, and
legacy Zstd settings plus LZ4-fast and Snappy comparisons (18 workers). All
181 native workers completed with full artifact-hash round trips and no
timeouts or failures. Incremental results are reported by
artifact class below; do not use a 70-file dev ratio as a proxy for
incremental-cache compression.

### Measured artifact results

The screen and finalists used native arm64 CPython 3.14.6 on macOS 26.6.2 with
Zstandard 1.5.7, LZ4 1.10.0, Brotli 1.2.0, libdeflate from
`/opt/homebrew/lib/libdeflate.dylib`, Snappy from
`/opt/homebrew/lib/libsnappy.dylib`, and Apple's Compression framework. All
runs rechecked the 70-file manifest before and after: 63,598,721 logical bytes,
no changed or failed hashes. The screen took 123.5 s for 95 workers; the
slow-control run took 29.1 s for 8 workers; the repeated finalist run took 27.0
s for 60 workers. These elapsed times include one Python process per worker,
source hashing, and wrapper overhead; codec columns below sum the timed
per-artifact pack/unpack calls.
The sweep script SHA-256 was
`533e5dfdc55695dc1fbc7fd76ca09a762df6ed205010daae5f3cc93bfb89e19b`; each
`run.json` contains the complete environment record and command.
The raw records are in the screen run (private receipt archive)
and summary (private receipt archive),
size-control run (private receipt archive)
and summary (private receipt archive),
and five-repetition finalist run (private receipt archive)
and summary (private receipt archive),
with per-worker logs and artifact-level hashes beside each file.

Five-run medians for the requested profile candidates:

| Candidate | Stored logical bytes | Saved | Pack wall / CPU | Full decode wall / CPU | Modeled 3×4 KiB range decode | Worker peak RSS |
|---|---:|---:|---:|---:|---:|---:|
| Fast: Zstd -1 / 256 KiB | 19,951,013 | 68.63% | 0.070 / 0.069 s | 0.031 / 0.031 s | 28.9 MB | 57.6 MiB |
| Existing-format control: Zstd 3 / 64 KiB | 18,259,839 | 71.29% | 0.093 / 0.093 s | 0.043 / 0.043 s | 9.6 MB | 57.2 MiB |
| Balanced: Zstd 3 / 256 KiB | 17,639,343 | 72.26% | 0.101 / 0.101 s | 0.043 / 0.043 s | 28.9 MB | 57.7 MiB |
| Zstd 3 / 1 MiB | 17,044,074 | 73.20% | 0.100 / 0.099 s | 0.042 / 0.041 s | 74.1 MB | 63.0 MiB |
| Zstd 3 / 4 MiB | 16,775,412 | 73.62% | 0.104 / 0.104 s | 0.041 / 0.041 s | 164.9 MB | 83.3 MiB |
| Small: Zstd 9 / 1 MiB | 16,011,832 | 74.82% | 0.508 / 0.505 s | 0.041 / 0.040 s | 74.1 MB | 70.0 MiB |
| Zstd 9 / 4 MiB | 15,511,599 | 75.61% | 0.512 / 0.509 s | 0.040 / 0.039 s | 164.9 MB | 88.0 MiB |

The requested ranges total 819,296 bytes for this corpus. At 256 KiB, the
modeled decoder processes 35 times that amount; at 1 MiB, 90 times; at 4 MiB,
201 times. The ratio columns count the format header/index and raw fallbacks,
but not filesystem allocation or APFS shared extents. The RSS column is the
median process high-water across five worker processes, not native codec-only
memory.

For eager consumers, Zstd 3 / 1 MiB adds 0.94 percentage points of savings over
256 KiB with virtually unchanged pack/decode time. For a balanced default,
256 KiB is still reasonable: it sacrifices under one percentage point while
cutting modeled range decode by about 2.6× and RSS by about 5 MiB. Zstd 3 / 4
MiB adds only 0.42 points over 1 MiB, while more than doubling modeled range
decode and adding about 20 MiB RSS. Among the five-repetition finalists, Zstd 9
/ 4 MiB gives the smallest container, but gains only 0.79 points over Zstd 9 /
1 MiB at roughly five times the pack CPU of Zstd 3 and higher RSS. The
single-pass screen also tried Zstd 19: at 4 MiB it saved 78.85% with 9.31
seconds pack time and 139.3 MiB worker peak RSS; whole-artifact Zstd 19 saved
79.06% with 9.62 seconds pack time and 203.0 MiB peak RSS. These high-ratio
controls received only one screening run; the whole-artifact result is not
representable by the current container. These are codec-only profile
tradeoffs, not measured rustc overhead.

The screen also shows the codec frontier. At 256 KiB, Zstd -1 saves 68.63% in
70 ms, versus LZ4 fast (acceleration 1) at 59.86% in 60 ms. Zstd level 3 saves
72.26% in 101 ms. Brotli 4 saves 73.81% at 391 ms and decodes more slowly than
Zstd 3; libdeflate 6 saves 72.50% at 321 ms. LZFSE saves 71.72% at 351 ms. In
this run these alternatives do not beat Zstd 3 on both size and time.

The separate controls at 1 MiB show the cost of stronger codecs:

| Control | Stored logical bytes | Saved | Pack wall / CPU | Decode wall / CPU | Worker peak RSS |
|---|---:|---:|---:|---:|---:|
| LZMA 3 | 13,043,549 | 79.49% | 1.899 / 1.889 s | 0.412 / 0.409 s | 72.9 MiB |
| Brotli 7 | 14,266,545 | 77.57% | 0.959 / 0.950 s | 0.105 / 0.105 s | 80.0 MiB |
| libdeflate 12 | 16,822,953 | 73.55% | 8.211 / 8.156 s | 0.052 / 0.052 s | 58.6 MiB |

LZMA 3 could suit batch or cold data when decode latency is acceptable, but its
decode time is about ten times Zstd 3 / 1 MiB here. Brotli 7 spends about nine
times the Zstd 3 pack CPU for only 4.37 additional percentage points saved and
slower decode. libdeflate 12 is much slower to encode than the tested Zstd
levels and does not approach their size savings. The incremental-corpus scan
does not establish a profile for frequently rewritten incremental data: it
recompresses the full captured cache, while rustc usually writes only the cache
records affected by a change.

### Incremental-cache results

The incremental manifest has 1,800 unique-inode files totaling 284,153,953
logical bytes after two body edits. The 18 trials took about 24 seconds
across three commands. Each command rehashed the full manifest before and after;
all workers verified the SHA-256 of all decoded files. This is a capacity and
codec-cost replay over retained files. It is not a measurement of actual rustc
write amplification, cache reuse, or edit-build latency.

Three-run medians by candidate:

| Candidate | Stored logical bytes | Saved | Pack wall / CPU | Full decode wall / CPU | Worker peak RSS |
|---|---:|---:|---:|---:|---:|
| Fast: Zstd -1 / 256 KiB | 124,345,273 | 56.24% | 0.296 / 0.295 s | 0.132 / 0.132 s | 144.7 MiB |
| Balanced: Zstd 3 / 256 KiB | 113,405,176 | 60.09% | 0.481 / 0.479 s | 0.199 / 0.198 s | 142.5 MiB |
| Small: Zstd 9 / 1 MiB | 106,279,110 | 62.60% | 2.299 / 2.288 s | 0.189 / 0.188 s | 151.1 MiB |
| Legacy: Zstd 3 / 64 KiB | 118,801,105 | 58.19% | 0.457 / 0.454 s | 0.210 / 0.209 s | 147.4 MiB |
| LZ4 fast 1 / 256 KiB | 147,660,552 | 48.04% | 0.240 / 0.239 s | 0.061 / 0.060 s | 144.4 MiB |
| Snappy / 256 KiB | 146,706,902 | 48.37% | 0.217 / 0.216 s | 0.075 / 0.075 s | 144.9 MiB |

Class savings by candidate:

| Class | Raw bytes | Zstd -1 / 256 KiB | Zstd 3 / 64 KiB | Zstd 3 / 256 KiB | Zstd 9 / 1 MiB |
|---|---:|---:|---:|---:|---:|
| Dep-graph | 156,459,730 | 47.03% | 48.85% | 50.79% | 53.44% |
| Objects | 22,409,712 | 70.82% | 73.97% | 74.07% | 75.82% |
| Query cache | 85,215,240 | 67.95% | 69.61% | 72.03% | 74.67% |
| Rmeta | 19,918,663 | 62.25% | 65.13% | 66.42% | 68.17% |
| Work products | 150,608 | 35.87% | 42.76% | 42.76% | 42.95% |

The dep-graph class is over half the raw bytes but saves about 51% at balanced
settings, less than object and query-cache files. Moving from 64 KiB to 256 KiB
improves total savings by 1.90 points for Zstd 3. Pack time rises about 5%
(0.457 to 0.481 s), while full-decode time drops about 5% (0.210 to 0.199 s)
on this replay. Zstd 9 / 1 MiB gains another 2.51 points over Zstd 3 /
256 KiB, with about 1.8 seconds more pack CPU for this full-cache replay. Its
full-decode time is slightly lower in this measurement, but the compressor's
extra work makes it a capacity-oriented candidate until compiler edit-cycle
cost is measured.

The LZ4-fast and Snappy rows pack somewhat faster than Zstd -1 on this complete
corpus, but save about eight percentage points less. Their results support
keeping them as low-latency comparisons; they do not replace Zstd for the
balanced/small profiles on these files.

Raw run and summary JSON, commands, and per-worker records are in the
fast/balanced run (private receipt archive)
and summary (private receipt archive),
small run (private receipt archive)
and summary (private receipt archive),
and legacy run (private receipt archive)
and summary (private receipt archive).

An initial run under plain `uv run python` selected an x86_64 Python runtime,
which could not load the host's arm64 Homebrew libraries. That run completed 20
Python-wrapper rows and failed 75 native-library rows; all 95 are excluded. It
is preserved at
`screen-20260926` (private receipt archive).
Corrected runs used an explicit native interpreter and record
`platform.machine()==arm64` in each `run.json`.

Illustrative commands from the checkout root (timing requires an idle,
reserved machine):

```sh
rtk uv run --no-project --python /opt/homebrew/bin/python3 python tools/compressed-artifacts/compression-sweep.py \
  --manifest docs/target-directory-size/measurements/2026-09-26-compression/corpus/manifest.json \
  --out docs/target-directory-size/measurements/2026-09-26-compression/sweep-artifacts/screen-native-20260926 \
  --profiles screen --chunk-sizes whole 64KiB 256KiB 1MiB 4MiB \
  --repetitions 1 --timeout-seconds 300

rtk uv run --no-project --python /opt/homebrew/bin/python3 python tools/compressed-artifacts/compression-sweep.py \
  --manifest docs/target-directory-size/measurements/2026-09-26-compression/corpus/manifest.json \
  --out docs/target-directory-size/measurements/2026-09-26-compression/sweep-artifacts/size-controls-native-20260926 \
  --codec-spec lzma=0 lzma=3 brotli=7 libdeflate=12 \
  --chunk-sizes 256KiB 1MiB --repetitions 1 --timeout-seconds 300

rtk uv run --no-project --python /opt/homebrew/bin/python3 python tools/compressed-artifacts/compression-sweep.py \
  --manifest docs/target-directory-size/measurements/2026-09-26-compression/corpus/manifest.json \
  --out docs/target-directory-size/measurements/2026-09-26-compression/sweep-artifacts/finalists-zstd-native-20260926 \
  --codec-spec zstd=-1 zstd=3 zstd=9 --chunk-sizes 64KiB 256KiB 1MiB 4MiB \
  --repetitions 5 --seed 20260926 --timeout-seconds 300
```

The incremental-corpus replay used three commands, each with three repetitions
and a 300-second per-worker timeout. These reproduce the retained-cache codec
measurements; they do not reproduce rustc's selective writes after source edits.

```sh
rtk uv run --no-project --python /opt/homebrew/bin/python3 python tools/compressed-artifacts/compression-sweep.py \
  --manifest docs/target-directory-size/measurements/2026-09-26-compression/incremental-corpus/manifest.json \
  --out docs/target-directory-size/measurements/2026-09-26-compression/sweep-artifacts/incremental-fast-balanced-native-20260926 \
  --codec-spec zstd=-1 zstd=3 lz4=fast:1 snappy=default \
  --chunk-sizes 256KiB --repetitions 3 --seed 20260926 --timeout-seconds 300

rtk uv run --no-project --python /opt/homebrew/bin/python3 python tools/compressed-artifacts/compression-sweep.py \
  --manifest docs/target-directory-size/measurements/2026-09-26-compression/incremental-corpus/manifest.json \
  --out docs/target-directory-size/measurements/2026-09-26-compression/sweep-artifacts/incremental-small-native-20260926 \
  --codec-spec zstd=9 --chunk-sizes 1MiB \
  --repetitions 3 --seed 20260926 --timeout-seconds 300

rtk uv run --no-project --python /opt/homebrew/bin/python3 python tools/compressed-artifacts/compression-sweep.py \
  --manifest docs/target-directory-size/measurements/2026-09-26-compression/incremental-corpus/manifest.json \
  --out docs/target-directory-size/measurements/2026-09-26-compression/sweep-artifacts/incremental-legacy-native-20260926 \
  --codec-spec zstd=3 --chunk-sizes 64KiB \
  --repetitions 3 --seed 20260926 --timeout-seconds 300
```

Record host load and any background compiler activity with future runs. Do not
mix measured codec speed with modeled disk savings or call modeled logical
bytes allocated/physical bytes.

## Source ledger

All links below point to primary specifications, project source, or vendor API
documentation. Links were checked for this note on 2026-09-26; a link to a
repository's current branch can move as that project changes.

| Source | Date/version | Verified claim used here | Limitation |
|---|---|---|---|
| [Zstandard C API manual](https://github.com/facebook/zstd/blob/v1.5.7/doc/zstd_manual.html) | Zstd 1.5.7 docs | Level and context APIs; dictionaries; context reuse | Does not compare performance on Rust artifacts |
| [Zstandard compression format](https://github.com/facebook/zstd/blob/v1.5.7/doc/zstd_compression_format.md) | Zstd 1.5.7 docs | Frame format and independent frame decoding | Does not provide a concatenated-frame seek index |
| [Zstandard seekable format](https://github.com/facebook/zstd/blob/v1.5.7/contrib/seekable_format/README.md) | Zstd 1.5.7 contribution | Independent frames plus seek table | Separate format/convention; not used by local prototype |
| [RFC 8878](https://www.rfc-editor.org/rfc/rfc8878.html) | 2021, Informational RFC | Standardized Zstd frame representation | Informational status; no artifact benchmark |
| [LZ4 block API](https://github.com/lz4/lz4/blob/v1.10.0/lib/lz4.h), [HC API](https://github.com/lz4/lz4/blob/v1.10.0/lib/lz4hc.h) | LZ4 1.10.0 | Fast acceleration and HC level controls; size-bounded decode | Raw blocks require the outer format's sizes |
| [LZ4 frame format](https://github.com/lz4/lz4/blob/v1.10.0/doc/lz4_Frame_format.md), [block format](https://github.com/lz4/lz4/blob/v1.10.0/doc/lz4_Block_format.md) | LZ4 1.10.0 | Linked versus independent blocks; block format needs framing | No end-to-end compiler measurements |
| [Snappy README](https://github.com/google/snappy/blob/main/README.md), [C API](https://github.com/google/snappy/blob/main/snappy-c.h) | Current upstream docs | Default C API and speed-oriented design | Upstream throughput numbers are hardware-specific and not reused |
| [zlib manual](https://www.zlib.net/manual.html) | Current official manual | Compression levels, streaming API | Not specific to compiler artifacts |
| [libdeflate README](https://github.com/ebiggers/libdeflate), [header](https://github.com/ebiggers/libdeflate/blob/master/libdeflate.h) | Current upstream docs | Optimized whole-buffer DEFLATE interfaces and level range | Results depend on installed build and CPU |
| [Brotli encoder](https://github.com/google/brotli/blob/master/c/include/brotli/encode.h), [decoder](https://github.com/google/brotli/blob/master/c/include/brotli/decode.h) | Current upstream API | Quality/window settings; streaming decode | Not a claim that Brotli is suitable for interactive build caches |
| [RFC 7932](https://datatracker.ietf.org/doc/html/rfc7932) | 2016, Informational RFC | Brotli format/window behavior | Does not prescribe cache block layout |
| [Python LZMA docs](https://docs.python.org/3/library/lzma.html) | Current Python docs | Presets and memory caveats | Wrapper docs; sweep uses raw LZMA2 |
| [Apple Compression LZFSE](https://developer.apple.com/documentation/compression/compression_lzfse?language=objc_3), [LZFSE source](https://github.com/lzfse/lzfse) | Current Apple docs and upstream source | Buffer/stream APIs and LZFSE reference implementation | Vendor docs do not establish Linux/Windows availability |
| [zlib-ng README](https://github.com/zlib-ng/zlib-ng) | Current upstream README | SIMD/intrinsic support as future DEFLATE option | Not included in installed-codec timing sweep |
