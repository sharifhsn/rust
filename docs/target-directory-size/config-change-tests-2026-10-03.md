# Configuration-change results: Bevy, Polars, and Nushell

The three configuration-change comparisons completed on October 3, 2026.
Cargo retention reduced target allocation by 62.4% to 65.5% in these histories.
The experiment kept full debug information and incremental compilation enabled.

## Target allocation

| Project | Configurations | Ordinary GiB | Active GiB | Saved |
|---|---:|---:|---:|---:|
| Bevy | 6 | 100.48 | 34.62 | 65.5% |
| Polars | 3 | 26.63 | 9.83 | 63.1% |
| Nushell | 3 | 30.13 | 11.32 | 62.4% |

These are new results from one continuous run on an on-demand m5.2xlarge.
Both arms used the same source edits, configurations, compiler, Cargo binary, and CPU.
Bevy reached the first complete configuration boundary above 100 GiB.
Polars and Nushell used the default, portable-v2, and frame-pointer configurations.

The table uses deduplicated allocation from the final file censuses.
The original command rows remain unchanged.
Later censuses added 12 KiB and 4 KiB for Polars ordinary and active allocation.
They added 8 KiB and 4 KiB for Nushell.
Bevy had no census difference.
These differences do not change the rounded results.

Retention started with empty targets.
These results do not establish cleanup of arbitrary old Cargo targets.
The earlier Bevy result and the separate compiler/Wild experiment remain separate evidence.

## Time and configuration returns

| Project | Build timers, ordinary → active | First return, ordinary → active |
|---|---:|---:|
| Bevy | 42.38 → 42.31 min | 31.97 → 263.32 s |
| Polars | 6.79 → 6.78 min | 24.61 → 157.31 s |
| Nushell | 5.91 → 5.92 min | 22.71 → 172.81 s |

Build timers measure builds after the Cargo checks.
They do not measure isolated clean builds from empty targets.
The build totals differ by less than five seconds per project in this single ordered run.
This result does not establish a statistical speed difference.

A return to an evicted configuration rebuilt its graph.
Ordinary retention reused 460/461 Bevy artifacts, 366/367 Polars artifacts, and 805/806 Nushell artifacts.
Active retention reused zero artifacts on those first returns.
The next build reused all artifacts in both arms for all projects.
This is the primary storage-versus-time tradeoff.

A bounded guest observation overlapped Bevy's first ordinary check.
The comparison excludes that check and its active counterpart from history timer totals.
The selected Bevy build timers include the driver's periodic disk observations.
S3 progress reads did not operate on the test host.

## Warm command distributions

Each operation has five measured samples per arm after two preparation cycles.
The driver interleaved ordinary and active samples.
All compiler artifacts in these samples were fresh.
Here, fresh means that Cargo reused the artifact without compilation.

| Project | Operation | Ordinary median, s | Active median, s |
|---|---|---:|---:|
| Bevy | check | 0.615 | 0.665 |
| Bevy | build | 0.565 | 0.565 |
| Bevy | test | 0.565 | 0.565 |
| Bevy | clippy | 0.665 | 0.715 |
| Polars | check | 0.417 | 0.465 |
| Polars | build | 0.416 | 0.417 |
| Polars | test | 0.417 | 0.417 |
| Polars | clippy | 0.465 | 0.515 |
| Nushell | check | 0.665 | 0.715 |
| Nushell | build | 0.665 | 0.715 |
| Nushell | test | 0.615 | 0.665 |
| Nushell | clippy | 0.715 | 0.766 |

Measured median differences range from approximately zero to 50 milliseconds.
The timed process wait and monitor add overhead to these short commands.
The samples show approximately 50-millisecond steps.
These results do not establish a precise sub-50-millisecond cost.
The [public metrics](public-results/retention-metrics.json) retains all measured warm samples.

## Correctness and scope

The driver completed 326 commands and 45 exact runtime oracles.
Of those commands, 321 returned zero.
Three deliberate compile failures preserved the successful session roots.
Their recovery builds and runtime oracles succeeded.

Bevy's pinned upstream suite reproduced the same backtrace-format failure in both arms.
Both arms then passed 890 tests with that one test excluded and two tests ignored.
Nushell passed 279 nu-protocol tests in each arm.
The selected Polars library suite contained zero executable tests.
Its lazy/CSV/Parquet runtime probe produced the exact expected result in all oracles.

The build graph covers the selected applications and their transitive dependencies.
The experiment does not cover every package, example, or feature combination in each repository.
It does not add full-workspace concurrency coverage or native Windows process-lifetime coverage.

The driver sampled disk allocation during Bevy's default builds at five-second intervals.
It did not capture a complete peak during replacement of an evicted graph.
Temporary replacement headroom remains a separate capacity requirement.


## Source identities

The [pinned driver](../../tools/artifact-retention/large.py) records the workload revisions and exact runtime oracles.
The [public metrics](public-results/retention-metrics.json) record sizes, original allocation differences, warm samples, and command outcomes.

The complete raw archives passed SHA-256 readback and restored-tree checks.
They remain private. The public metrics are excerpts from those receipts.
