# Rust artifact compression: CPU and compatibility findings

## Current status

The optimized codec passes its direct correctness suite, and both readers accept the saved legacy 64 KiB containers. Five paired reader/component timing runs are complete; full compiler build results are still needed to assess total compile-time cost.

## Hot-path changes

The codec now reuses one Zstandard compression or decompression context for all chunks in a file. It also reuses input, compressed-input, and range-read buffers. Full-file decode allocates the final output once and decompresses each frame directly into its slice, avoiding a temporary decoded `Vec` and the copy into the final output. Readers open the artifact once; full reads and unpack stream sequentially across the contiguous payload, while range reads seek once to the first intersecting chunk.

These changes follow the Zstandard 1.5.7 C API guidance: repeated operations can reuse `CCtx` and `DCtx` objects; reuse is a speed and resource optimization that does not change the compression ratio, and contexts should remain per-thread. `ZSTD_compressCCtx` mirrors the simple `ZSTD_compress` call at the requested level. [Zstandard 1.5.7 API header](https://github.com/facebook/zstd/blob/v1.5.7/lib/zstd.h)

The writer now tracks a checked output offset, starting at the fixed header length and advancing by each stored chunk length. This removes the old `stream_position()` query inside every chunk iteration and the additional query before writing the index. The current measurement is still needed to establish whether this change affects build time.

The writer skips files of 88 bytes or less. A nonempty v1 container needs a 64-byte header, a 24-byte index entry, and at least one payload byte, so it cannot be smaller than those files. It retains the existing raw-chunk fallback whenever compressed bytes are not smaller.

The standalone codec's `pack()` helper keeps its level-3, 64 KiB default. The compiler's opt-in `-Zcompress-incremental` path uses the selected unstable profile; its current `balanced` profile is level 3 with 256 KiB chunks. Supported chunk sizes are powers of two from 16 KiB through 4 MiB. The v1 header carries the selected chunk size, and the reader uses it for chunk-count validation, decode bounds, and ranges. New readers continue to read old 64 KiB v1 containers. Older readers that hard-code 64 KiB will not understand containers written with a nondefault chunk size.

CRC32 uses runtime-selected AArch64 CRC instructions where the `crc` feature is available, with the portable slicing-by-8 implementation as fallback. The codec uses the IEEE `__crc32d`/`__crc32w`/`__crc32h`/`__crc32b` intrinsics. It does not use the similarly named `__crc32cd` family, which computes CRC-32C. Rust documents both the AArch64 intrinsics and runtime feature detection. [AArch64 `__crc32d`](https://doc.rust-lang.org/stable/core/arch/aarch64/fn.__crc32d.html), [AArch64 `__crc32cd` (CRC-32C)](https://doc.rust-lang.org/stable/core/arch/aarch64/fn.__crc32cd.html), [AArch64 runtime feature detection](https://doc.rust-lang.org/stable/std/arch/macro.is_aarch64_feature_detected.html). The x86 SSE4.2 CRC instruction is also CRC-32C, so it is not a correct implementation of this file checksum. [Rust x86-64 `_mm_crc32_u64`](https://doc.rust-lang.org/stable/core/arch/x86_64/fn._mm_crc32_u64.html)

## Correctness and reader comparison

On the current Apple Silicon host, direct `rustc --test` passes 12 tests. Coverage includes byte-aligned and tail CRC cases, hardware-to-portable CRC agreement, default and custom chunk sizes, cross-chunk ranges, round-trip pack/unpack, raw fallback, small-file bypass, metadata preservation, and malformed, truncated, or corrupted containers.

Both the build-5 control reader and the optimized reader validated the same saved regex `dev` artifact corpus: 10 `.rlib` and `.rmeta` files, 19.42 MiB decoded and 5.86 MiB packed. The optimized source includes the checked output-offset change. The exact toolchain, linked libzstd, commands, source hashes, and raw test output are in `codec-validation` (private receipt archive).

Five alternating control/optimized pairs completed on the saved regex `dev` corpus. Each helper invocation ran 20 loops for each row. The sample loader had already read and validated the artifacts, so the reader result is a warm-page-cache measurement; it includes opening the files, parsing the container, and decoding, but does not represent cold-storage throughput. The raw logs, pair order, binary/source hashes, and per-run elapsed time are in `codec-performance-offset-v1` (private receipt archive); `summary.csv` (private receipt archive) gives all medians and ranges. The exact reader-helper source snapshots, optimized/control module snapshots, matched Rust compiler identity, and helper rebuild commands are in the run directory's `README.md` (private receipt archive).

| Production reader measurement | Build-5 control | Optimized codec | Change |
|---|---:|---:|---:|
| `read_all`, MiB/s (median; five-run range) | 837.6 (831.4–840.1) | 1206.9 (1131.2–1217.4) | +44.1% |

The whole-reader result combines several codec changes, so it does not isolate the benefit of context reuse, output-buffer handling, or faster CRC individually. The build-5 reader already used slicing-by-8 CRC. In the component rows, hardware IEEE CRC reached 1264.6 MiB/s for direct decode with a reused `DCtx`, versus 898.1 MiB/s with slicing-by-8; the table-CRC diagnostic mode reached 387.8 MiB/s. CRC-only throughput was 10586.6, 2393.9, and 533.7 MiB/s for hardware, slicing-by-8, and table CRC, respectively. Reusing a `CCtx` improved level-3/64 KiB compression throughput from 613.9 to 626.4 MiB/s (+2.0%); reusing a `DCtx` with the same table CRC improved decode throughput from 384.2 to 387.8 MiB/s (+0.9%). These are local component measurements on this corpus, not general throughput guarantees.

The direct component rows use preloaded bytes and live in the same helper binary, so control-versus-optimized repetition is a stability check for those rows; the `read_all` row exercises each respective codec source. The checked writer-offset change is not isolated by this reader benchmark. Full compiler build measurements remain necessary to assess net build-time cost, and no cold-cache or standalone pack/write speed claim is made here.
