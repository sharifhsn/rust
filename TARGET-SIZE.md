# September 28 measured compact compiler snapshot

This branch contains the compiler source and measurement tools from local report commit `ccac5086ce6`.
The publication excludes unrelated Cranelift and rust-analyzer changes.
The associated Wild snapshot is [codex/compact-artifacts-2026-09-28](https://github.com/sharifhsn/wild/tree/codex/compact-artifacts-2026-09-28).

Read the [public results and limitations](https://github.com/sharifhsn/rust/blob/codex/target-size-research/TARGET-SIZE.md).
The [build guide](tools/compressed-artifacts/COMPACT-WILD.md) gives the compiler/linker procedure.
The compiler expects the Wild checkout beside the Rust checkout.

```sh
git clone --single-branch --branch codex/compact-artifacts-2026-09-28 https://github.com/sharifhsn/rust.git rust
git clone --single-branch --branch codex/compact-artifacts-2026-09-28 https://github.com/sharifhsn/wild.git wild
```

This is an LLM-assisted prototype and documentation in a personal fork.
It does not constitute an upstream proposal or accepted artifact format.
