#!/usr/bin/env python3
"""Benchmark pinned upstream workspaces with the existing compression measurement helpers.

Upstream checkouts and generated Cargo.lock files must be prepared before timing.
This runner copies sources and changes only a controlled leaf source. Optional
cleanup removes its own completed target/tmp trees after saving file headers and
censuses. It never modifies the supplied source checkout.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location("compression_benchmark", HERE / "benchmark.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

WORKLOADS = {
    "bevy": {
        "version": "0.19.1",
        "revision": "b56fc29d3016e641754765244b5ba3f9cc504671",
        "cargo_args": ["-p", "bevy", "--example", "compression_probe"],
        "binary": "debug/examples/compression_probe",
        "edit_path": "examples/compression_probe.rs",
        "fixture": "bevy.rs",
        "main_marker": "fn main() {",
        "runtime_args": [],
        "expected_stdout": "bevy: entities=1000 updates=30 sum=559500",
        "features": "upstream default: 2d, 3d, ui, audio",
    },
    "nushell": {
        "version": "0.116.0",
        "revision": "2459fdd134ea4fdbae42efd6924e2b41201cf363",
        "cargo_args": ["-p", "nu", "--bin", "nu"],
        "binary": "debug/nu",
        "edit_path": "src/main.rs",
        "main_marker": "fn main() -> Result<()> {",
        "runtime_args": ["--no-config-file", "--no-history", "-c", "[1 2 3 4] | each {|x| $x * $x } | math sum"],
        "expected_stdout": "30",
        "features": "upstream default features",
    },
    "polars": {
        "version": "0.55.2",
        "revision": "d7488c71ecfbc77790292ff5b365b991c08380ce",
        "cargo_args": ["-p", "polars", "--example", "compression_probe", "--features", "lazy,parquet"],
        "binary": "debug/examples/compression_probe",
        "edit_path": "crates/polars/examples/compression_probe.rs",
        "fixture": "polars.rs",
        "main_marker": "fn main() -> PolarsResult<()> {",
        "runtime_args": [],
        "expected_stdout": "polars: csv+lazy+parquet groups=2 sum=10",
        "features": "upstream defaults plus lazy and parquet",
    },
}


def fingerprint(directory: Path) -> dict:
    """Hash public source inputs while excluding Git internals and build output."""
    files = []
    for current, dirs, names in os.walk(directory, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in {".git", "target", "__pycache__"})
        for name in sorted(names):
            path = Path(current) / name
            relative = str(path.relative_to(directory))
            if path.is_symlink():
                files.append({"path": relative, "symlink": os.readlink(path)})
            elif path.is_file():
                files.append({"path": relative, "bytes": path.stat().st_size, "sha256": bench.sha256_file(path)})
    files.sort(key=lambda row: row["path"])
    return {"sha256": bench.sha256_bytes(json.dumps(files, sort_keys=True).encode()), "files": files}


def check_runtime(binary: Path, config: dict, step: int, workspace: Path, env: dict, directory: Path) -> dict:
    result = bench.run_plain([str(binary), *config["runtime_args"]], cwd=workspace,
                             env=env, log_dir=directory, label="runtime-oracle", timeout=60)
    expected = (f"compression-bench-edit:{step}\n" if step else "") + config["expected_stdout"]
    actual = Path(result["stdout_log"]).read_text(errors="replace").strip()
    result.update(binary=str(binary), binary_sha256=bench.sha256_file(binary),
                  expected_stdout=expected, actual_stdout=actual, stdout_matches=actual == expected)
    if result["exit_code"] or actual != expected:
        raise RuntimeError(f"runtime failed: {directory}; expected {expected!r}, got {actual!r}")
    return result


def noop_inventory(target: Path, temp_dir: Path, binary: Path) -> dict:
    """Track Cargo's output aliases and dep-info rewrites without ignoring file changes."""
    result = {}
    for entry in bench.census([target, temp_dir], with_files=True)["files"]:
        if entry["kind"] != "file":
            continue
        path = Path(entry["path"])
        entry["mtime_ns"] = path.lstat().st_mtime_ns
        # Cargo can replace output aliases even when their producing unit is Fresh.
        if path.suffix == ".d" or path.name == "build-script-build" or path == binary:
            entry["sha256"] = bench.sha256_file(path)
        result[str(path)] = entry
    return result


def validate_noop(before: dict, after: dict) -> dict:
    assert before.keys() == after.keys(), "no-op changed the set of output paths"
    changes = []
    for path, previous in before.items():
        current = after[path]
        if previous == current:
            continue
        assert previous.get("sha256") is not None, f"unexpected no-op file change: {path}"
        assert previous["sha256"] == current.get("sha256"), f"no-op changed output contents: {path}"
        assert previous["logical_bytes"] == current["logical_bytes"]
        changes.append(dict(path=path, before=previous, after=current))
    return dict(output_paths_unchanged=True, rewritten_outputs_content_identical=True,
                other_file_metadata_unchanged=True, changes=changes,
                note="Cargo may replace build-script/binary aliases and rewrite identical .d content on a Fresh build. Their allocated blocks can change; all such paths are hashed before/after.")


def run_one(args, name, arm, repetition, base_env, source_record):
    config = WORKLOADS[name]
    run_dir = args.out / name / "dev" / f"rep-{repetition:02d}" / arm
    run_dir.mkdir(parents=True, exist_ok=False)
    source = args.sources / name
    workspace = args.workspaces / args.out.name / name / f"rep-{repetition:02d}" / arm
    workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, workspace, symlinks=True, ignore=shutil.ignore_patterns(".git", "target"))
    main = workspace / config["edit_path"]
    if fixture := config.get("fixture"):
        main.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(HERE / "large-fixtures" / fixture, main)
    initial = fingerprint(workspace)
    (run_dir / "workspace-inputs.json").write_text(json.dumps(initial, indent=2) + "\n")
    original_main = main.read_text()
    assert original_main.count(config["main_marker"]) == 1
    target, temp_dir = run_dir / "target", run_dir / "tmp"
    temp_dir.mkdir()
    env = base_env.copy()
    env.update(RUSTC=str(args.rustc), RUSTC_BOOTSTRAP="1", CARGO_TARGET_DIR=str(target),
               CARGO_INCREMENTAL="1", CARGO_PROFILE_DEV_DEBUG="0", CARGO_PROFILE_DEV_INCREMENTAL="true",
               TMPDIR=str(temp_dir), TMP=str(temp_dir), TEMP=str(temp_dir),
               CARGO_ENCODED_RUSTFLAGS="\x1f".join(bench.arm_flags(arm)))
    before = bench.census([target, temp_dir])
    record = dict(workload=name, package=config["cargo_args"][1], profile="dev", arm=arm,
                  repetition=repetition, incremental=True, semantic_edits=2,
                  workspace=str(workspace), target_dir=str(target), temp_dir=str(temp_dir),
                  started_utc=bench.utc_now(), source=source_record, phases=[],
                  input_fingerprint=initial["sha256"], start_disk_census=before,
                  environment_overrides={k: env[k] for k in ["RUSTC", "CARGO_INCREMENTAL", "CARGO_PROFILE_DEV_DEBUG", "CARGO_ENCODED_RUSTFLAGS"]})
    for phase, step in [("clean_build", 0), ("noop", 0), ("semantic_edit_1", 1), ("semantic_edit_2", 2)]:
        noop_before = noop_inventory(target, temp_dir, target / config["binary"]) if phase == "noop" else None
        if noop_before is not None:
            (run_dir / "noop-files-before.json").write_text(json.dumps(noop_before, indent=2) + "\n")
        if step:
            statement = f'\n    println!("compression-bench-edit:{{}}", std::hint::black_box({step}u64));'
            main.write_text(original_main.replace(config["main_marker"], config["main_marker"] + statement, 1))
        command = [str(args.cargo), "-Zno-embed-metadata", "build", "--offline", "--locked", "--jobs", str(args.jobs), "--verbose", *config["cargo_args"]]
        directory = run_dir / "commands" / phase / "build"
        print(f"starting {name}/rep-{repetition:02d}/{arm}/{phase}", flush=True)
        result = bench.run_measured(command, cwd=workspace, env=env, target=target, temp_dir=temp_dir,
                                    log_dir=directory, label=phase, interval=args.sample_interval, timeout=None)
        invocations_file = directory / "rustc-invocations.json"
        invocations = json.loads(invocations_file.read_text()) if invocations_file.exists() else []
        audit = bench.audit_invocations(invocations, arm, separated=True, incremental=True, expected_rustc=str(args.rustc))
        result["rustc_audit"] = audit
        if result["exit_code"] or audit["audit_errors"]:
            (run_dir / "failed-command.json").write_text(json.dumps(result, indent=2) + "\n")
            raise RuntimeError(f"build/audit failed: {directory}; exit={result['exit_code']}, audit={audit['audit_errors']}")
        if phase == "noop":
            assert audit["count"] == 0, "no-op unexpectedly invoked rustc"
        else:
            assert any(i["incremental_codegen"] for i in audit["invocations"]), "missing incremental codegen"
        runtime = check_runtime(target / config["binary"], config, step, workspace, env, run_dir / "runtime" / phase)
        after = bench.census([target, temp_dir])
        monitor = result["monitor"]
        row = dict(name=phase, cargo_commands=[result], runtime_oracle=runtime, disk_before=before, disk_after=after,
                   cargo_wall_seconds_sum=result["wall_seconds"],
                   child_user_cpu_seconds_sum=result["child_user_cpu_seconds"],
                   child_system_cpu_seconds_sum=result["child_system_cpu_seconds"],
                   sampled_process_tree_rss_peak_bytes=monitor["sampled_process_tree_rss_sum_peak_bytes"],
                   sampled_target_tmp_allocated_peak_bytes=monitor["sampled_target_tmp_allocated_unique_inode_peak_bytes"],
                   sampled_target_tmp_logical_peak_bytes=monitor["sampled_target_tmp_logical_unique_inode_peak_bytes"],
                   disk_logical_growth_bytes=after["logical_unique_inode_bytes"]-before["logical_unique_inode_bytes"],
                   disk_allocated_growth_bytes=after["allocated_unique_inode_bytes"]-before["allocated_unique_inode_bytes"])
        if phase == "noop":
            assert row["disk_logical_growth_bytes"] == 0
            noop_after = noop_inventory(target, temp_dir, target / config["binary"])
            (run_dir / "noop-files-after.json").write_text(json.dumps(noop_after, indent=2) + "\n")
            row["noop_validation"] = validate_noop(noop_before, noop_after)
        record["phases"].append(row)
        (run_dir / f"{phase}.json").write_text(json.dumps(row, indent=2) + "\n")
        before = after
        print(f"completed {name}/{arm}/{phase}: {result['wall_seconds']:.3f}s, {after['allocated_unique_inode_bytes']/2**30:.3f} GiB", flush=True)
    census = bench.census([target, temp_dir], with_files=True)
    entries = census.pop("files")
    residue = []
    for entry in entries:
        path = Path(entry["path"])
        if ".artifact-compression-" in path.name or path.name.startswith("rustc-artifacts"):
            residue.append(str(path))
        if entry["kind"] == "file" and path.is_file():
            with path.open("rb") as stream:
                header = stream.read(64)
            entry["is_compressed"] = header[:8] == b"RUSTZRL1"
            if entry["is_compressed"]:
                assert len(header) == 64
                entry["packed_decoded_bytes"] = struct.unpack_from("<Q", header, 24)[0]
    assert not residue, residue
    with (run_dir / "final-file-census.jsonl").open("w") as stream:
        for entry in entries:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")
    record["final_target_tmp_census"] = census
    # Restore the deliberate leaf edits before validating all copied source bytes.
    main.write_text(original_main)
    record["workspace_sources_unchanged_except_controlled_edits"] = fingerprint(workspace) == initial
    record["input_unchanged"] = fingerprint(source)["sha256"] == source_record["fingerprint"]["sha256"]
    record["dependency_repositories_unchanged"] = record["input_unchanged"]
    assert record["workspace_sources_unchanged_except_controlled_edits"] and record["input_unchanged"]
    record["finished_utc"] = bench.utc_now()
    (run_dir / "run.json").write_text(json.dumps(record, indent=2) + "\n")
    if args.cleanup_targets:
        for path in [target, temp_dir]:
            assert path.parent == run_dir and path.name in {"target", "tmp"} and not path.is_symlink()
            shutil.rmtree(path)
        record["cleanup"] = dict(removed=[str(target), str(temp_dir)],
                                  retained_allocated_file_bytes=census["allocated_unique_inode_bytes"],
                                  note="Owned scratch only; file headers/census saved before removal. Physical APFS free space not measured.")
        (run_dir / "run.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=Path("/tmp/rust-compression-large-20260926/sources"))
    parser.add_argument("--workspaces", type=Path, default=Path("/tmp/rust-compression-large-20260926/workspaces"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workloads", nargs="+", choices=WORKLOADS, required=True)
    parser.add_argument("--arms", nargs="+", choices=["dedup_off", "incremental_only", "both_fast", "both_balanced"], default=["dedup_off", "incremental_only", "both_fast", "both_balanced"])
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--sample-interval", type=float, default=0.25)
    parser.add_argument("--cleanup-targets", action="store_true", help="remove only each completed run's target/tmp after saving its file headers and census")
    parser.add_argument("--rustc", type=Path, default=Path("/tmp/rust-compressed-artifacts-bootstrap/aarch64-apple-darwin/stage1/bin/rustc"))
    parser.add_argument("--cargo", type=Path, default=Path("/tmp/rust-compressed-artifacts-bootstrap/aarch64-apple-darwin/stage0/bin/cargo"))
    args = parser.parse_args()
    if args.repetitions < 1 or args.jobs < 1 or args.sample_interval <= 0:
        parser.error("repetitions, jobs and sampling interval must be positive")
    if platform.machine() != "arm64" or bench.psutil is None:
        raise RuntimeError("Use native ARM Python with psutil on the recorded host")
    for field in ["out", "sources", "workspaces", "rustc", "cargo"]:
        setattr(args, field, getattr(args, field).resolve())
    build_record = bench.find_build_record(args.rustc)
    if not build_record:
        raise RuntimeError("compiler does not match a successful source-stable build")
    build = json.loads(Path(build_record).read_text())
    assert all(bench.sha256_file(ROOT / p) == h for p, h in build["source_sha256_after"].items())
    base_env, removed = bench.split_env()
    config = bench.cargo_config_facts(base_env, [args.sources / n for n in args.workloads])
    args.out.mkdir(parents=True, exist_ok=False)
    source_records = {}
    for name in args.workloads:
        source = args.sources / name
        revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        assert revision == WORKLOADS[name]["revision"]
        assert (source / "Cargo.lock").is_file()
        source_records[name] = dict(path=str(source), revision=revision, definition=WORKLOADS[name],
                                    cargo_lock_sha256=bench.sha256_file(source / "Cargo.lock"), fingerprint=fingerprint(source))
    manifest = dict(created_utc=bench.utc_now(), compiler_build_record=build_record,
                    rustc=bench.tool_identity(args.rustc), cargo=bench.tool_identity(args.cargo),
                    rustc_driver_libraries=build["driver_libraries"], sources=source_records,
                    harness_sha256=bench.sha256_file(Path(__file__)), helper_sha256=bench.sha256_file(HERE / "benchmark.py"),
                    fixture_sha256={p.name:bench.sha256_file(p) for p in (HERE / "large-fixtures").glob("*.rs")},
                    repetitions=args.repetitions, arms=args.arms, workloads=args.workloads, jobs=args.jobs,
                    sample_interval=args.sample_interval, profile="dev", debug=0, incremental=True,
                    removed_inherited_environment_names=removed, cargo_config_facts=config,
                    host=dict(platform=platform.platform(), machine=platform.machine(), python=sys.version),
                    notes=["All default upstream dev optimization settings retained; Rust debug info forced to zero.",
                           "Workspace path dependencies use incremental codegen; registry dependencies follow Cargo's normal policy.",
                           "Cargo downloads excluded; measured builds are offline and locked.",
                           "CARGO_ENCODED_RUSTFLAGS replaces repository target rustflags consistently in all arms.",
                           "No-op and two leaf semantic edits have exact stdout oracles; no claim of complete upstream test coverage.",
                           "RSS and storage peaks are sampled lower bounds; allocated st_blocks do not resolve APFS clone sharing."])
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    records = []
    for wi, name in enumerate(args.workloads):
        for repetition in range(1, args.repetitions + 1):
            offset = (wi + repetition - 1) % len(args.arms)
            for arm in args.arms[offset:] + args.arms[:offset]:
                record = run_one(args, name, arm, repetition, base_env, source_records[name])
                records.append(record)
                (args.out / "results.json").write_text(json.dumps(dict(manifest=str(args.out / "manifest.json"), completed_runs=len(records), runs=records), indent=2) + "\n")
    print(json.dumps(dict(completed_runs=len(records), out=str(args.out))), flush=True)


if __name__ == "__main__":
    main()
