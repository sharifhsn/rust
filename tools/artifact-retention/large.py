#!/usr/bin/env python3
"""Paired configuration histories on pinned real workspaces, with raw receipts.

The primary control stops at the first predefined configuration boundary above
100 GiB. Active repeats exactly those configurations. Other projects provide
independent coverage. Warm comparisons interleave arms without disk scans.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import threading
import time
import urllib.request

import psutil

from profile import inventory
from checkpoint import checkpoint

GIB = 1024 ** 3
WORKLOADS = {
    "bevy": {
        "repo": "bevyengine/bevy", "revision": "b56fc29d3016e641754765244b5ba3f9cc504671",
        "probe": "examples/retention_probe.rs", "manifest": "Cargo.toml",
        "selection": ["-p", "bevy", "--example", "retention_probe"],
        "tests": ["-p", "bevy", "-p", "bevy_ecs", "--lib"],
        "wide": ["-p", "bevy", "-p", "bevy_ecs", "--lib", "--tests"],
        "features": [None, "dev", "dev,serialize"],
        "oracle": "bevy: entities=1000 updates=30 sum=559500",
    },
    "polars": {
        "repo": "pola-rs/polars", "revision": "d7488c71ecfbc77790292ff5b365b991c08380ce",
        "probe": "crates/polars/examples/retention_probe.rs", "manifest": "crates/polars/Cargo.toml",
        "selection": ["-p", "polars", "--example", "retention_probe"],
        "tests": ["-p", "polars", "--lib"],
        "wide": ["-p", "polars", "--example", "retention_probe"],
        "features": ["lazy,parquet", "lazy,parquet,dtype-struct", "lazy,parquet,strings,temporal"],
        "oracle": "polars: csv+lazy+parquet groups=2 sum=10",
    },
    "nushell": {
        "repo": "nushell/nushell", "revision": "2459fdd134ea4fdbae42efd6924e2b41201cf363",
        "probe": "src/main.rs", "manifest": None,
        "selection": ["-p", "nu", "--bin", "nu"],
        "tests": ["-p", "nu-protocol", "--lib"],
        "wide": ["-p", "nu-protocol", "--all-targets"],
        "features": [None, None, None],
        "oracle": "30", "runtime_args": ["--no-config-file", "--no-history", "-c", "[1 2 3 4] | each {|x| $x * $x } | math sum"],
    },
}


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2))
    tmp.replace(path)


def configurations(workload):
    # Deliberate, disclosed stress trace of real code-generation choices. A short
    # ordinary feature/command history is reported separately (first two entries).
    features = workload["features"]
    return [
        dict(name="default", features=features[0], flags="", opt="0", assertions="true"),
        dict(name="development-features", features=features[1], flags="", opt="0", assertions="true"),
        dict(name="portable-v2", features=features[1], flags="-Ctarget-cpu=x86-64-v2", opt="0", assertions="true"),
        dict(name="optimized-dev", features=features[1], flags="-Ctarget-cpu=x86-64-v2", opt="1", assertions="true"),
        dict(name="native-dev", features=features[2], flags="-Ctarget-cpu=native", opt="1", assertions="true"),
        dict(name="native-target", features=features[2], flags="-Ctarget-cpu=native", opt="1", assertions="true", target="x86_64-unknown-linux-gnu"),
        dict(name="frame-pointers", features=features[1], flags="-Cforce-frame-pointers=yes", opt="0", assertions="true"),
        dict(name="unchecked-dev", features=features[1], flags="-Cforce-frame-pointers=yes", opt="1", assertions="false"),
        dict(name="portable-v3", features=features[0], flags="-Ctarget-cpu=x86-64-v3", opt="0", assertions="true"),
        dict(name="optimized-v3", features=features[0], flags="-Ctarget-cpu=x86-64-v3", opt="1", assertions="true"),
        dict(name="native-frame-pointers", features=features[2], flags="-Ctarget-cpu=native -Cforce-frame-pointers=yes", opt="0", assertions="true"),
        dict(name="optimized-native-frame-pointers", features=features[2], flags="-Ctarget-cpu=native -Cforce-frame-pointers=yes", opt="1", assertions="true"),
        dict(name="eight-codegen-units", features=features[0], flags="", opt="0", assertions="true", units="8"),
        dict(name="thirty-two-codegen-units", features=features[1], flags="", opt="0", assertions="true", units="32"),
    ]


class Experiment:
    def __init__(self, args):
        self.args = args
        args.resume = getattr(args, "resume", False)
        self.rows = []
        self.identities = {}
        if args.resume:
            self.rows = json.loads((args.out / "results.json").read_text())
            self.identities = json.loads((args.out / "source-identities.json").read_text())
            summary_path = args.out / "summary.json"
            completed = set(json.loads(summary_path.read_text())) if summary_path.exists() else set()
            for row in self.rows:
                if row["project"] not in completed and not self.warm_ready(row["project"]) and row["label"].startswith("warm-"):
                    row["warmup_excluded"] = "incomplete QA attempt; warm sampling restarted after preparation"
        self.env = dict(os.environ)
        cargo_home = self.env.get("CARGO_HOME")
        for key in list(self.env):
            if key.startswith("CARGO_") or key in {"RUSTFLAGS", "RUSTDOCFLAGS", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTC_BOOTSTRAP"}:
                self.env.pop(key)
        if cargo_home:
            self.env["CARGO_HOME"] = cargo_home
        self.env.update(RUSTC=str(args.toolchain / "rustc"), RUSTDOC=str(args.toolchain / "rustdoc"),
                        PATH=str(args.toolchain) + os.pathsep + self.env["PATH"],
                        CARGO_TERM_COLOR="never", CARGO_INCREMENTAL="1",
                        RUST_BACKTRACE="1",
                        CARGO_PROFILE_DEV_DEBUG="2", CARGO_PROFILE_TEST_DEBUG="2",
                        CARGO_PROFILE_DEV_INCREMENTAL="true", CARGO_PROFILE_TEST_INCREMENTAL="true",
                        CARGO_PROFILE_DEV_SPLIT_DEBUGINFO="off", CARGO_PROFILE_TEST_SPLIT_DEBUGINFO="off",
                        CARGO_PROFILE_DEV_CODEGEN_UNITS="16", CARGO_PROFILE_TEST_CODEGEN_UNITS="16",
                        __CARGO_TEST_CHANNEL_OVERRIDE_DO_NOT_USE_THIS="nightly")
        args.out.mkdir(parents=True, exist_ok=True)

    def warm_ready(self, name):
        return all(len([r for r in self.rows if r["project"] == name and r["arm"] == arm
                        and r["label"].startswith("warm-") and r["label"].endswith("-" + mode)
                        and not r.get("warmup_excluded") and r["returncode"] == 0
                        and r["artifacts"] > 0 and r["fresh"] == r["artifacts"]]) >= 5
                   for arm in ["control", "active"] for mode in ["check", "build", "test", "clippy"])

    def status(self, value):
        atomic_json(self.args.out / "status.json", value)
        bucket = os.environ.get("BENCH_BUCKET")
        if bucket:
            subprocess.run(["aws", "s3", "cp", str(self.args.out / "status.json"),
                            f"s3://{bucket}/status.json", "--only-show-errors"], check=True)
            if value["phase"] in {"boundary", "complete"}:
                self.checkpoint()

    def checkpoint(self):
        # Outside command timings. Preserve failed commands and QA samples too:
        # a Spot reclaim can occur before the next configuration boundary.
        checkpoint(self.args.out)

    def prepare(self, name):
        import tarfile
        config = WORKLOADS[name]
        root = self.args.sources / name
        root.mkdir(parents=True, exist_ok=True)
        archive = self.args.sources / f"{name}.tar.gz"
        if not archive.exists():
            urllib.request.urlretrieve(f"https://codeload.github.com/{config['repo']}/tar.gz/{config['revision']}", archive)
        with tarfile.open(archive) as tf:
            # Public, source-pinned archive; strip its single root directory.
            for entry in tf.getmembers():
                parts = Path(entry.name).parts[1:]
                if not parts:
                    continue
                entry.name = str(Path(*parts))
                tf.extract(entry, root, filter="data")
        lock = self.args.sources / f"{name}.Cargo.lock"
        if lock.exists():
            shutil.copyfile(lock, root / "Cargo.lock")
        if config["manifest"]:
            probe = root / config["probe"]
            probe.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.args.sources / f"{name}.rs", probe)
            with (root / config["manifest"]).open("a") as stream:
                relative = Path(config["probe"]).relative_to(Path(config["manifest"]).parent)
                stream.write(f'\n[[example]]\nname = "retention_probe"\npath = "{relative}"\n')
        self.original = (root / config["probe"]).read_text()
        original = self.args.out / "source-originals" / f"{name}.rs"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_text(self.original)
        resolve = getattr(self.args, "resolve_locks", False)
        if resolve:
            shutil.copyfile(root / "Cargo.lock", self.args.out / f"{name}-Cargo.lock.before")
        subprocess.run([str(self.args.cargo), "fetch", *([] if resolve else ["--locked"])], cwd=root, env=self.env,
                       stdout=(self.args.out / f"{name}-fetch.stdout").open("w"),
                       stderr=(self.args.out / f"{name}-fetch.stderr").open("w"), check=True)
        shutil.copyfile(root / "Cargo.lock", self.args.out / f"{name}-Cargo.lock")
        if resolve:
            shutil.copyfile(root / "Cargo.lock", lock)
        identity = dict(revision=config["revision"], archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                        lock_sha256=hashlib.sha256((root / "Cargo.lock").read_bytes()).hexdigest(),
                        probe_sha256=hashlib.sha256(self.original.encode()).hexdigest())
        self.identities[name] = identity
        atomic_json(self.args.out / "source-identities.json", self.identities)
        return root

    def edit(self, root, config, index):
        # Same source content at the corresponding boundary in both arms.
        path = root / config["probe"]
        content = self.original + f"\npub fn retention_edit_{index}() -> u64 {{ {index} }}\n"
        if path.read_text() != content:
            path.write_text(content)

    def measure(self, name, root, arm, cfg, label, command, census=True, expected=0, runtime=False):
        target = self.args.sources / "targets" / arm / name
        target.mkdir(parents=True, exist_ok=True)
        env = {**self.env, "CARGO_TARGET_DIR": str(target),
               "RUSTFLAGS": "-Clink-arg=-fuse-ld=lld " + cfg["flags"],
               "CARGO_PROFILE_DEV_OPT_LEVEL": cfg["opt"], "CARGO_PROFILE_TEST_OPT_LEVEL": cfg["opt"],
               "CARGO_PROFILE_DEV_DEBUG_ASSERTIONS": cfg["assertions"], "CARGO_PROFILE_TEST_DEBUG_ASSERTIONS": cfg["assertions"]}
        env.update(CARGO_PROFILE_DEV_CODEGEN_UNITS=cfg.get("units", "16"),
                   CARGO_PROFILE_TEST_CODEGEN_UNITS=cfg.get("units", "16"))
        if runtime:
            argv = command
        else:
            split = command.index("--") if "--" in command else len(command)
            argv = [str(self.args.cargo), *command[:split], "--locked", "--offline", "--message-format=json", f"-j{self.args.jobs}"]
            if arm == "active":
                argv += ["-Zactive-artifacts", "--config", f'build.artifact-session="{name}-agent"']
            if cfg["features"]:
                argv += ["--features", cfg["features"]]
            if cfg.get("target"):
                argv += ["--target", cfg["target"]]
            argv += command[split:]
        key = f"{len(self.rows):04d}-{name}-{arm}-{cfg['name']}-{label}"
        log = self.args.out / "commands" / key
        log.mkdir(parents=True)
        self.status(dict(phase="running", project=name, arm=arm, configuration=cfg["name"], command=label, completed=len(self.rows)))
        stop = threading.Event()
        samples = []
        disk_samples = []
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        start = time.perf_counter()
        with (log / "stdout").open("w") as stdout, (log / "stderr").open("w") as stderr:
            child = subprocess.Popen(argv, cwd=root, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
            def monitor():
                process = psutil.Process(child.pid)
                while not stop.is_set():
                    rss = 0
                    fds = 0
                    try:
                        procs = [process, *process.children(recursive=True)]
                        for item in procs:
                            try:
                                rss += item.memory_info().rss
                                fds += item.num_fds()
                            except psutil.Error:
                                pass
                    except psutil.Error:
                        pass
                    samples.append([time.perf_counter() - start, rss, fds])
                    stop.wait(0.1)
            watcher = threading.Thread(target=monitor)
            watcher.start()
            def sample_disk():
                while not stop.is_set():
                    try:
                        size = inventory(target)["allocated"]
                        disk_samples.append([time.perf_counter() - start, size])
                    except FileNotFoundError:
                        pass
                    stop.wait(5)
            disk_watcher = None
            if name == "bevy" and label == "build" and cfg["name"] in {"default", "frame-pointers"}:
                disk_watcher = threading.Thread(target=sample_disk)
                disk_watcher.start()
            try:
                code = child.wait(timeout=2400)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                raise
            finally:
                stop.set()
                watcher.join()
                if disk_watcher:
                    disk_watcher.join()
        wall = time.perf_counter() - start
        after_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        messages = []
        for line in (log / "stdout").read_text().splitlines():
            try:
                messages.append(json.loads(line))
            except ValueError:
                pass
        artifacts = [m for m in messages if isinstance(m, dict) and m.get("reason") == "compiler-artifact"]
        row = dict(project=name, arm=arm, configuration=cfg, label=label, command=argv,
                   seconds=wall, returncode=code, fresh=sum(m["fresh"] for m in artifacts), artifacts=len(artifacts),
                   user_seconds=after_usage.ru_utime - usage.ru_utime, system_seconds=after_usage.ru_stime - usage.ru_stime,
                   sampled_tree_rss_peak=max((s[1] for s in samples), default=0), log=key)
        row["sampled_tree_fds_peak"] = max((s[2] for s in samples), default=0)
        if census:
            row["inventory"] = inventory(target)
        state_path = target / ".cargo-active-artifacts/state.json"
        if state_path.exists():
            row["state"] = json.loads(state_path.read_text())
        atomic_json(log / "result.json", row)
        atomic_json(log / "rss-samples.json", samples)
        if disk_samples:
            row["sampled_disk_peak"] = max(s[1] for s in disk_samples)
            row["disk_sampling_overhead_included"] = True
            atomic_json(log / "disk-samples.json", disk_samples)
        if runtime:
            digest = hashlib.sha256()
            with Path(argv[0]).open("rb") as stream:
                for block in iter(lambda: stream.read(1024*1024), b""):
                    digest.update(block)
            row["binary_sha256"] = digest.hexdigest()
        atomic_json(log / "result.json", row)
        self.rows.append(row)
        atomic_json(self.args.out / "results.json", self.rows)
        print(f"{key} {wall:.2f}s {row['fresh']}/{row['artifacts']} fresh "
              f"{row.get('inventory',{}).get('allocated',0)/GIB:.3f} GiB", flush=True)
        self.checkpoint()
        assert code == expected, (key, (log / "stderr").read_text()[-2000:])
        if runtime:
            assert (log / "stdout").read_text().strip() == WORKLOADS[name]["oracle"], key
        return row

    def oracle(self, name, root, arm, cfg):
        target = self.args.sources / "targets" / arm / name
        if cfg.get("target"):
            target /= cfg["target"]
        binary = target / "debug" / ("nu" if name == "nushell" else "examples/retention_probe")
        self.measure(name, root, arm, cfg, "runtime-oracle", [str(binary), *WORKLOADS[name].get("runtime_args", [])],
                     census=False, runtime=True)

    def history(self, name, root, arm, trace, discover=False):
        config = WORKLOADS[name]
        used = []
        for index, cfg in enumerate(trace):
            self.edit(root, config, index)
            for mode, selection in [("check", config["selection"]), ("build", config["selection"]), ("test", config["tests"])]:
                cmd = [mode, *selection]
                if mode == "test":
                    cmd += ["--no-run"]
                self.measure(name, root, arm, cfg, mode, cmd)
            self.oracle(name, root, arm, cfg)
            used.append(cfg)
            self.status(dict(phase="boundary", project=name, arm=arm, configurations=len(used),
                             allocated=inventory(self.args.sources / "targets" / arm / name)["allocated"]))
            if discover and self.rows[-2]["inventory"]["allocated"] >= 100 * GIB:
                break
        return used

    def file_receipt(self, name, arm):
        root = self.args.sources / "targets" / arm / name
        with gzip.open(self.args.out / f"{name}-{arm}-history-files.jsonl.gz", "wt") as stream:
            for base, _, files in os.walk(root):
                for name in files:
                    path = Path(base) / name
                    st = path.lstat()
                    stream.write(json.dumps(dict(path=str(path.relative_to(root)), device=st.st_dev, inode=st.st_ino,
                                                 logical=st.st_size, allocated=st.st_blocks*512)) + "\n")
        self.checkpoint()

    def run(self):
        summary_path = self.args.out / "summary.json"
        summary = json.loads(summary_path.read_text()) if self.args.resume and summary_path.exists() else {}
        for name in getattr(self.args, "projects", WORKLOADS):
            if name in summary:
                continue
            trace = configurations(WORKLOADS[name])
            if name != "bevy":
                trace = [trace[0], trace[2], trace[6]]
            trace_path = self.args.out / f"{name}-trace.json"
            used = json.loads(trace_path.read_text()) if self.args.resume and trace_path.exists() else trace
            complete_histories = self.args.resume and all(
                any(r["project"] == name and r["arm"] == arm and r["configuration"] == cfg
                    and r["label"] == mode and r["returncode"] == 0 for r in self.rows)
                for arm in ["control", "active"] for cfg in used for mode in ["check", "build", "test"])
            if complete_histories:
                root = self.args.sources / name
                original = self.args.out / "source-originals" / f"{name}.rs"
                self.original = (original if original.exists() else self.args.sources / f"{name}.rs").read_text()
                control, active = [next(r["inventory"] for r in reversed(self.rows)
                                       if r["project"] == name and r["arm"] == arm and r["label"] == "test"
                                       and r["configuration"] == used[-1]) for arm in ["control", "active"]]
            else:
                if self.args.resume and any(r["project"] == name and r["label"] in {"check", "build", "test"} for r in self.rows):
                    raise ValueError(f"{name}: preserve the incomplete history and use isolated fresh targets before rerunning")
                root = self.prepare(name)
                used = self.history(name, root, "control", trace, discover=name == "bevy")
                control = self.rows[-2]["inventory"]
                self.file_receipt(name, "control")
                atomic_json(trace_path, used)
                self.history(name, root, "active", used)
                active = self.rows[-2]["inventory"]
                self.file_receipt(name, "active")
            cfg = used[-1]
            self.edit(root, WORKLOADS[name], len(used)-1)
            if not self.warm_ready(name):
                # Establish widened check and wrapper slots before timed repetitions.
                for arm in ["control", "active"]:
                    for mode in ["check", "clippy"]:
                        self.measure(name, root, arm, cfg, "wide-" + mode, [mode, *WORKLOADS[name]["wide"]])
                    self.oracle(name, root, arm, cfg)
                # Quiesce every operation after source edits and graph widening.
                # A second cycle proves preparation before five timed repetitions.
                for cycle in range(2):
                    for arm in ["control", "active"]:
                        for mode, selection in [("check", WORKLOADS[name]["wide"]), ("build", WORKLOADS[name]["selection"]),
                                                ("test", WORKLOADS[name]["tests"]), ("clippy", WORKLOADS[name]["wide"])]:
                            row = self.measure(name, root, arm, cfg, f"prepare-{cycle}-{mode}",
                                               [mode, *selection, *(["--no-run"] if mode == "test" else [])], census=False)
                            if cycle == 1:
                                assert row["artifacts"] > 0 and row["fresh"] == row["artifacts"], row["log"]
                for repeat in range(5):
                    for arm in (["control", "active"] if repeat % 2 == 0 else ["active", "control"]):
                        for mode, selection in [("check", WORKLOADS[name]["wide"]), ("build", WORKLOADS[name]["selection"]),
                                                ("test", WORKLOADS[name]["tests"]), ("clippy", WORKLOADS[name]["wide"])]:
                            cmd = [mode, *selection, *(["--no-run"] if mode == "test" else [])]
                            row = self.measure(name, root, arm, cfg, f"warm-{repeat}-{mode}", cmd, census=False)
                            assert row["artifacts"] > 0 and row["fresh"] == row["artifacts"], row["log"]
            returned = all(any(r["project"] == name and r["arm"] == arm and r["label"] == "switch-back-warm"
                               and r["returncode"] == 0 and r["artifacts"] > 0 and r["fresh"] == r["artifacts"] for r in self.rows)
                           for arm in ["control", "active"])
            if not returned:
                # Measure return before any failed attempt can warm the evicted graph.
                for arm in ["control", "active"]:
                    self.measure(name, root, arm, used[0], "switch-back", ["build", *WORKLOADS[name]["selection"]])
                    self.oracle(name, root, arm, used[0])
                    row = self.measure(name, root, arm, used[0], "switch-back-warm", ["build", *WORKLOADS[name]["selection"]], census=False)
                    assert row["artifacts"] > 0 and row["fresh"] == row["artifacts"], row["log"]
            # Upstream tests use the supported default assertion configuration.
            for arm in ["control", "active"]:
                cmd = ["test", *WORKLOADS[name]["tests"]]
                if name == "bevy":
                    # The pinned suite asserts exact backtrace frame strings that
                    # differ with this compiler/profile. Keep the full baseline
                    # failure visible in both arms, then exercise all other tests.
                    row = self.measure(name, root, arm, used[0], "upstream-backtrace-baseline", cmd, expected=101)
                    text = (self.args.out / "commands" / row["log"] / "stdout").read_text()
                    failure = text[text.rfind("\nfailures:\n"):].split("\n\n", 1)[0].strip()
                    assert failure == "failures:\n    error::bevy_error::tests::filtered_backtrace_test", failure
                    cmd += ["--", "--skip", "error::bevy_error::tests::filtered_backtrace_test"]
                self.measure(name, root, arm, used[0], "actual-tests", cmd)
                self.oracle(name, root, arm, used[0])
            # A failed new configuration must preserve the successful session roots.
            saved = (root / WORKLOADS[name]["probe"]).read_text()
            target = self.args.sources / "targets/active" / name
            before = json.loads((target / ".cargo-active-artifacts/state.json").read_text())["sessions"]
            (root / WORKLOADS[name]["probe"]).write_text(saved + "\npub fn retention_failure() { deliberately_missing_symbol(); }\n")
            try:
                self.measure(name, root, "active", cfg, "expected-compile-failure",
                             ["check", *WORKLOADS[name]["selection"]], expected=101)
                after = json.loads((target / ".cargo-active-artifacts/state.json").read_text())["sessions"]
                assert before == after, name
                for session in before.values():
                    for graph in session["slots"].values():
                        assert all((target / unit).exists() for unit in graph)
            finally:
                (root / WORKLOADS[name]["probe"]).write_text(saved)
            self.measure(name, root, "active", used[0], "failure-recovery", ["build", *WORKLOADS[name]["selection"]])
            self.oracle(name, root, "active", used[0])
            row = self.measure(name, root, "active", used[0], "failure-recovery-warm", ["build", *WORKLOADS[name]["selection"]], census=False)
            assert row["artifacts"] > 0 and row["fresh"] == row["artifacts"], row["log"]
            summary[name] = dict(configurations=len(used), control_bytes=control["allocated"], active_bytes=active["allocated"],
                                 saved_fraction=1 - active["allocated"]/control["allocated"],
                                 reached_100_gib=control["allocated"] >= 100*GIB)
            atomic_json(self.args.out / "summary.json", summary)
            # Only our isolated completed targets: receipts and sources survive.
            # This keeps the next large workload's transition headroom available.
            for arm in ["control", "active"]:
                shutil.rmtree(self.args.sources / "targets" / arm / name)
        self.status(dict(phase="complete", summary=summary, commands=len(self.rows)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["cargo", "toolchain", "sources", "out"]:
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--resume", action="store_true", help="Resume completed paired histories and unfinished QA phases")
    parser.add_argument("--projects", nargs="+", choices=list(WORKLOADS), default=list(WORKLOADS))
    parser.add_argument("--resolve-locks", action="store_true", help="Resolve locks before measuring and preserve before/after copies")
    args = parser.parse_args()
    deadline = Path("/opt/benchmark/deadline-epoch")
    if deadline.exists():
        def deadline_reached(signum, frame):
            # Leave ten minutes for manifest upload before cloud termination.
            children = psutil.Process().children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            _, alive = psutil.wait_procs(children, timeout=10)
            for child in alive:
                try:
                    child.kill()
                except psutil.Error:
                    pass
            raise TimeoutError("benchmark receipt collection deadline reached")
        signal.signal(signal.SIGALRM, deadline_reached)
        signal.alarm(max(1, int(deadline.read_text()) - int(time.time()) - 600))
    atomic_json(args.out / "driver-identity.json", dict(
        sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
    Experiment(args).run()


if __name__ == "__main__":
    main()
