# Retention measurement tools

The [public summary](../../TARGET-SIZE.md) gives results and limitations.
`reviewer-repro.py` is the small offline reproduction.
`large.py` contains pinned Bevy, Polars, and Nushell workloads and the paired configuration driver.
`astral-retention.py` applies the same driver to pinned Ruff and uv sources.
`profile.py` measures deduplicated file allocation, and `checkpoint.py` is an optional receipt publisher.
The large drivers require `psutil` and a prebuilt patched Cargo binary.

Leave `BENCH_BUCKET` unset for local reproduction without AWS uploads.
Large drivers download their pinned public source archives during preparation.
Use a separate host and sufficient temporary disk space for the large runs.

The Ruff library-test adapter expects the built Cargo at `cargo-src/target/release/cargo` beside the scripts.
It uses `setpriv` to preserve upstream permission-fixture behavior if the process is root.

```sh
python3 large.py --cargo /absolute/path/to/patched/cargo   --toolchain /absolute/path/to/toolchain/bin --sources /absolute/path/to/sources   --out /absolute/path/to/new-results --projects bevy polars nushell --jobs 8
python3 astral-retention.py --cargo /absolute/path/to/patched/cargo   --toolchain /absolute/path/to/toolchain/bin --sources /absolute/path/to/sources   --out /absolute/path/to/new-results --jobs 16
```

Keep failed and interrupted receipt paths.
The driver refuses an unsafe resume of an incomplete cold target.
This publication does not start another measurement run.
