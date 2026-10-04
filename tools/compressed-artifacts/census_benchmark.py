"""Debug-on, incremental-on target census and Wild link profile for large projects.

Runs inside the Linux benchmark container. For each project and arm it performs a
clean build, a touch-only rebuild, and several semantic edits of the project's
fixture, with `-Cdebuginfo=2`, `CARGO_INCREMENTAL=1`, compact artifacts linked
directly by Wild, compressed incremental state, and Zstd-compressed final DWARF.

Each phase records wall time, sampled process-tree RSS, a target census that splits
the incremental directory by file kind and attributes hard-linked inodes to the set
of categories that name them, and one record per Wild link (duration, `--time`
phases, and compact-object statistics, including objects whose payload was never
read). An optional phase builds one library's unit-test binary to widen the
member-selection sample beyond the main executable.

Both arms share one compiler and linker binary. `ordinary` writes ordinary artifacts
and incremental state; `compressed` uses compact objects, compressed artifacts, and
compressed incremental state. Example edits recompile only the tiny example crate;
library edits add a public item to a large crate, so it and its dependents recompile
against their incremental caches.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECTS = {
    "bevy": {
        "cargo_args": ["-p", "bevy", "--example", "compression_probe"],
        "binary": "debug/examples/compression_probe",
        "edit_path": "examples/compression_probe.rs",
        "main_marker": "fn main() {",
        "runtime_args": [],
        "expected_stdout": "bevy: entities=1000 updates=30 sum=559500",
        "gdb": ["info line compression_probe::main", "ptype bevy_ecs::world::World"],
        "lib_edit_path": "crates/bevy_pbr/src/lib.rs",
        "test_args": ["-p", "bevy_ecs", "--lib"],
    },
    "polars": {
        "cargo_args": ["-p", "polars", "--example", "compression_probe", "--features", "lazy,parquet"],
        "binary": "debug/examples/compression_probe",
        "edit_path": "crates/polars/examples/compression_probe.rs",
        "main_marker": "fn main() -> PolarsResult<()> {",
        "runtime_args": [],
        "expected_stdout": "polars: csv+lazy+parquet groups=2 sum=10",
        "gdb": ["info line compression_probe::main", "ptype polars_core::frame::DataFrame"],
        "lib_edit_path": "crates/polars-lazy/src/lib.rs",
        "test_args": ["-p", "polars", "--lib", "--features", "lazy,parquet"],
    },
    "nushell": {
        "cargo_args": ["-p", "nu", "--bin", "nu"],
        "binary": "debug/nu",
        "edit_path": "src/main.rs",
        "main_marker": "fn main() -> Result<()> {",
        "runtime_args": ["--no-config-file", "--no-history", "-c",
                         "[1 2 3 4] | each {|x| $x * $x } | math sum"],
        "expected_stdout": "30",
        "gdb": ["info line nu::main", "ptype nu_protocol::value::Value"],
        "lib_edit_path": "crates/nu-command/src/lib.rs",
        "test_args": ["-p", "nu-protocol", "--lib"],
    },
}

# `ordinary` is the control: same compiler, Wild, and Zstd DWARF, no artifact changes.
# Default arms. `--arms-file` replaces them with a JSON list of objects:
#   {"name": str, "rustc": path (optional), "flags": [str], "env": {str: str | null},
#    "store": bool, "linker": "wild" | "lld", "cargo": path (optional),
#    "stock_cargo": bool, "release": bool, "cargo_args": [str]}
# `{store}` in a flag expands to the arm's object-store directory, which `store` creates.
# A `null` env value removes the variable. `stock_cargo` drops the harness's nightly
# additions (`-Zno-embed-metadata` and the base rustflags) so the arm matches what a stable
# Cargo passes by default. `release` builds the release profile without incremental.
COMPRESSED_FLAGS = [
    "-Zcompress-incremental=yes", "-Zcompress-artifacts=yes",
    "-Zartifact-compression-profile=balanced", "-Zcompact-artifact-store={store}",
    # One copy of DWARF: compressed `.dwo` files beside the objects.
    "-Csplit-debuginfo=unpacked", "-Zdebuginfo-compression=zstd",
    "-Cllvm-args=-dwarf-linkage-names=Abstract",
]
FINAL_DWARF_ZSTD = ["-Clink-arg=-Wl,--compress-debug-sections=zstd"]
DEFAULT_ARMS = [
    {"name": "ordinary", "flags": FINAL_DWARF_ZSTD, "store": False, "linker": "wild"},
    {"name": "compressed", "flags": COMPRESSED_FLAGS + FINAL_DWARF_ZSTD, "store": True,
     "linker": "wild"},
]
# `--ld-path` makes clang run the recording wrapper; `-fuse-ld=lld` would find the real
# `ld.lld` next to clang first.
LINKER_FLAGS = {
    "wild": ["-Clinker=clang", "-Clink-arg=--ld-path={wrappers}/ld.wild"],
    "lld": ["-Clinker=clang", "-Clink-arg=--ld-path={wrappers}/ld.lld"],
}

WRAPPER = """#!/bin/bash
# Records one JSON line per link, keeping the linker's stderr for the benchmark.
out=""; prev=""
for arg in "$@"; do [ "$prev" = "-o" ] && out="$arg"; prev="$arg"; done
id="$(date +%s%N)-$$"
err="${WILD_LINK_LOG_DIR:-/tmp}/$id.stderr"
start=$(date +%s.%N)
# `--time` reports on stdout, which the compiler driver does not need.
WILD_COMPACT_STATS=1 "__LINKER__" __EXTRA__ "$@" 2>"$err" >"${err%.stderr}.stdout"
code=$?
end=$(date +%s.%N)
cat "$err" >&2
if [ -n "$WILD_LINK_LOG_DIR" ]; then
  printf '{"id":"%s","output":"%s","exit":%d,"start":%s,"end":%s}\\n' \\
    "$id" "$out" "$code" "$start" "$end" >> "$WILD_LINK_LOG_DIR/links.jsonl"
else
  rm -f "$err" "${err%.stderr}.stdout"
fi
exit $code
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def classify(rel: str, path: Path, binary: str) -> str:
    parts = rel.split("/")
    name = parts[-1]
    if name.endswith(".dwo"):
        return "split_dwarf"
    if "incremental" in parts[:-1]:
        if name.endswith(".o"):
            return "incremental/work_product_object"
        if name.startswith("dep-graph-page-"):
            return "incremental/dep_graph_pages"
        if name.startswith("dep-graph-index-"):
            return "incremental/dep_graph_reverse_index"
        if name == "dep-graph.bin":
            return "incremental/dep_graph_manifest"
        if name == "query-cache.bin":
            return "incremental/query_cache"
        if name == "work-products.bin":
            return "incremental/work_product_list"
        if name.endswith(".rmeta"):
            return "incremental/metadata"
        return "incremental/other"
    if parts[0] == "compact-store":
        return "compact_object_store"
    if rel == binary:
        return "final_binary"
    if name.endswith(".rlib"):
        return "rlib_manifest"
    if name.endswith(".rmeta"):
        return "rmeta"
    if name.endswith(".so"):
        return "proc_macro_or_dylib"
    if "build" in parts[:-1]:
        if name.startswith("build-script-"):
            return "build_script_executable"
        if "out" in parts:
            return "build_script_output"
        return "build_other"
    if name.endswith(".d") or ".fingerprint" in parts:
        return "cargo_bookkeeping"
    if os.access(path, os.X_OK) and "." not in name:
        return "executable"
    return "other"


def is_final_link(output: str, binary: str) -> bool:
    """Cargo links `<name>-<hash>` and hard-links it to the unhashed binary path."""
    name = Path(output).name
    stem = Path(binary).name
    return name == stem or (name.startswith(stem + "-") and "." not in name)


def target_census(root: Path, binary: str) -> dict:
    inodes: dict[tuple[int, int], dict] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        stat = path.stat()
        rel = path.relative_to(root).as_posix()
        entry = inodes.setdefault((stat.st_dev, stat.st_ino),
                                  {"bytes": stat.st_blocks * 512, "categories": set()})
        entry["categories"].add(classify(rel, path, binary))
    by_category: dict[str, int] = {}
    by_link_set: dict[str, int] = {}
    for entry in inodes.values():
        categories = sorted(entry["categories"])
        key = "+".join(categories)
        by_link_set[key] = by_link_set.get(key, 0) + entry["bytes"]
        # Attribute a shared inode to its first category in this priority order.
        owner = next((c for c in ("compact_object_store", "final_binary", "executable")
                      if c in categories), categories[0])
        by_category[owner] = by_category.get(owner, 0) + entry["bytes"]
    return {
        "allocated_unique_inode_bytes": sum(e["bytes"] for e in inodes.values()),
        "unique_inodes": len(inodes),
        "allocated_by_category": dict(sorted(by_category.items())),
        "allocated_by_link_set": dict(sorted(by_link_set.items())),
    }


def sampled_rss_tree(root_pid: int) -> tuple[int, dict[str, int]]:
    processes = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text()
            parent = int(re.search(r"^PPid:\s+(\d+)$", status, re.M).group(1))
            rss = int(re.search(r"^VmRSS:\s+(\d+)\s+kB$", status, re.M).group(1)) * 1024
            processes[int(entry.name)] = (parent, rss, (entry / "comm").read_text().strip())
        except (OSError, AttributeError, ValueError):
            continue
    members = {root_pid}
    while True:
        newer = members | {pid for pid, (parent, _, _) in processes.items() if parent in members}
        if newer == members:
            break
        members = newer
    by_kind: dict[str, int] = {}
    for pid in members & processes.keys():
        _, rss, comm = processes[pid]
        kind = {"rustc": "rustc", "wild": "wild_linker", "ld.lld": "lld_linker",
                "cargo": "cargo"}.get(comm, "other")
        by_kind[kind] = by_kind.get(kind, 0) + rss
    return sum(by_kind.values()), by_kind


def parse_links(log_dir: Path) -> list[dict]:
    links_path = log_dir / "links.jsonl"
    if not links_path.exists():
        return []
    links = []
    for line in links_path.read_text().splitlines():
        link = json.loads(line)
        stderr_path = log_dir / f"{link['id']}.stderr"
        stderr = stderr_path.read_text(errors="replace") if stderr_path.exists() else ""
        stats = re.findall(r"WILD_COMPACT_STATS (\{[^\n]+\})", stderr)
        link["seconds"] = link["end"] - link["start"]
        link["compact_stats"] = json.loads(stats[-1]) if stats else None
        stdout_path = log_dir / f"{link['id']}.stdout"
        link["time_report"] = (stdout_path.read_text(errors="replace").splitlines()
                               if stdout_path.exists() else [])
        links.append(link)
    return links


def run_phase(command: list[str], cwd: Path, env: dict, log_dir: Path) -> dict:
    log_dir.mkdir(parents=True, exist_ok=True)
    env = {**env, "WILD_LINK_LOG_DIR": str(log_dir)}
    start = time.monotonic()
    with (log_dir / "stdout.log").open("w") as out, (log_dir / "stderr.log").open("w") as err:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=out, stderr=err, text=True)
        peak, peak_by_kind = 0, {}
        while process.poll() is None:
            total, by_kind = sampled_rss_tree(process.pid)
            peak = max(peak, total)
            for kind, rss in by_kind.items():
                peak_by_kind[kind] = max(peak_by_kind.get(kind, 0), rss)
            time.sleep(0.2)
        code = process.wait()
    wall = time.monotonic() - start
    stderr = (log_dir / "stderr.log").read_text(errors="replace")
    return {
        "command": command,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "exit_code": code,
        "wall_seconds": wall,
        "sampled_process_tree_rss_peak_bytes": peak,
        "sampled_process_tree_rss_peak_by_kind_bytes": peak_by_kind,
        "links": parse_links(log_dir),
        "stderr_tail": stderr[-3000:],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects", default="bevy,polars,nushell")
    parser.add_argument("--arms", default="ordinary,compressed")
    parser.add_argument("--edits", type=int, default=2, help="example edits")
    parser.add_argument("--lib-edits", type=int, default=2)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--tests-arm", default="compressed", help="arm that also builds a unit-test binary")
    parser.add_argument("--sources", type=Path, default=Path("/tmp/bench/sources"))
    parser.add_argument("--targets", type=Path, default=Path("/tmp/bench/targets"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rustc", type=Path,
                        default=Path("/tmp/rustc-linux-build/aarch64-unknown-linux-gnu/stage1/bin/rustc"))
    parser.add_argument("--cargo", type=Path,
                        default=Path("/tmp/rustc-linux-build/aarch64-unknown-linux-gnu/stage0/bin/cargo"))
    parser.add_argument("--wild", type=Path, default=Path("/tmp/wild-release/release/wild"))
    parser.add_argument("--lld", type=Path, default=Path("/usr/bin/ld.lld"))
    parser.add_argument("--arms-file", type=Path, help="JSON arm definitions (see DEFAULT_ARMS)")
    parser.add_argument("--target", help="cross-compilation target triple")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    wrapper_dir = args.targets / "linker-bin"
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    for name, linker, extra in (("ld.wild", args.wild, "--time"), ("ld.lld", args.lld, "")):
        if linker.exists():
            wrapper = wrapper_dir / name
            wrapper.write_text(WRAPPER.replace("__LINKER__", str(linker)).replace("__EXTRA__", extra))
            wrapper.chmod(0o755)
    arm_specs = {arm["name"]: arm for arm in
                 (json.loads(args.arms_file.read_text()) if args.arms_file else DEFAULT_ARMS)}
    if args.arms_file and args.arms == parser.get_default("arms"):
        args.arms = ",".join(arm_specs)

    toolchain_bin = "/usr/local/rustup/toolchains/1.98.1-aarch64-unknown-linux-gnu/bin"
    base_env = {k: v for k, v in os.environ.items() if k not in {
        "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER",
        "CARGO_TARGET_DIR", "CARGO_INCREMENTAL", "CARGO_PROFILE_DEV_DEBUG",
    }}
    base_env.update({
        "RUSTC": str(args.rustc), "RUSTC_BOOTSTRAP": "1", "CARGO_HOME": "/root/.cargo",
        "CARGO_INCREMENTAL": "1", "CARGO_PROFILE_DEV_DEBUG": "2",
        "PATH": f"{wrapper_dir}:{toolchain_bin}:{os.environ.get('PATH', '')}",
    })

    record = {
        "rustc_version": subprocess.check_output([str(args.rustc), "-vV"], text=True),
        "rustc_sha256": sha256(args.rustc),
        "wild_version": subprocess.check_output([str(args.wild), "--version"], text=True).strip(),
        "wild_sha256": sha256(args.wild),
        "host": subprocess.check_output(["uname", "-a"], text=True).strip(),
        "target": args.target,
        "profile": f"dev; CARGO_INCREMENTAL=1; debug=2 (build tools at Cargo default); jobs={args.jobs}",
        "arm_specs": arm_specs,
        "projects": {},
    }
    for index, project in enumerate(args.projects.split(",")):
        config = PROJECTS[project]
        source = args.sources / project
        subprocess.run([str(args.cargo), "fetch", "--locked"], cwd=source, env=base_env, check=True)
        fixture = source / config["edit_path"]
        original = fixture.read_text()
        library = source / config["lib_edit_path"]
        library_original = library.read_text()
        arms = args.arms.split(",")
        # Rotate arm order between projects so neither arm always runs first.
        arms = arms[index % len(arms):] + arms[:index % len(arms)]
        project_record = {
            "source_revision": (source / ".benchmark-revision").read_text().strip(),
            "arm_order": arms,
            "arms": {},
        }
        record["projects"][project] = project_record
        for arm in arms:
            target = args.targets / project / arm
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True)
            spec = arm_specs[arm]
            binary_rel = (f"{args.target}/" if args.target else "") + config["binary"]
            if spec.get("release"):
                binary_rel = binary_rel.replace("debug/", "release/", 1)
            stock = spec.get("stock_cargo", False)
            store = target / "compact-store"
            scratch = args.targets / "tmp" / project / arm
            scratch.mkdir(parents=True, exist_ok=True)
            flags = [
                # Debug info comes from CARGO_PROFILE_DEV_DEBUG=2, so build scripts and proc
                # macros keep Cargo's default of no debug info.
                # Not for release: a profile with `lto` builds dependencies as bitcode only,
                # and `-Clto=no` would then hand that bitcode to the linker.
                *([] if stock else ["-Zembed-metadata=no"] if spec.get("release") else
                  ["-Cembed-bitcode=no", "-Clto=no", "-Zembed-metadata=no"]),
                *(flag.replace("{wrappers}", str(wrapper_dir))
                  for flag in LINKER_FLAGS[spec.get("linker", "wild")]),
                *(flag.replace("{store}", str(store)) for flag in spec.get("flags", [])),
            ]
            if spec.get("store"):
                store.mkdir()
            rustc = Path(spec.get("rustc", args.rustc))
            env = {**base_env, **spec.get("env", {}), "RUSTC": str(rustc),
                   "CARGO_TARGET_DIR": str(target), "CARGO_ENCODED_RUSTFLAGS": "\x1f".join(flags),
                   "TMPDIR": str(scratch)}
            if spec.get("release"):
                env["CARGO_INCREMENTAL"] = None
            env = {k: v for k, v in env.items() if v is not None}
            if args.target:
                # Cross builds: the target linker needs its triple, and binaries run under
                # binfmt emulation.
                env[f"CARGO_TARGET_{args.target.upper().replace('-', '_')}_LINKER"] = "clang"
                # Build scripts that compile C need the cross compiler (Debian's triple
                # drops the vendor).
                gnu = args.target.replace("-unknown-", "-")
                for tool, name in (("CC", "gcc"), ("CXX", "g++"), ("AR", "ar")):
                    env[f"{tool}_{args.target.replace('-', '_')}"] = f"{gnu}-{name}"
                # Build scripts that probe system libraries (Bevy's Wayland and ALSA) use
                # the target's multiarch pkg-config files.
                env["PKG_CONFIG_ALLOW_CROSS"] = "1"
                env[f"PKG_CONFIG_PATH_{args.target.replace('-', '_')}"] = (
                    f"/usr/lib/{gnu}/pkgconfig:/usr/share/pkgconfig")
                flags += [f"-Clink-arg=--target={args.target}"]
                env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)
            target_args = ["--target", args.target] if args.target else []
            cargo = str(spec.get("cargo", args.cargo))
            nightly_args = [] if stock else ["-Zno-embed-metadata"]
            profile_args = ["--release"] if spec.get("release") else []
            build = [cargo, *nightly_args, "build", "--offline", "--locked", f"-j{args.jobs}",
                     *profile_args, *target_args, *spec.get("cargo_args", []), *config["cargo_args"]]
            phases = ([("clean", None, None), ("touch", 0, None)]
                      + [(f"edit{i}", i, None) for i in range(1, args.edits + 1)]
                      + [(f"libedit{i}", None, i) for i in range(1, args.lib_edits + 1)])
            arm_record = {"rustc": str(rustc), "rustc_sha256": sha256(rustc),
                          "env": spec.get("env", {}), "flags": flags, "command": build,
                          "phases": []}
            project_record["arms"][arm] = arm_record
            try:
                for phase, step, lib_step in phases:
                    # A new public item changes the crate's metadata, so dependents rebuild.
                    # Write only on change: a rewrite alone updates the mtime and triggers a
                    # rebuild of the crate and its dependents.
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
                    log_dir = args.out / project / arm / phase
                    measurement = run_phase(build, source, env, log_dir)
                    if measurement["exit_code"]:
                        raise RuntimeError(f"{project}/{arm}/{phase} failed; see {log_dir}")
                    binary = target / binary_rel
                    runtime = subprocess.run([str(binary), *config["runtime_args"]], cwd=source,
                                             env=env, text=True, capture_output=True, timeout=120)
                    expected = (f"compression-bench-edit:{step}\n" if step else "") + config["expected_stdout"]
                    if runtime.returncode or runtime.stdout.strip() != expected:
                        raise RuntimeError(f"{project}/{arm}/{phase} runtime mismatch: "
                                           f"{runtime.returncode} {runtime.stdout!r} {runtime.stderr[-500:]!r}")
                    phase_record = {"phase": phase, "measurement": measurement,
                                    "target_census": target_census(target, binary_rel),
                                    # A cross build keeps host artifacts (build scripts, proc
                                    # macros and their dependencies) outside the triple's
                                    # directory; a native build of the target would not
                                    # duplicate them.
                                    "triple_census": (target_census(target / args.target,
                                                                    binary_rel.split("/", 1)[1])
                                                      if args.target else None),
                                    "binary_sha256": sha256(binary)}
                    arm_record["phases"].append(phase_record)
                    (log_dir / "result.json").write_text(json.dumps(phase_record, indent=2) + "\n")
                    final_links = [l for l in measurement["links"]
                                   if is_final_link(l["output"], binary_rel)]
                    census = phase_record["target_census"]
                    print(f"{project}/{arm}/{phase}: {measurement['wall_seconds']:.1f}s, "
                          f"{census['allocated_unique_inode_bytes'] / 2**30:.3f} GiB, "
                          f"RSS {measurement['sampled_process_tree_rss_peak_bytes'] / 2**30:.2f} GiB, "
                          f"{len(measurement['links'])} links, final link "
                          f"{sum(l['seconds'] for l in final_links):.2f}s", flush=True)
                gdb_name = "gdb-multiarch" if args.target else "gdb"
                if shutil.which(gdb_name):
                    binary = target / binary_rel
                    commands = [a for c in config["gdb"] for a in ("-ex", c)]
                    gdb = subprocess.run([gdb_name, "-batch", "-iex", "set auto-load safe-path /",
                                          *commands, str(binary)], cwd=source, text=True,
                                         capture_output=True, timeout=1200)
                    output = gdb.stdout + gdb.stderr
                    arm_record["gdb"] = {
                        "commands": config["gdb"],
                        "line_found": bool(re.search(r"^Line \d+ of", output, re.M)),
                        "type_found": bool(re.search(r"^type = ", output, re.M)),
                        "read_errors": len(re.findall(r"BFD: error|Can't read data", output)),
                    }
                    print(f"{project}/{arm}/gdb: {arm_record['gdb']}", flush=True)
                if arm == args.tests_arm:
                    fixture.write_text(original)
                    if library.read_text() != library_original:
                        library.write_text(library_original)
                    test = [cargo, *nightly_args, "test", "--no-run", "--offline", "--locked",
                            f"-j{args.jobs}", *profile_args, *target_args,
                            *spec.get("cargo_args", []), *config["test_args"]]
                    log_dir = args.out / project / arm / "unit_tests"
                    measurement = run_phase(test, source, env, log_dir)
                    if measurement["exit_code"]:
                        raise RuntimeError(f"{project}/{arm}/unit_tests failed; see {log_dir}")
                    arm_record["unit_tests"] = {
                        "measurement": measurement,
                        "target_census": target_census(target, binary_rel),
                    }
                    print(f"{project}/{arm}/unit_tests: {measurement['wall_seconds']:.1f}s, "
                          f"{len(measurement['links'])} links", flush=True)
            finally:
                fixture.write_text(original)
                if library.read_text() != library_original:
                    library.write_text(library_original)
                (args.out / "results.json").write_text(json.dumps(record, indent=2, default=list) + "\n")
            shutil.rmtree(target)
            shutil.rmtree(scratch, ignore_errors=True)
    (args.out / "results.json").write_text(json.dumps(record, indent=2, default=list) + "\n")


if __name__ == "__main__":
    main()
