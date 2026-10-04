#!/usr/bin/env python3
"""Measure fresh Cargo targets with compiler artifact compression off/on.

The runner intentionally creates a new output tree and never removes targets.
Each arm gets a fresh workspace copy and target directory. Measurements include
sampling overhead; RSS and disk peaks are sampled lower bounds.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import ctypes.util
from datetime import datetime, timezone
import hashlib
import json
import os
import platform
import resource
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib

try:
    import psutil
except ImportError:
    psutil = None


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
INPUTS = ROOT / "docs/target-directory-size/measurements/2026-09-26-no-debug/inputs"
FIXTURE = HERE / "benchmark-fixtures/native-proc-macro"
DEFAULT_WORKLOADS = ("clap", "regex", "serde", "syn", "native-fixture")
EXPECTED_STDOUT = {
    "clap": "clap: validated 5 subcommands and 2 invalid inputs",
    "regex": "regex:2:42",
    "serde": "serde: roundtrip validated; endpoints=2",
    "syn": "syn: parsed Rust file; functions=1 structs=1 enums=0",
    "native-fixture": "compression-fixture: native=42 proc-macro=ok",
}
EXPECTED_TEST_MARKERS = (
    "fixture-test: unit",
    "fixture-test: native",
    "fixture-test: proc-macro",
)
PROFILE_DIR = {"dev": "debug", "release": "release"}
PROFILE_ARMS = {
    "legacy": ("legacy", False, True),
    "fast": ("fast", False, True),
    "balanced": ("balanced", False, True),
    "small": ("small", False, True),
    "incremental_only": ("balanced", True, False),
    "both_fast": ("fast", True, True),
    "both_balanced": ("balanced", True, True),
    "both_small": ("small", True, True),
}


# Remove inherited compiler/build controls so both arms use the exact settings
# recorded below. CARGO_HOME and PATH are retained for the offline registry and
# native tool lookup; their paths are recorded without dumping the full env.
REMOVE_EXACT = {
    "RUSTC", "RUSTDOC", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER",
    "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CARGO_TARGET_DIR",
    "CARGO_BUILD_TARGET", "CARGO_BUILD_RUSTFLAGS", "CARGO_BUILD_RUSTC",
    "CARGO_BUILD_RUSTC_WRAPPER", "CARGO_INCREMENTAL", "CARGO_PROFILE",
    "CARGO_PROFILE_DEV_DEBUG", "CARGO_PROFILE_DEV_INCREMENTAL",
    "CARGO_PROFILE_RELEASE_DEBUG", "CARGO_PROFILE_RELEASE_INCREMENTAL",
    "CC", "CFLAGS", "CXX", "CXXFLAGS", "AR", "RANLIB", "MAKEFLAGS",
    "MFLAGS", "DYLD_INSERT_LIBRARIES", "RUSTC_BOOTSTRAP",
}
REMOVE_PREFIXES = ("CARGO_PROFILE_", "CARGO_BUILD_", "RUSTC_", "SCCACHE_")
REMOVE_NAMES = {"SCCACHE", "CCACHE_DIR", "CCACHE_PREFIX", "CMAKE_BUILD_PARALLEL_LEVEL"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_fingerprint(path: Path, *, exclude_logs: bool = False) -> dict[str, object]:
    """Hash a fixture's relative file names and contents deterministically."""
    digest = hashlib.sha256()
    files = []
    for item in sorted(path.rglob("*")):
        if not item.is_file() or item.is_symlink():
            continue
        rel = item.relative_to(path).as_posix()
        if exclude_logs and rel in {"build.log", "build-metadata-once.log"}:
            continue
        file_hash = sha256_file(item)
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(file_hash))
        files.append({"path": rel, "bytes": item.stat().st_size, "sha256": file_hash})
    return {"sha256": digest.hexdigest(), "files": files}


def git_root(path: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return Path(result.stdout.strip()).resolve()


def git_snapshot(repo: Path) -> dict[str, object]:
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout.strip()
    branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout
    diff = subprocess.run(
        ["git", "-C", str(repo), "diff", "--binary", "--no-ext-diff", "HEAD", "--"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout
    status_lines = [line for line in status.splitlines()]
    untracked_paths = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout.decode().split("\0")
    untracked = {}
    for name in sorted(filter(None, untracked_paths)):
        path = repo / name
        if path.is_symlink():
            untracked[name] = {"symlink": os.readlink(path)}
        elif path.is_file():
            untracked[name] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
    return {
        "repo": str(repo),
        "head": head,
        "branch": branch or None,
        "tracked_dirty": any(line[:2].strip() not in {"??", ""} for line in status_lines),
        "status": status_lines,
        "diff_from_head_sha256": sha256_bytes(diff),
        "untracked_contents": untracked,
    }


def collect_path_dependencies(manifest: Path) -> tuple[list[Path], list[Path]]:
    """Walk path dependencies and their manifests, returning paths and Git roots."""
    seen_manifests: set[Path] = set()
    packages: set[Path] = set()
    queue = [manifest.resolve()]
    while queue:
        current_manifest = queue.pop()
        if current_manifest in seen_manifests or not current_manifest.is_file():
            continue
        seen_manifests.add(current_manifest)
        try:
            document = tomllib.loads(current_manifest.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        stack = [document]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                raw_path = value.get("path")
                if isinstance(raw_path, str):
                    package = (current_manifest.parent / raw_path).resolve()
                    if package.is_file() and package.name == "Cargo.toml":
                        package = package.parent
                    if package.is_dir():
                        packages.add(package)
                        child_manifest = package / "Cargo.toml"
                        if child_manifest.is_file():
                            queue.append(child_manifest.resolve())
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
    roots = set()
    for package in packages:
        root = git_root(package)
        # The synthetic fixture itself is under the main checkout; its files
        # are already content-hashed and do not represent an external dep repo.
        if root is not None and root != ROOT:
            roots.add(root)
    return sorted(packages), sorted(roots)


def dependency_snapshot(roots: list[Path]) -> dict[str, dict[str, object]]:
    return {str(root): git_snapshot(root) for root in roots}


def process_tree_snapshot(root_pid: int) -> list[dict[str, int | float]]:
    """Sample only this command's process tree; psutil avoids system-wide ps."""
    if psutil is None:
        return []
    try:
        root = psutil.Process(root_pid)
        processes = [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []
    result = []
    for process in processes:
        try:
            memory = process.memory_info()
            times = process.cpu_times()
            result.append({
                "pid": process.pid,
                "ppid": process.ppid(),
                "rss_bytes": memory.rss,
                "cpu_seconds": times.user + times.system,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return result


def census(roots: list[Path], *, with_files: bool = False) -> dict[str, object]:
    start = time.monotonic()
    logical_total = 0
    inode_logical: dict[tuple[int, int], int] = {}
    inode_allocated: dict[tuple[int, int], int] = {}
    entries = 0
    files = []
    for root in roots:
        if not root.exists():
            continue
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as listing:
                    children = list(listing)
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                continue
            for entry in children:
                try:
                    stat = entry.stat(follow_symlinks=False)
                except (FileNotFoundError, PermissionError):
                    continue
                entries += 1
                key = (stat.st_dev, stat.st_ino)
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    if with_files:
                        files.append({"path": str(Path(entry.path)), "kind": "directory",
                                      "logical_bytes": 0, "allocated_bytes": 0,
                                      "device": stat.st_dev, "inode": stat.st_ino,
                                      "hardlink_count": stat.st_nlink})
                    continue
                size = stat.st_size if entry.is_file(follow_symlinks=False) else 0
                allocated = getattr(stat, "st_blocks", 0) * 512
                logical_total += size
                inode_logical[key] = max(inode_logical.get(key, 0), size)
                inode_allocated[key] = max(inode_allocated.get(key, 0), allocated)
                if with_files:
                    files.append({"path": str(Path(entry.path)), "kind": "file",
                                  "logical_bytes": size, "allocated_bytes": allocated,
                                  "device": stat.st_dev, "inode": stat.st_ino,
                                  "hardlink_count": stat.st_nlink})
    answer: dict[str, object] = {
        "roots": [str(root) for root in roots],
        "logical_path_bytes": logical_total,
        "logical_unique_inode_bytes": sum(inode_logical.values()),
        "allocated_unique_inode_bytes": sum(inode_allocated.values()),
        "entries": entries,
        "unique_inodes": len(inode_logical),
        "hardlink_alias_bytes": logical_total - sum(inode_logical.values()),
        "scan_seconds": time.monotonic() - start,
        "allocated_measurement": "st_blocks * 512, unique (device,inode); APFS clone extents may be shared",
    }
    if with_files:
        answer["files"] = files
    return answer


class Sampler:
    def __init__(self, pid: int, roots: list[Path], interval: float):
        self.pid = pid
        self.roots = roots
        self.interval = interval
        self.stop_event = threading.Event()
        self.samples: list[dict[str, object]] = []
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        last_cpu: dict[int, float] = {}
        sampled_cpu_delta = 0.0
        started = time.monotonic()
        while True:
            scan_start = time.monotonic()
            processes = process_tree_snapshot(self.pid)
            disk = census(self.roots)
            scan_seconds = time.monotonic() - scan_start
            for proc in processes:
                pid = int(proc["pid"])
                cpu = float(proc["cpu_seconds"])
                if pid in last_cpu and cpu >= last_cpu[pid]:
                    sampled_cpu_delta += cpu - last_cpu[pid]
                last_cpu[pid] = cpu
            self.samples.append({
                "relative_seconds": time.monotonic() - started,
                "host_load_average": list(os.getloadavg()),
                "processes": processes,
                "tree_rss_sum_bytes": sum(int(p["rss_bytes"]) for p in processes),
                "tree_cpu_seconds_sampled_lower_bound": sampled_cpu_delta,
                "disk": disk,
                "sample_overhead_seconds": scan_seconds,
            })
            if self.stop_event.wait(self.interval):
                break

    def stop(self) -> dict[str, object]:
        if self.thread is not None:
            self.stop_event.set()
            self.thread.join()
        rss_peak = max((int(s["tree_rss_sum_bytes"]) for s in self.samples), default=0)
        cpu_lb = max((float(s["tree_cpu_seconds_sampled_lower_bound"]) for s in self.samples), default=0.0)
        disk_logical_peak = max(
            (int(s["disk"]["logical_unique_inode_bytes"]) for s in self.samples), default=0
        )
        disk_allocated_peak = max(
            (int(s["disk"]["allocated_unique_inode_bytes"]) for s in self.samples), default=0
        )
        scan_overhead = sum(float(s["sample_overhead_seconds"]) for s in self.samples)
        return {
            "sample_count": len(self.samples),
        "sample_interval_seconds": self.interval,
        "process_sampler": "psutil process tree",
            "sampled_process_tree_rss_sum_peak_bytes": rss_peak,
            "sampled_process_tree_cpu_seconds_lower_bound": cpu_lb,
            "sampled_target_tmp_logical_unique_inode_peak_bytes": disk_logical_peak,
            "sampled_target_tmp_allocated_unique_inode_peak_bytes": disk_allocated_peak,
            "disk_census_scan_seconds_total": scan_overhead,
            "sampling_caveats": [
                "Short-lived processes and files between samples are missed; peaks are lower bounds.",
                "Summed RSS can count shared pages more than once.",
                "Disk allocated bytes use st_blocks and do not account for APFS clone sharing.",
                "Sampling and directory scans add overhead to both arms; their time is included.",
            ],
        }


def split_env() -> tuple[dict[str, str], list[str]]:
    env = os.environ.copy()
    removed = []
    for key in list(env):
        if (key in REMOVE_EXACT or key in REMOVE_NAMES or
                any(key.startswith(prefix) for prefix in REMOVE_PREFIXES)):
            removed.append(key)
            env.pop(key, None)
    return env, sorted(removed)


def cargo_config_facts(env: dict[str, str], workspaces: list[Path]) -> list[dict[str, object]]:
    candidates = set()
    for workspace in workspaces:
        for parent in (workspace, *workspace.parents):
            for name in ("config", "config.toml"):
                path = parent / ".cargo" / name
                if path.is_file():
                    candidates.add(path.resolve())
    cargo_home = Path(env.get("CARGO_HOME", str(Path.home() / ".cargo")))
    for name in ("config", "config.toml"):
        path = cargo_home / name
        if path.is_file():
            candidates.add(path.resolve())
    facts = []
    for path in sorted(candidates):
        raw = path.read_bytes()
        try:
            doc = tomllib.loads(raw.decode())
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            doc = {}
        build = doc.get("build", {}) if isinstance(doc, dict) else {}
        wrappers = []
        if isinstance(build, dict):
            for key in ("rustc-wrapper", "rustc-workspace-wrapper"):
                if build.get(key):
                    wrappers.append(key)
        config_env = doc.get("env", {}) if isinstance(doc, dict) else {}
        sensitive_env = sorted(
            key for key in config_env
            if key in {"RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC", "RUSTC_WRAPPER",
                       "RUSTC_WORKSPACE_WRAPPER", "RUSTC_BOOTSTRAP", "CC", "CFLAGS",
                       "CXX", "CXXFLAGS", "AR", "RANLIB", "CARGO_TARGET_DIR",
                       "CARGO_INCREMENTAL"} or key.startswith(("CARGO_PROFILE_", "CARGO_BUILD_"))
        ) if isinstance(config_env, dict) else []
        facts.append({
            "path": str(path), "sha256": sha256_bytes(raw),
            "build_wrapper_keys_present": wrappers,
            "sensitive_env_keys_present": sensitive_env,
            "has_build_target_dir": isinstance(build, dict) and "target-dir" in build,
            "has_build_rustflags": isinstance(build, dict) and any(
                key in build for key in ("rustflags", "rustc-flags")
            ),
            "has_env_section": isinstance(doc, dict) and "env" in doc,
        })
        if wrappers:
            raise RuntimeError(
                f"Cargo config {path} sets {', '.join(wrappers)}; refusing to benchmark with a wrapper"
            )
        if sensitive_env:
            raise RuntimeError(
                f"Cargo config {path} injects compiler-sensitive env keys {sensitive_env}; refusing to benchmark"
            )
    return facts


def capture_probe(command: list[str], env: dict[str, str], cwd: Path, out: Path) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (out).write_text(result.stdout)
    if result.returncode:
        raise RuntimeError(f"Probe failed ({result.returncode}): {' '.join(command)}; see {out}")
    return result.stdout


def parse_cargo_rustc_invocations(stderr_path: Path, stdout_path: Path) -> list[list[str]]:
    invocations = []
    for path in (stdout_path, stderr_path):
        for line in path.read_text(errors="replace").splitlines():
            if "Running " not in line or "rustc" not in line or "--crate-name" not in line:
                continue
            command_part = line[line.find("Running ") + len("Running "):].strip()
            if len(command_part) >= 2 and command_part[0] == "`" and command_part[-1] == "`":
                command_part = command_part[1:-1]
            try:
                words = shlex.split(command_part)
            except ValueError:
                continue
            if words and "rustc" in Path(words[0]).name and "--crate-name" in words:
                invocations.append(words)
    return invocations


def audit_invocations(invocations: list[list[str]], arm: str, separated: bool, incremental: bool = False, expected_rustc: str | None = None) -> dict[str, object]:
    errors = []
    per_invocation = []
    expected_compression = next(f for f in arm_flags(arm) if f.startswith("-Zcompress-artifacts="))
    for command in invocations:
        if expected_rustc and Path(command[0]).resolve() != Path(expected_rustc).resolve():
            errors.append(f"unexpected compiler executable: {command[0]}")
        args = command[1:]
        joined = []
        skip = False
        for index, arg in enumerate(args):
            if arg in {"-Z", "-C"} and index + 1 < len(args):
                joined.append(arg + args[index + 1])
                skip = True
                continue
            if skip:
                skip = False
                continue
            joined.append(arg)
        has_debug0 = "-Cdebuginfo=0" in joined
        if not has_debug0:
            # Handle both -Cdebuginfo=0 and `-C debuginfo=0` spellings.
            has_debug0 = any(joined[i] == "-C" and i + 1 < len(joined) and
                             joined[i + 1] == "debuginfo=0" for i in range(len(joined)))
        has_compression = expected_compression in joined
        has_metadata_off = "-Zembed-metadata=no" in joined
        expected_options = {flag.split("=", 1)[0]: flag.split("=", 1)[1]
                            for flag in arm_flags(arm) if flag.startswith(("-Z", "-C")) and "=" in flag}
        expected_options["-Cdebuginfo"] = "0"
        if separated:
            expected_options["-Zembed-metadata"] = "no"
        for name, expected in expected_options.items():
            values = [arg.split("=", 1)[1] for arg in joined if arg.startswith(name + "=")]
            if any(value != expected for value in values):
                errors.append(f"conflicting {name}: {values}; expected {expected}")
        crate_type = None
        for index, arg in enumerate(joined):
            if arg == "--crate-type" and index + 1 < len(joined):
                crate_type = joined[index + 1]
            elif arg.startswith("--crate-type="):
                crate_type = arg.split("=", 1)[1]
        if separated:
            has_metadata_off = has_metadata_off or any(
                joined[i] == "-Z" and i + 1 < len(joined) and
                joined[i + 1] == "embed-metadata=no" for i in range(len(joined))
            )
        incremental_args = [arg for arg in joined if arg.startswith("incremental=") or arg.startswith("-Cincremental=")]
        if not has_debug0:
            errors.append("missing explicit -Cdebuginfo=0")
        if not has_compression:
            errors.append(f"missing {expected_compression}")
        # Cargo need not emit embed-able metadata for executable and proc-macro
        # outputs. Verify the explicit mode for library-like crate outputs.
        metadata_applicable = crate_type in {"lib", "rlib"} and "--test" not in joined
        if separated and metadata_applicable and not has_metadata_off:
            errors.append("metadata-separated arm lacks rustc -Zembed-metadata=no")
        if incremental_args and not incremental:
            errors.append(f"unexpected incremental arguments: {incremental_args}")
        for flag in arm_flags(arm):
            if flag.startswith("-Z") and flag not in joined:
                errors.append(f"missing {flag}")
        if not incremental and any(arg.startswith("incremental=") or arg.startswith("-Cincremental=") for arg in joined):
            errors.append("incremental codegen enabled unexpectedly")
        per_invocation.append({
            "crate_name": joined[joined.index("--crate-name") + 1],
            "crate_type": crate_type,
            "debug0": has_debug0, "compression_flag": expected_compression if has_compression else None,
            "metadata_mode_checked": separated and metadata_applicable,
            "embed_metadata_off": has_metadata_off, "incremental_arguments": incremental_args,
            "incremental_codegen": any(arg.startswith("incremental=") or arg.startswith("-Cincremental=") for arg in joined),
        })
    return {"count": len(invocations), "invocations": per_invocation,
            "audit_errors": sorted(set(errors))}


def run_measured(command: list[str], *, cwd: Path, env: dict[str, str],
                 target: Path, temp_dir: Path, log_dir: Path, label: str,
                 interval: float, timeout: float | None = None) -> dict[str, object]:
    log_dir.mkdir(parents=True, exist_ok=False)
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    cmd_record = {
        "command": command, "cwd": str(cwd), "label": label,
        "started_utc": utc_now(),
        "environment_overrides": {
            key: env[key] for key in (
                "RUSTC", "RUSTC_BOOTSTRAP", "CARGO_TARGET_DIR", "CARGO_INCREMENTAL",
                "CARGO_ENCODED_RUSTFLAGS", "CARGO_PROFILE_DEV_DEBUG",
                "CARGO_PROFILE_DEV_INCREMENTAL", "CARGO_PROFILE_RELEASE_DEBUG",
                "CARGO_PROFILE_RELEASE_INCREMENTAL", "TMPDIR", "CARGO_HOME",
            ) if key in env
        },
    }
    cmd_record["environment_overrides"]["CARGO_ENCODED_RUSTFLAGS"] = [
        part for part in env.get("CARGO_ENCODED_RUSTFLAGS", "").split("\x1f") if part
    ]
    (log_dir / "command.json").write_text(json.dumps(cmd_record, indent=2) + "\n")
    wall_start = time.monotonic()
    child_usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    with stdout_path.open("wb") as stdout_stream, stderr_path.open("wb") as stderr_stream:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stdout_stream,
                                    stderr=stderr_stream, start_new_session=True)
        sampler = Sampler(process.pid, [target, temp_dir], interval)
        sampler.start()
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            exit_code = process.returncode
            cmd_record["timed_out"] = True
        wall_seconds = time.monotonic() - wall_start
        child_usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        monitor = sampler.stop()
    sample_path = log_dir / "samples.jsonl"
    with sample_path.open("w") as stream:
        for sample in sampler.samples:
            stream.write(json.dumps(sample, sort_keys=True) + "\n")
    invocations = parse_cargo_rustc_invocations(stderr_path, stdout_path) if "cargo" in Path(command[0]).name else []
    result = {
        **cmd_record, "exit_code": exit_code,
        "wall_seconds": wall_seconds,
        "child_user_cpu_seconds": child_usage_after.ru_utime - child_usage_before.ru_utime,
        "child_system_cpu_seconds": child_usage_after.ru_stime - child_usage_before.ru_stime,
        "stdout_log": str(stdout_path), "stderr_log": str(stderr_path),
        "sample_file": str(sample_path),
        "monitor": monitor,
    }
    # Store invocations separately so command records stay readable.
    if invocations:
        (log_dir / "rustc-invocations.json").write_text(json.dumps(invocations, indent=2) + "\n")
    result["rustc_invocation_count"] = len(invocations)
    (log_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def run_plain(command: list[str], *, cwd: Path, env: dict[str, str],
              log_dir: Path, label: str, timeout: float | None = None) -> dict[str, object]:
    log_dir.mkdir(parents=True, exist_ok=False)
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    command_record = {"label": label, "command": command, "cwd": str(cwd),
                      "started_utc": utc_now()}
    (log_dir / "command.json").write_text(json.dumps(command_record, indent=2) + "\n")
    started = time.monotonic()
    with stdout_path.open("wb") as stdout_stream, stderr_path.open("wb") as stderr_stream:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stdout_stream,
                                    stderr=stderr_stream, start_new_session=True)
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait()
            exit_code = process.returncode
    result = {
        **command_record, "exit_code": exit_code,
        "wall_seconds": time.monotonic() - started,
        "stdout_log": str(stdout_path), "stderr_log": str(stderr_path),
    }
    (log_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def cargo_command(cargo: str, arm: str, profile: str, jobs: int,
                  action: str, *, test_nocapture: bool = False) -> list[str]:
    command = [cargo]
    if arm != "embedded_off":
        command.append("-Zno-embed-metadata")
    command += [action, "--offline", "--locked", "--jobs", str(jobs), "--verbose"]
    if profile == "release":
        command.append("--release")
    if test_nocapture:
        command += ["--", "--nocapture"]
    return command


def binary_path(target: Path, profile: str, package: str) -> Path:
    return target / PROFILE_DIR[profile] / package


def verify_runtime(binary: Path, expected: str, *, cwd: Path, env: dict[str, str],
                   log_dir: Path) -> dict[str, object]:
    if not binary.is_file():
        raise RuntimeError(f"expected runtime binary is missing: {binary}")
    result = run_plain([str(binary)], cwd=cwd, env=env, log_dir=log_dir,
                       label="runtime-oracle", timeout=60)
    stdout = Path(result["stdout_log"]).read_text(errors="replace").strip()
    stderr = Path(result["stderr_log"]).read_text(errors="replace")
    result.update({"binary": str(binary), "binary_sha256": sha256_file(binary),
                   "expected_stdout": expected, "actual_stdout": stdout,
                   "stdout_matches": stdout == expected, "stderr": stderr})
    if result["exit_code"] != 0 or stdout != expected:
        raise RuntimeError(f"runtime oracle failed for {binary}; see {log_dir}")
    return result


def copy_workload(name: str, workspace: Path) -> tuple[str, Path]:
    source = FIXTURE if name == "native-fixture" else INPUTS / name
    ignore = shutil.ignore_patterns("build.log", "build-metadata-once.log", "target")
    shutil.copytree(source, workspace, ignore=ignore)
    manifest = tomllib.loads((workspace / "Cargo.toml").read_text())
    return str(manifest["package"]["name"]), source


def tool_identity(path: Path) -> dict[str, object]:
    return {"path": str(path), "resolved_path": str(path.resolve()),
            "sha256": sha256_file(path) if path.is_file() else None}


def find_build_record(rustc: Path) -> str | None:
    expected = rustc.resolve()
    base = ROOT / "docs/target-directory-size/measurements"
    driver_dir = expected.parent.parent / "lib"
    actual_driver = {
        str(path.resolve()): sha256_file(path)
        for path in sorted(driver_dir.glob("*rustc_driver*")) if path.is_file()
    }
    launcher_sha = sha256_file(rustc)
    matches = []
    for path in sorted(base.glob("*/build-*/build.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        recorded = record.get("rustc")
        if not recorded or Path(recorded).resolve() != expected:
            continue
        if record.get("exit_code") != 0 or not record.get("sources_unchanged_during_build"):
            continue
        if record.get("rustc_launcher_sha256") != launcher_sha:
            continue
        recorded_driver = record.get("driver_libraries", {})
        normalized_recorded_driver = {
            str(Path(path).resolve()): digest for path, digest in recorded_driver.items()
        }
        if normalized_recorded_driver != actual_driver:
            continue
        matches.append((record.get("finished_utc", ""), path))
    if not matches:
        return None
    return str(max(matches, key=lambda item: item[0])[1])


def selected_arms(include_embedded: bool) -> list[str]:
    arms = ["dedup_off", "compress_on"]
    if include_embedded:
        arms.insert(0, "embedded_off")
    return arms


def arm_flags(arm: str) -> list[str]:
    if arm in PROFILE_ARMS:
        profile, incremental_compression, artifacts = PROFILE_ARMS[arm]
        return ["-Cdebuginfo=0", f"-Zcompress-artifacts={'yes' if artifacts else 'no'}",
                f"-Zcompress-incremental={'yes' if incremental_compression else 'no'}",
                f"-Zartifact-compression-profile={profile}"]
    compression = "-Zcompress-artifacts=yes" if arm == "compress_on" else "-Zcompress-artifacts=no"
    return ["-Cdebuginfo=0", compression]


def run_arm(*, out: Path, workload: str, profile: str, repetition: int,
            arm: str, rustc: str, cargo: str, base_env: dict[str, str],
            removed_env: list[str], jobs: int, interval: float,
            timeout: float | None, incremental: bool = False, semantic_edits: int = 0) -> dict[str, object]:
    run_dir = out / workload / profile / f"rep-{repetition:02d}" / arm
    run_dir.mkdir(parents=True, exist_ok=False)
    workspace = run_dir / "workspace"
    target = run_dir / "target"
    temp_dir = run_dir / "tmp"
    temp_dir.mkdir(parents=True)
    package, source = copy_workload(workload, workspace)
    original_fingerprint = tree_fingerprint(source, exclude_logs=workload != "native-fixture")
    _, dependency_roots = collect_path_dependencies(source / "Cargo.toml")
    dependencies_start = dependency_snapshot(dependency_roots)

    env = base_env.copy()
    env.update({
        "RUSTC": rustc,
        "RUSTC_BOOTSTRAP": "1",
        "CARGO_TARGET_DIR": str(target),
        "CARGO_INCREMENTAL": "1" if incremental else "0",
        "TMPDIR": str(temp_dir), "TMP": str(temp_dir), "TEMP": str(temp_dir),
    })
    profile_key = profile.upper()
    env[f"CARGO_PROFILE_{profile_key}_DEBUG"] = "0"
    env[f"CARGO_PROFILE_{profile_key}_INCREMENTAL"] = "true" if incremental else "false"
    env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(arm_flags(arm))
    start_disk = census([target, temp_dir])
    run_record: dict[str, object] = {
        "workload": workload, "package": package, "profile": profile,
        "repetition": repetition, "arm": arm, "incremental": incremental, "semantic_edits": semantic_edits,
        "started_utc": utc_now(), "workspace": str(workspace),
        "target_dir": str(target), "temp_dir": str(temp_dir),
        "removed_inherited_environment_names": removed_env,
        "cargo_home": env.get("CARGO_HOME", str(Path.home() / ".cargo")),
        "input_fingerprint": original_fingerprint,
        "dependency_repositories_at_start": dependencies_start,
        "start_disk_census": start_disk,
        "source_edit": "append comment before edited_rebuild; semantic phases print an incrementing checked value from main",
        "edit_kind": "comment-only first edit; subsequent semantic edits change an output checked by the runtime oracle" if semantic_edits else "comment-only; does not represent a semantic source edit",
        "phases": [],
    }
    phases = ["clean_build", "noop", "edited_rebuild"] + [f"semantic_edit_{i + 1}" for i in range(semantic_edits)]
    phase_disk_before = start_disk
    all_audits = []
    for phase in phases:
        if phase == "edited_rebuild":
            with (workspace / "src/main.rs").open("a") as stream:
                stream.write("\n// Controlled one-file edit for artifact reuse measurement.\n")
        if phase.startswith("semantic_edit_"):
            main = workspace / "src/main.rs"
            text = main.read_text()
            marker = "fn main() {"
            if marker not in text:
                raise RuntimeError(f"cannot inject controlled semantic edit into {main}")
            step = int(phase.rsplit("_", 1)[1])
            def statement(value):
                return f'\n    println!("compression-bench-edit:{{}}", std::hint::black_box({value}u64));'
            if step == 1:
                text = text.replace(marker, marker + statement(step), 1)
            else:
                assert statement(step - 1) in text
                text = text.replace(statement(step - 1), statement(step), 1)
            main.write_text(text)
        phase_commands = ["build"]
        if workload == "native-fixture":
            phase_commands.append("test")
        results = []
        phase_samples = []
        for cargo_action in phase_commands:
            command = cargo_command(
                cargo, arm, profile, jobs,
                "test" if cargo_action == "test" else "build",
                test_nocapture=cargo_action == "test",
            )
            command_dir = run_dir / "commands" / phase / cargo_action
            result = run_measured(
                command, cwd=workspace, env=env, target=target, temp_dir=temp_dir,
                log_dir=command_dir, label=f"{phase}:{cargo_action}", interval=interval,
                timeout=timeout,
            )
            invocation_words = []
            invocations_file = command_dir / "rustc-invocations.json"
            if invocations_file.is_file():
                invocation_words = json.loads(invocations_file.read_text())
            audit = audit_invocations(
                invocation_words, arm, separated=arm != "embedded_off", incremental=incremental, expected_rustc=rustc
            )
            if phase == "noop" and audit["count"] != 0:
                raise RuntimeError(
                    f"no-op phase compiled {audit['count']} Rust crates for {workload}/{arm}; see {command_dir}"
                )
            if audit["audit_errors"]:
                raise RuntimeError(
                    f"rustc flags failed audit for {workload}/{arm}/{phase}: {audit['audit_errors']}"
                )
            result["rustc_audit"] = audit
            all_audits.append(audit)
            if result["exit_code"] != 0:
                raise RuntimeError(f"Cargo command failed ({result['exit_code']}): {command}; see {command_dir}")
            if cargo_action == "test":
                output = Path(result["stdout_log"]).read_text(errors="replace") + Path(result["stderr_log"]).read_text(errors="replace")
                missing = [marker for marker in EXPECTED_TEST_MARKERS if marker not in output]
                result["expected_test_markers"] = list(EXPECTED_TEST_MARKERS)
                result["missing_test_markers"] = missing
                if missing:
                    raise RuntimeError(f"synthetic fixture tests did not emit {missing}; see {command_dir}")
            results.append(result)
            phase_samples.append(result["monitor"])
        if incremental and phase != "noop" and not any(
            invocation["incremental_codegen"]
            for result in results for invocation in result["rustc_audit"]["invocations"]
        ):
            raise RuntimeError(f"no actual incremental codegen invocation for {workload}/{arm}/{phase}")
        expected_stdout = EXPECTED_STDOUT[workload]
        if phase.startswith("semantic_edit_"):
            expected_stdout = f"compression-bench-edit:{phase.rsplit('_', 1)[1]}\n" + expected_stdout
        runtime = verify_runtime(
            binary_path(target, profile, package), expected_stdout,
            cwd=workspace, env=env,
            log_dir=run_dir / "runtime" / phase,
        )
        disk_after = census([target, temp_dir])
        phase_summary = {
            "name": phase,
            "cargo_commands": results,
            "runtime_oracle": runtime,
            "disk_before": phase_disk_before,
            "disk_after": disk_after,
            "disk_logical_growth_bytes": int(disk_after["logical_unique_inode_bytes"]) - int(phase_disk_before["logical_unique_inode_bytes"]),
            "disk_allocated_growth_bytes": int(disk_after["allocated_unique_inode_bytes"]) - int(phase_disk_before["allocated_unique_inode_bytes"]),
            "cargo_wall_seconds_sum": sum(float(item["wall_seconds"]) for item in results),
            "child_user_cpu_seconds_sum": sum(float(item["child_user_cpu_seconds"]) for item in results),
            "child_system_cpu_seconds_sum": sum(float(item["child_system_cpu_seconds"]) for item in results),
            "sampled_process_tree_rss_peak_bytes": max(
                (int(monitor["sampled_process_tree_rss_sum_peak_bytes"]) for monitor in phase_samples), default=0
            ),
            "sampled_process_tree_cpu_seconds_lower_bound_sum": sum(
                float(monitor["sampled_process_tree_cpu_seconds_lower_bound"]) for monitor in phase_samples
            ),
            "sampled_target_tmp_logical_peak_bytes": max(
                (int(monitor["sampled_target_tmp_logical_unique_inode_peak_bytes"]) for monitor in phase_samples), default=0
            ),
            "sampled_target_tmp_allocated_peak_bytes": max(
                (int(monitor["sampled_target_tmp_allocated_unique_inode_peak_bytes"]) for monitor in phase_samples), default=0
            ),
        }
        if phase != "noop" and sum(
            int(item["rustc_audit"]["count"]) for item in results
        ) == 0:
            raise RuntimeError(f"{phase} ran no rustc commands for {workload}/{arm}; see {run_dir}")
        (run_dir / f"{phase}.json").write_text(json.dumps(phase_summary, indent=2) + "\n")
        run_record["phases"].append(phase_summary)
        phase_disk_before = disk_after

    final_manifest = census([target, temp_dir], with_files=True)
    files = final_manifest.pop("files")
    with (run_dir / "final-file-census.jsonl").open("w") as stream:
        for entry in files:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")
    run_record["final_target_tmp_census"] = final_manifest
    run_record["all_rustc_invocations_audited"] = sum(int(a["count"]) for a in all_audits)
    run_record["dependency_repositories_at_end"] = dependency_snapshot(dependency_roots)
    run_record["dependency_repositories_unchanged"] = (
        run_record["dependency_repositories_at_start"] == run_record["dependency_repositories_at_end"]
    )
    run_record["input_unchanged"] = tree_fingerprint(
        source, exclude_logs=workload != "native-fixture"
    ) == original_fingerprint
    run_record["finished_utc"] = utc_now()
    (run_dir / "run.json").write_text(json.dumps(run_record, indent=2) + "\n")
    if not run_record["dependency_repositories_unchanged"]:
        raise RuntimeError(f"path dependency state changed during run: {run_dir}")
    if not run_record["input_unchanged"]:
        raise RuntimeError(f"source fixture changed during run: {source}")
    return run_record


def medians(records: list[dict[str, object]]) -> dict[str, object]:
    from statistics import median

    samples = defaultdict(list)
    for record in records:
        for phase in record["phases"]:
            key = (record["workload"], record["profile"], record["arm"], phase["name"])
            samples[key].append(phase)
    rows = []
    for key, phases in sorted(samples.items()):
        rows.append({
            "workload": key[0], "profile": key[1], "arm": key[2], "phase": key[3],
            "repetitions": len(phases),
            "median_cargo_wall_seconds": median(float(p["cargo_wall_seconds_sum"]) for p in phases),
            "median_child_user_cpu_seconds": median(float(p["child_user_cpu_seconds_sum"]) for p in phases),
            "median_child_system_cpu_seconds": median(float(p["child_system_cpu_seconds_sum"]) for p in phases),
            "median_disk_after_logical_bytes": median(int(p["disk_after"]["logical_unique_inode_bytes"]) for p in phases),
            "median_disk_after_allocated_bytes": median(int(p["disk_after"]["allocated_unique_inode_bytes"]) for p in phases),
            "median_disk_logical_growth_bytes": median(int(p["disk_logical_growth_bytes"]) for p in phases),
            "median_disk_allocated_growth_bytes": median(int(p["disk_allocated_growth_bytes"]) for p in phases),
            "median_sampled_rss_peak_bytes": median(int(p["sampled_process_tree_rss_peak_bytes"]) for p in phases),
            "median_sampled_cpu_seconds_lower_bound": median(float(p["sampled_process_tree_cpu_seconds_lower_bound_sum"]) for p in phases),
            "median_sampled_target_tmp_logical_peak_bytes": median(int(p["sampled_target_tmp_logical_peak_bytes"]) for p in phases),
            "median_sampled_target_tmp_allocated_peak_bytes": median(int(p["sampled_target_tmp_allocated_peak_bytes"]) for p in phases),
        })
    comparisons = []
    by_pair = defaultdict(dict)
    for row in rows:
        if row["arm"] in {"dedup_off", "compress_on"}:
            by_pair[(row["workload"], row["profile"], row["phase"])][row["arm"]] = row
    for key, arms in sorted(by_pair.items()):
        if set(arms) != {"dedup_off", "compress_on"}:
            continue
        before, after = arms["dedup_off"], arms["compress_on"]
        comparison = {"workload": key[0], "profile": key[1], "phase": key[2],
                      "baseline_arm": "dedup_off", "compressed_arm": "compress_on"}
        for metric in ("median_disk_after_logical_bytes", "median_disk_after_allocated_bytes",
                       "median_disk_logical_growth_bytes", "median_disk_allocated_growth_bytes",
                       "median_cargo_wall_seconds", "median_child_user_cpu_seconds",
                       "median_child_system_cpu_seconds", "median_sampled_rss_peak_bytes"):
            baseline, compressed = float(before[metric]), float(after[metric])
            comparison[metric + "_delta_compressed_minus_baseline"] = compressed - baseline
            comparison[metric + "_ratio_compressed_over_baseline"] = compressed / baseline if baseline else None
        comparisons.append(comparison)
    return {"rows": rows, "paired_comparisons": comparisons,
            "note": "Medians are per workload/profile/phase; sampled RSS, process CPU and disk peaks are lower bounds. Compression comparison holds metadata separation constant."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rustc", required=True, type=Path)
    parser.add_argument("--cargo", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--profile", choices=("dev", "release"), default="dev")
    parser.add_argument("--workloads", nargs="*", choices=DEFAULT_WORKLOADS,
                        default=list(DEFAULT_WORKLOADS))
    parser.add_argument("--incremental", action="store_true", help="enable Cargo incremental codegen in every arm")
    parser.add_argument("--semantic-edits", type=int, default=0)
    parser.add_argument("--arms", nargs="+", choices=["dedup_off", "compress_on", "embedded_off", *PROFILE_ARMS])
    parser.add_argument("--include-embedded-baseline", action="store_true",
                        help="also measure embedded metadata with compression disabled")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--sample-interval", type=float, default=0.05)
    parser.add_argument("--timeout", type=float, default=None,
                        help="optional timeout in seconds for each Cargo command")
    args = parser.parse_args()
    if args.repetitions < 1 or args.jobs < 1 or args.sample_interval <= 0:
        parser.error("repetitions/jobs must be positive and sample interval must be greater than zero")
    if args.semantic_edits < 0:
        parser.error("semantic edits must be nonnegative")
    if args.incremental and args.semantic_edits < 1:
        parser.error("incremental benchmarks require --semantic-edits >= 1")
    if args.arms and any(PROFILE_ARMS.get(a, (None, False, False))[1] for a in args.arms) and not args.incremental:
        parser.error("incremental compression arms require --incremental")
    if not args.workloads:
        parser.error("select at least one workload")
    if psutil is None:
        raise RuntimeError("psutil is required for process-tree sampling; run with `uv run --no-project --with psutil python`")
    rustc, cargo = args.rustc.resolve(), args.cargo.resolve()
    if not rustc.is_file() or not os.access(rustc, os.X_OK):
        parser.error(f"--rustc is not an executable file: {rustc}")
    if not cargo.is_file() or not os.access(cargo, os.X_OK):
        parser.error(f"--cargo is not an executable file: {cargo}")
    out = args.out.resolve()
    base_env, removed_names = split_env()
    config_paths = [INPUTS / name for name in args.workloads if name != "native-fixture"]
    if "native-fixture" in args.workloads:
        config_paths.append(FIXTURE)
    config_facts = cargo_config_facts(base_env, config_paths)
    clang, ar = shutil.which("clang"), shutil.which("ar")
    if "native-fixture" in args.workloads and (not clang or not ar):
        raise RuntimeError("native-fixture requires clang and ar on PATH")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.mkdir(exist_ok=False)
    except FileExistsError:
        parser.error(f"--out already exists; choose a new empty path: {out}")

    # These probes do not compile anything. Save their raw output before any run.
    probe_dir = out / "probes"
    probe_dir.mkdir()
    rustc_version = capture_probe([str(rustc), "-vV"], base_env, ROOT, probe_dir / "rustc-vV.txt")
    rustc_help = capture_probe([str(rustc), "-Zhelp"], base_env, ROOT, probe_dir / "rustc-Zhelp.txt")
    if "compress-artifacts" not in rustc_help:
        raise RuntimeError("selected rustc does not advertise -Zcompress-artifacts; no benchmarks were run")
    cargo_version = capture_probe([str(cargo), "-Vv"], base_env, ROOT, probe_dir / "cargo-Vv.txt")
    cargo_help = capture_probe([str(cargo), "-Zhelp"], {**base_env, "RUSTC_BOOTSTRAP": "1"}, ROOT,
                               probe_dir / "cargo-Zhelp.txt")
    if "no-embed-metadata" not in cargo_help:
        raise RuntimeError("selected Cargo does not advertise -Zno-embed-metadata; no benchmarks were run")
    clang_version = None
    if clang:
        clang_version = capture_probe([clang, "--version"], base_env, ROOT,
                                      probe_dir / "clang-version.txt")
    zstd_cli = shutil.which("zstd")
    zstd_version = None
    if zstd_cli:
        result = subprocess.run([zstd_cli, "--version"], cwd=ROOT, env=base_env,
                                text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        zstd_version = result.stdout
    build_record = find_build_record(rustc)
    if not build_record:
        raise RuntimeError("no successful, source-stable build record matched this rustc launcher and rustc_driver; no benchmarks were run")
    actual_driver = {
        str(path.resolve()): sha256_file(path)
        for path in sorted(rustc.resolve().parent.parent.joinpath("lib").glob("*rustc_driver*"))
        if path.is_file()
    }

    manifest = {
        "created_utc": utc_now(), "repo_root": str(ROOT), "out": str(out),
        "harness_path": str(Path(__file__).resolve()),
        "harness_sha256": sha256_file(Path(__file__)),
        "rustc": tool_identity(rustc), "cargo": tool_identity(cargo),
        "compiler_build_record": build_record,
        "rustc_driver_libraries": actual_driver,
        "rustc_version": rustc_version, "cargo_version": cargo_version,
        "profile": args.profile, "debug_info": 0, "incremental": args.incremental, "semantic_edits": args.semantic_edits,
        "repetitions": args.repetitions, "jobs": args.jobs,
        "sampling_interval_seconds": args.sample_interval,
        "workloads": list(args.workloads), "arms": (args.arms or selected_arms(args.include_embedded_baseline)),
        "arm_flags": {a: arm_flags(a) for a in (args.arms or selected_arms(args.include_embedded_baseline))},
        "arm_definitions": {
            "dedup_off": "Cargo -Zno-embed-metadata; rustc -Zcompress-artifacts=no",
            "compress_on": "Cargo -Zno-embed-metadata; rustc -Zcompress-artifacts=yes",
            "embedded_off": "metadata embedded; rustc -Zcompress-artifacts=no",
        },
        "rustc_probe_advertises_compress_artifacts": True,
        "cargo_probe_advertises_no_embed_metadata": True,
        "removed_inherited_environment_names": removed_names,
        "cargo_home": base_env.get("CARGO_HOME", str(Path.home() / ".cargo")),
        "cargo_config_facts": config_facts,
        "native_tools": {"clang": shutil.which("clang"), "ar": shutil.which("ar")},
        "host": {
            "platform": platform.platform(), "machine": platform.machine(),
            "python_version": sys.version,
            "psutil_version": psutil.__version__,
            "clang_version": clang_version,
            "ar_path": ar,
            "zstd_library_name": ctypes.util.find_library("zstd"),
            "zstd_executable": zstd_cli, "zstd_version": zstd_version,
        },
        "source_fixture_fingerprints": {
            name: tree_fingerprint(FIXTURE if name == "native-fixture" else INPUTS / name,
                                   exclude_logs=name != "native-fixture")
            for name in args.workloads
        },
        "measurement_notes": [
            "Every arm and repetition uses a fresh copied workspace and fresh target directory.",
            "Default comparison separates metadata for both arms; compression is the only changed compiler flag.",
            "The optional embedded_off arm provides a baseline against the original embedded-metadata behavior.",
            "The edited_rebuild phase appends a comment. Optional semantic_edit_N phases change a printed black_box value and verify the new stdout.",
            "Targets and scratch trees are preserved under --out; this runner does not clean or delete them.",
            "Disk figures count logical path bytes, logical unique-inode bytes, and allocated st_blocks*512 unique-inode bytes. APFS clone extent sharing is not observable here.",
            "Sampled disk and process peaks are lower bounds. RSS sums can double-count shared pages; sampled process-tree CPU can miss short-lived processes.",
            "Wall times include the same process and disk sampling overhead in every arm.",
            "The synthetic fixture disables doctests; this benchmark harness does not validate rustdoc.",
        ],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    records = []
    arms = (args.arms or selected_arms(args.include_embedded_baseline))
    for workload_index, workload in enumerate(args.workloads):
        for repetition in range(1, args.repetitions + 1):
            offset = (repetition - 1 + workload_index) % len(arms)
            order = arms[offset:] + arms[:offset]
            for arm in order:
                record = run_arm(
                    out=out, workload=workload, profile=args.profile,
                    repetition=repetition, arm=arm, rustc=str(rustc), cargo=str(cargo),
                    base_env=base_env, removed_env=removed_names, jobs=args.jobs,
                    interval=args.sample_interval, timeout=args.timeout, incremental=args.incremental, semantic_edits=args.semantic_edits,
                )
                records.append(record)
                print(f"completed {workload}/{args.profile}/rep-{repetition:02d}/{arm}", flush=True)
    result = {"manifest": str(out / "manifest.json"),
              "completed_runs": len(records), "runs": records,
              "analysis": medians(records)}
    (out / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"out": str(out), "completed_runs": len(records),
                      "summary": str(out / "results.json")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
