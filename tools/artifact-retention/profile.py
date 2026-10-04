#!/usr/bin/env python3
"""Profile native Cargo active-artifact retention against the same binary disabled.

Use uv run python. All projects and target directories are isolated under --out.
Raw stdout/stderr, per-command metrics, inventories, and provenance are retained.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import time

from checkpoint import checkpoint


def inventory(root):
    seen = set()
    allocated = logical = files = 0
    categories = {}
    units = []
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
        for name in names:
            path = Path(base) / name
            st = path.lstat()
            key = st.st_dev, st.st_ino
            if key in seen:
                continue
            seen.add(key)
            files += 1
            logical += st.st_size
            allocated += st.st_blocks * 512
            parts = path.relative_to(root).parts
            cat = "incremental" if "incremental" in parts else "session-state" if ".cargo-active-artifacts" in parts else "other"
            categories[cat] = categories.get(cat, 0) + st.st_blocks * 512
        rel = Path(base).relative_to(root).parts
        if len(rel) >= 4 and rel[-3] == "build" and len(rel[-1]) == 16:
            units.append("/".join(rel))
    return dict(allocated=allocated, logical=logical, files=files, categories=categories, units=sorted(units))


def fixture(root, medium=False):
    root.mkdir(parents=True, exist_ok=True)
    features = "\n".join(f"v{i} = []" for i in range(8))
    deps = '\nbevy_ecs = "=0.19.0"\nsyn = { version = "=2.0.118", features = ["full", "visit"] }\nserde_json = "=1.0.150"' if medium else ""
    manifest = f'''[workspace]
[package]
name = "retention_probe"
version = "0.1.0"
edition = "2024"
[features]
{features}
parallel = ["bevy_ecs/multi_threaded"]
[dependencies]
shared = {{ path = "shared" }}
probe_macro = {{ path = "probe-macro" }}
{deps}
[profile.dev]
debug = 2
incremental = true
split-debuginfo = "unpacked"
[profile.test]
debug = 2
incremental = true
split-debuginfo = "unpacked"
'''.replace('parallel = ["bevy_ecs/multi_threaded"]', 'parallel = ["bevy_ecs/multi_threaded"]' if medium else 'parallel = []')
    (root / "Cargo.toml").write_text(manifest)
    (root / "shared/src").mkdir(parents=True)
    (root / "shared/Cargo.toml").write_text('[package]\nname="shared"\nversion="0.1.0"\nedition="2024"\n')
    (root / "shared/src/lib.rs").write_text('pub fn value() -> u64 { 42 }\n')
    (root / "probe-macro/src").mkdir(parents=True)
    (root / "probe-macro/Cargo.toml").write_text('[package]\nname="probe_macro"\nversion="0.1.0"\nedition="2024"\n[lib]\nproc-macro=true\n')
    (root / "probe-macro/src/lib.rs").write_text('extern crate proc_macro;\n#[proc_macro] pub fn identity(t: proc_macro::TokenStream) -> proc_macro::TokenStream { t }\n')
    (root / "build.rs").write_text('fn main() { let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap()); std::fs::write(out.join("value.rs"), "pub const GENERATED: u64 = 1;").unwrap(); println!("cargo:rerun-if-changed=build.rs"); }\n')
    (root / "src").mkdir()
    lib = 'include!(concat!(env!("OUT_DIR"), "/value.rs"));\n'
    lib += '/// ```\n/// assert_eq!(retention_probe::answer(), 43);\n/// ```\npub fn answer() -> u64 { let warning_probe = 1; shared::value() + GENERATED }\n'
    lib += 'probe_macro::identity! { pub fn macro_value() -> u64 { answer() } }\n'
    for i in range(24 if medium else 8):
        lib += f'pub mod m{i} {{\n' + '\n'.join(f'pub fn f{j}(x: u64) -> u64 {{ x.wrapping_add({i+j}) }}' for j in range(32)) + '\n}\n'
    if medium:
        lib += '#[derive(bevy_ecs::prelude::Component)] struct Counter(u64);\npub fn workload() -> u64 { let mut world = bevy_ecs::prelude::World::new(); for i in 0..1000 { world.spawn(Counter(i)); } let mut q = world.query::<&Counter>(); let sum: u64 = q.iter(&world).map(|v| v.0).sum(); let ast: syn::File = syn::parse_str("fn main() {}").unwrap(); assert_eq!(ast.items.len(), 1); let value: serde_json::Value = serde_json::from_str("{\\\"answer\\\":43}").unwrap(); assert_eq!(value["answer"], 43); sum }\n'
    lib += '#[test] fn oracle() { assert_eq!(answer(), 43); assert_eq!(macro_value(), 43);' + ('assert_eq!(workload(), 499500);' if medium else '') + '}\n'
    (root / "src/lib.rs").write_text(lib)
    (root / "src/main.rs").write_text('fn main() { assert_eq!(retention_probe::answer(), 43);' + ('assert_eq!(retention_probe::workload(), 499500);' if medium else '') + 'println!("oracle:43"); }\n')
    for folder in ["examples", "tests", "benches"]:
        (root / folder).mkdir()
    (root / "examples/probe.rs").write_text('fn main() { assert_eq!(retention_probe::answer(), 43); }\n')
    (root / "tests/integration.rs").write_text('#[test] fn integration() { assert_eq!(retention_probe::answer(), 43); }\n')
    (root / "benches/probe.rs").write_text('#[test] fn bench_oracle() { assert_eq!(retention_probe::answer(), 43); }\n')


class Runner:
    def __init__(self, args):
        self.args = args
        self.rows = []
        self.host = next(line.removeprefix("host: ") for line in
                         subprocess.check_output([args.toolchain / "rustc", "-vV"], text=True).splitlines()
                         if line.startswith("host: "))
        self.env = dict(os.environ)
        cargo_home = self.env.get("CARGO_HOME")
        for k in list(self.env):
            if k.startswith("CARGO_") or k in ["RUSTFLAGS", "RUSTDOCFLAGS", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTC_BOOTSTRAP"]:
                self.env.pop(k)
        if cargo_home:
            self.env["CARGO_HOME"] = cargo_home
        self.env.update(RUSTC=str(args.toolchain / "rustc"), RUSTDOC=str(args.toolchain / "rustdoc"), PATH=str(args.toolchain)+os.pathsep+self.env["PATH"], CARGO_INCREMENTAL="1", CARGO_TERM_COLOR="never", __CARGO_TEST_CHANNEL_OVERRIDE_DO_NOT_USE_THIS="nightly")

    def run(self, project, arm, label, command, features=None, session="agent", extra_env=None, expect=0, test_args=None):
        target = project / f"target-{arm}"
        env = {**self.env, "CARGO_TARGET_DIR": str(target)}
        if extra_env: env.update(extra_env)
        argv = [str(self.args.cargo), *command, "--offline", "--message-format=json"]
        if arm == "active":
            argv += ["-Zactive-artifacts", "--config", f'build.artifact-session="{session}"']
        if features: argv += ["--features", features]
        if test_args: argv += ["--", *test_args]
        before = inventory(target)
        start = time.perf_counter()
        log_id = f"{project.name}-{arm}-{len(self.rows):04d}-{label}"
        out = subprocess.run(argv, cwd=project, env=env, text=True, capture_output=True)
        seconds = time.perf_counter() - start
        (self.args.out / f"{log_id}.stdout").write_text(out.stdout)
        (self.args.out / f"{log_id}.stderr").write_text(out.stderr)
        messages = []
        for line in out.stdout.splitlines():
            try: messages.append(json.loads(line))
            except ValueError: pass
        artifacts = [v for v in messages if v.get("reason") == "compiler-artifact"]
        artifact_keys = sorted([json.dumps([v["package_id"], v["target"]["name"], v["target"]["kind"], v["profile"]["test"], v["filenames"]]) for v in artifacts])
        after = inventory(target)
        state = json.loads((target / ".cargo-active-artifacts/state.json").read_text()) if (target / ".cargo-active-artifacts/state.json").exists() else None
        row = dict(project=project.name, arm=arm, label=label, command=argv, features=features, session=session, seconds=seconds, returncode=out.returncode, artifacts=len(artifacts), fresh=sum(v["fresh"] for v in artifacts), artifact_keys=artifact_keys, before=before, after=after, state=state, log=log_id)
        self.rows.append(row)
        (self.args.out / "results.json").write_text(json.dumps(self.rows, indent=2))
        print(f"{project.name} {arm} {label}: {seconds:.3f}s {row['fresh']}/{len(artifacts)} fresh, {after['allocated']/1048576:.1f} MiB", flush=True)
        checkpoint(self.args.out)
        assert out.returncode == expect, out.stderr[-4000:]
        return row

    def execute(self):
        for medium in [False, True]:
            project = self.args.out / ("medium" if medium else "small")
            fixture(project, medium)
            if getattr(self.args, "fetch", False):
                subprocess.run([str(self.args.cargo), "fetch"], cwd=project, env=self.env, check=True)
            checkpoint(self.args.out)
            original_source = (project / "src/lib.rs").read_text()
            for arm in ["control", "active"]:
                (project / "src/lib.rs").write_text(original_source)
                for label, cmd in [("initial-check", ["check"]), ("initial-build", ["build"]), ("initial-test", ["test", "--no-run"]), ("all-targets", ["check", "--all-targets"]), ("doc", ["doc", "--no-deps"]), ("example", ["build", "--examples"])]:
                    self.run(project, arm, label, cmd)
                for repeat in range(5):
                    for cmd in [["check", "--all-targets"], ["build"], ["test", "--no-run"]]:
                        row = self.run(project, arm, f"warm-{repeat}-{'-'.join(cmd)}", cmd)
                        assert row["fresh"] == row["artifacts"]
                self.run(project, arm, "runtime-tests", ["test"])
                self.run(project, arm, "runtime-run", ["run"])
                for edit in range(12 if not medium else 4):
                    src = project / "src/lib.rs"
                    text = src.read_text()
                    src.write_text(text + f'\npub fn edit_{edit}() -> u64 {{ {edit} }}\n')
                    self.run(project, arm, f"edit-{edit}", ["build"])
                for config in range(8):
                    features = f"v{config}" + (",parallel" if config % 2 else "")
                    for cmd in [["check"], ["build"], ["test", "--no-run"]]:
                        self.run(project, arm, f"config-{config}-{'-'.join(cmd)}", cmd, features)
                self.run(project, arm, "final-oracles", ["test"], "v7,parallel")
                self.run(project, arm, "final-run", ["run"], "v7,parallel")
                self.run(project, arm, "return-config-0", ["build"], "v0")
                self.run(project, arm, "return-config-0-warm", ["build"], "v0")
                # A new check-flags configuration after a build keeps its published
                # output roots pinned, so the next full build replaces those pins.
                self.run(project, arm, "flags-switch", ["check"], "v0", extra_env={"RUSTFLAGS":"--cfg retention_flags"})
                self.run(project, arm, "flags-build", ["build"], "v0", extra_env={"RUSTFLAGS":"--cfg retention_flags"})
                self.run(project, arm, "flags-tests", ["test"], "v0", extra_env={"RUSTFLAGS":"--cfg retention_flags"})
                self.run(project, arm, "host-target", ["check", "--target", self.host], "v0")
                self.run(project, arm, "bench-no-run", ["bench", "--no-run"], "v0")
            (project / "initial-lib.rs").write_text(original_source)
        (self.args.out / "summary.json").write_text(json.dumps(summarize(self.rows), indent=2))


def summarize(rows):
    result = {}
    for project in sorted({r["project"] for r in rows}):
        result[project] = {}
        for arm in ["control", "active"]:
            relevant = [r for r in rows if r["project"] == project and r["arm"] == arm]
            warm = [r["seconds"] for r in relevant if r["label"].startswith("warm-")]
            after_variants = next(r for r in relevant if r["label"] == "final-oracles")
            result[project][arm] = dict(warm_median_s=statistics.median(warm), config_retained_bytes=after_variants["after"]["allocated"], config_units=len(after_variants["after"]["units"]), return_config=next(r for r in relevant if r["label"] == "return-config-0"), max_retained_bytes=max(r["after"]["allocated"] for r in relevant))
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cargo", type=Path, required=True)
    ap.add_argument("--toolchain", type=Path, required=True, help="bin directory with rustc/rustdoc")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--fetch", action="store_true", help="Fetch pinned fixture dependencies before timings")
    args = ap.parse_args()
    args.out = args.out.resolve()
    args.cargo = args.cargo.resolve()
    args.toolchain = args.toolchain.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    env = dict(platform=platform.platform(), machine=platform.machine(), cargo=str(args.cargo), cargo_sha256=hashlib.sha256(args.cargo.read_bytes()).hexdigest(), rustc=subprocess.check_output([args.toolchain/"rustc", "-vV"], text=True), cargo_version=subprocess.check_output([args.cargo, "-vV"], text=True), started=time.time())
    (args.out / "environment.json").write_text(json.dumps(env, indent=2))
    Runner(args).execute()

if __name__ == "__main__": main()
