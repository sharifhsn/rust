#!/usr/bin/env python3
"""macOS counterpart of census_benchmark.py: size and time `target/` configurations.

Uses the same pinned projects, fixtures and phases (clean, touch, example edits, library
edits) as the Linux harness, but runs rustup toolchains natively with Apple's linker and
without the Linux-only RSS sampling and linker wrappers.

Usage: mac_census.py --sources DIR --targets DIR --out DIR [--projects a,b] [--arms a,b]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from census_benchmark import PROJECTS, classify  # noqa: E402

NIGHTLY = "nightly-2026-09-27"
STABLE = "1.98.1"

# Stable arms run stable Cargo with the nightly rustc, like the Linux harness, so each arm
# differs from `default` only in what it names.
ARMS = {
    "default": {},
    "line-tables": {"env": {"CARGO_PROFILE_DEV_DEBUG": "line-tables-only"}},
    "deps-line-tables": {"cargo_args": ["--config", 'profile.dev.package."*".debug="line-tables-only"']},
    "noincr": {"env": {"CARGO_INCREMENTAL": "0"}},
    "packed": {"env": {"CARGO_PROFILE_DEV_SPLIT_DEBUGINFO": "packed"}},
    "nightly": {"nightly": True},
    "nightly-line-tables": {"nightly": True, "env": {"CARGO_PROFILE_DEV_DEBUG": "line-tables-only"}},
    "nightly-line-tables-noincr": {"nightly": True, "env": {"CARGO_PROFILE_DEV_DEBUG": "line-tables-only",
                                                            "CARGO_INCREMENTAL": "0"}},
    "nightly-noincr": {"nightly": True, "env": {"CARGO_INCREMENTAL": "0"}},
}


def mac_classify(rel: str, path: Path, binary: str) -> str:
    parts = rel.split("/")
    if any(p.endswith(".dSYM") for p in parts):
        return "dsym"
    category = classify(rel, path, binary)
    if category == "other" and parts[-1].endswith(".o"):
        return "loose_object"
    if category == "other" and parts[-1].endswith(".dylib"):
        return "proc_macro_or_dylib"
    return category


def census(root: Path, binary: str) -> dict:
    inodes = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        rel = path.relative_to(root).as_posix()
        inodes.setdefault((stat.st_dev, stat.st_ino), [stat.st_blocks * 512, mac_classify(rel, path, binary)])
    by_category = {}
    for size, category in inodes.values():
        by_category[category] = by_category.get(category, 0) + size
    return {"allocated_unique_inode_bytes": sum(s for s, _ in inodes.values()),
            "unique_inodes": len(inodes), "allocated_by_category": dict(sorted(by_category.items()))}


def toolchain_path(toolchain: str, tool: str) -> str:
    return subprocess.run(["rustup", "which", "--toolchain", toolchain, tool],
                          check=True, capture_output=True, text=True).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--projects", default="nushell,polars,bevy")
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--keep-targets", action="store_true")
    args = parser.parse_args()

    rustc = toolchain_path(NIGHTLY, "rustc")
    cargos = {False: toolchain_path(STABLE, "cargo"), True: toolchain_path(NIGHTLY, "cargo")}
    args.out.mkdir(parents=True, exist_ok=True)
    results_path = args.out / "results.json"
    record = json.loads(results_path.read_text()) if results_path.exists() else {"projects": {}}
    record.update(rustc=subprocess.run([rustc, "-vV"], capture_output=True, text=True).stdout,
                  host=os.uname().machine, cpus=os.cpu_count())

    for project in args.projects.split(","):
        config = PROJECTS[project]
        source = args.sources / project
        fixture = source / config["edit_path"]
        library = source / config["lib_edit_path"]
        original, library_original = fixture.read_text(), library.read_text()
        project_record = record["projects"].setdefault(project, {"arms": {}})
        for arm in args.arms.split(","):
            spec = ARMS[arm]
            target = args.targets / project / arm
            shutil.rmtree(target, ignore_errors=True)
            # Pin Cargo's macOS default explicitly: a user config may override split-debuginfo.
            env = {**os.environ, "CARGO_TARGET_DIR": str(target), "RUSTC": rustc,
                   "CARGO_INCREMENTAL": "1", "CARGO_PROFILE_DEV_DEBUG": "2",
                   "CARGO_PROFILE_DEV_SPLIT_DEBUGINFO": "unpacked", **spec.get("env", {})}
            env.pop("RUSTFLAGS", None)
            env.pop("CARGO_BUILD_RUSTFLAGS", None)
            nightly = spec.get("nightly", False)
            # Nightly Cargo already passes `-Zembed-metadata=no` to rustc by default.
            build = [cargos[nightly], "build", "--locked",
                     *spec.get("cargo_args", []), *config["cargo_args"]]
            arm_record = {"command": build, "env": spec.get("env", {}), "phases": []}
            project_record["arms"][arm] = arm_record
            phases = ([("clean", None, None), ("touch", 0, None), ("edit1", 1, None), ("edit2", 2, None),
                       ("libedit1", None, 1), ("libedit2", None, 2)])
            try:
                for phase, step, lib_step in phases:
                    library_text = (library_original if lib_step is None else
                                    library_original + "\n#[doc(hidden)]\npub fn "
                                    f"compression_bench_edit_{lib_step}() -> usize {{ {lib_step} }}\n")
                    if library.read_text() != library_text:
                        library.write_text(library_text)
                    text = original
                    if step:
                        text = original.replace(
                            config["main_marker"],
                            f'{config["main_marker"]}\n    println!("compression-bench-edit:{step}");', 1)
                    fixture.write_text(text)
                    if step == 0:
                        os.utime(fixture)
                    log = args.out / project / arm / f"{phase}.log"
                    log.parent.mkdir(parents=True, exist_ok=True)
                    start = time.monotonic()
                    with log.open("w") as out:
                        code = subprocess.run(build, cwd=source, env=env, stdout=out, stderr=subprocess.STDOUT).returncode
                    wall = time.monotonic() - start
                    if code:
                        raise RuntimeError(f"{project}/{arm}/{phase} failed; see {log}")
                    runtime = subprocess.run([str(target / config["binary"]), *config["runtime_args"]],
                                             cwd=source, text=True, capture_output=True, timeout=120)
                    expected = (f"compression-bench-edit:{step}\n" if step else "") + config["expected_stdout"]
                    if runtime.returncode or runtime.stdout.strip() != expected:
                        raise RuntimeError(f"{project}/{arm}/{phase} runtime mismatch: {runtime.stdout!r}")
                    entry = {"phase": phase, "wall_seconds": wall}
                    if phase in ("clean", phases[-1][0]):
                        entry["census"] = census(target, config["binary"])
                    arm_record["phases"].append(entry)
                    print(f"{project} {arm} {phase} {wall:.1f}s", flush=True)
            finally:
                fixture.write_text(original)
                library.write_text(library_original)
                results_path.write_text(json.dumps(record, indent=2) + "\n")
                if not args.keep_targets:
                    shutil.rmtree(target, ignore_errors=True)
            size = arm_record["phases"][-1]["census"]["allocated_unique_inode_bytes"] / 2**30
            print(f"== {project} {arm}: {size:.2f} GiB", flush=True)


if __name__ == "__main__":
    main()
