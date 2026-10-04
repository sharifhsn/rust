# `compress-artifacts`

This local experimental flag stores standalone crate metadata and Rust `.rlib`
archives in a versioned, chunk-compressed container. It is a research prototype,
not an upstream supported artifact format.

```text
rustc -Zcompress-artifacts ...
```

The producer finishes and validates compression before announcing an artifact to
Cargo. Readers in this compiler accept ordinary and compressed artifacts.
Metadata is currently decoded eagerly into owned memory to retain the existing
contiguous-slice decoder interface. System linkers receive conventional archives
materialized in a temporary directory owned by the link operation. Runnable
binaries, dynamic libraries, and final static libraries retain their usual format.

The flag is tracked in compiler configuration hashes. Use a separate Cargo target
directory when comparing enabled and disabled builds. Ordinary tools that expect
an `ar` archive cannot read a compressed `.rlib` directly; decode it before export.
Metadata separation via Cargo's corresponding experimental option is independent
of this flag and remains part of the minimum-storage baseline.

The initial implementation requires a system libzstd at compiler link time.
The current build configuration and measurements cover `aarch64-apple-darwin`.
Cross-platform support, lazy compiler metadata access, direct compressed linker
reads, and cleanup after forced process termination require further work. Normal
temporary linker inputs are removed after the link unless `-Csave-temps` is set.

For the implementation, validation commands, and benchmark results, see
`tools/compressed-artifacts/README.md` in the checkout.

## Compression profiles

`-Zartifact-compression-profile=fast|balanced|small|legacy` selects a storage
tradeoff. The setting is shared with `-Zcompress-incremental`; it does not enable
either kind of compression by itself. `balanced` is the default when compression
is enabled. `legacy` retains the original level-3, 64-KiB geometry for comparisons.

| Profile | Zstandard level | Chunk size |
|---|---:|---:|
| `fast` | -1 | 256 KiB |
| `balanced` | 3 | 256 KiB |
| `small` | 9 | 1 MiB |
| `legacy` | 3 | 64 KiB |

Two optional overrides apply after the profile:

- `-Zartifact-compression-level=N`: Zstandard level from -5 through 19.
- `-Zartifact-compression-chunk-size=N`: independent chunk size in bytes, a power
  of two from 16384 through 4194304.

Lower levels usually spend less compression CPU and retain more bytes. Larger
chunks allow more matches, but increase scratch memory and the minimum amount
that a range reader must decode. The actual tradeoffs depend on artifact content.
Readers use the stored chunk size, independently of their command-line settings.
These options are tracked, so changing them invalidates the incremental session.

The codec reuses contexts and scratch buffers within a file, decodes whole files
directly into their result buffer, and uses AArch64 IEEE CRC instructions when
available. Other architectures retain a portable checksum implementation. The
format and checksums remain compatible with the original 64-KiB prototype.
