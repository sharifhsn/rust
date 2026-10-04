# `compress-incremental`

This local experimental flag compresses persistent incremental compilation
records and cached code-generation work products:

```text
rustc -Cincremental=target/incremental -Zcompress-incremental \
    -Zartifact-compression-profile=fast ...
```

It requires incremental compilation to have been enabled separately. The flag is
independent of `-Zcompress-artifacts`, so either or both can be measured. It uses
the profiles and overrides documented under `compress-artifacts`.

The dependency graph, query cache, work-product index, saved codegen outputs,
metadata cache, pre-LTO bitcode, and ThinLTO keys retain their normal filenames.
Small or incompressible files may stay raw. Native tools receive decoded codegen
outputs. Existing session locks, hashes, and codegen reuse decisions are retained.

Query and graph readers currently decode packed files into contiguous memory.
This can increase memory use compared with mapping ordinary cache files. It does
not implement lazy query-record decompression. The prototype uses system libzstd
and is currently tested on native Apple Silicon. See the local benchmark report
under `tools/compressed-artifacts` for measured tradeoffs and validation limits.
