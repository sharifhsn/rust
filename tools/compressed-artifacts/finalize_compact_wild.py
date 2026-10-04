"""Validate quiescent store GC and optional final-executable stripping.

This operates only on the disposable final Bevy experiment in this container.
It leaves the measured target's executable unstripped.
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import psutil

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "docs/target-directory-size/measurements/2026-09-27-compact-wild/final"
BASE = Path("/tmp/compact-bench-final")


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def main():
    bench = module("bench", "benchmark.py")
    gc = module("gc", "compact_store.py")
    target, scratch = BASE / "compact/target", BASE / "compact/tmp"
    active = []
    for process in psutil.process_iter(["pid", "cmdline", "status"]):
        if process.info["status"] != psutil.STATUS_ZOMBIE and any(
            str(BASE) in arg for arg in process.info["cmdline"] or []
        ):
            active.append(process.info["pid"])
    assert not active, f"experiment processes are still active: {active}"
    summary = json.loads((OUT / "bevy-summary.json").read_text())
    assert len(summary["arms"]) == 3
    assert len(json.loads((OUT / "bevy-confirmation.json").read_text())) == 24
    report = {"script_sha256": bench.sha256_file(Path(__file__)),
              "before_gc": bench.census([target, scratch])}
    pack = "/tmp/wild-target/release/wild-pack"
    report["gc_preview"] = gc.collect(target, target / "compact-store", pack)
    report["gc_applied"] = gc.collect(target, target / "compact-store", pack, True)
    report["after_gc"] = bench.census([target, scratch])
    measurement = json.loads((OUT / "bevy/compact/edit3.json").read_text())
    env = os.environ.copy()
    for key, value in measurement["environment_overrides"].items():
        env[key] = "\x1f".join(value) if isinstance(value, list) else str(value)
    assert env["CARGO_TARGET_DIR"] == str(target)
    assert measurement["cwd"] == str(BASE / "bevy")
    assert (BASE / "bevy/examples/compression_probe.rs").read_bytes() == (
        HERE / "large-fixtures/bevy.rs").read_bytes()
    for phase in ("gc-rebuild", "gc-noop"):
        stdout, stderr = OUT / f"{phase}-stdout.log", OUT / f"{phase}-stderr.log"
        with stdout.open("w") as out, stderr.open("w") as err:
            p = subprocess.run(measurement["command"], cwd=measurement["cwd"], env=env,
                               stdout=out, stderr=err)
        assert p.returncode == 0, stderr
        invocations = bench.parse_cargo_rustc_invocations(stderr, stdout)
        assert len(invocations) == (1 if phase == "gc-rebuild" else 0)
        report[phase] = {"exit": p.returncode, "rustc_invocations": len(invocations)}
    binary = target / "debug/examples/compression_probe"
    expected = "bevy: entities=1000 updates=30 sum=559500\n"
    p = subprocess.run([binary], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout == expected
    report["runtime_after_gc"] = {"exit": p.returncode, "stdout": p.stdout}
    report["after_rebuild"] = bench.census([target, scratch])
    temp = Path(tempfile.mkdtemp(prefix="strip-", dir="/tmp/compact-lab"))
    stripped = temp / "compression_probe"
    command = ["objcopy", "--strip-all", str(binary), str(stripped)]
    subprocess.run(command, check=True)
    p = subprocess.run([stripped], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout == expected
    report["optional_stripping"] = {
        "command": command,
        "original_logical_bytes": binary.stat().st_size,
        "stripped_logical_bytes": stripped.stat().st_size,
        "original_allocated_bytes": binary.stat().st_blocks * 512,
        "stripped_allocated_bytes": stripped.stat().st_blocks * 512,
        "original_sha256": bench.sha256_file(binary),
        "stripped_sha256": bench.sha256_file(stripped),
        "runtime": {"exit": p.returncode, "stdout": p.stdout},
        "tradeoff": "Static symbol names are removed; the measured target remains unstripped.",
        "temporary_copy_removed": True,
    }
    shutil.rmtree(temp)
    (OUT / "gc-and-strip.json").write_text(json.dumps(report, indent=2) + "\n")
    saved = OUT / "source/rust/tools/compressed-artifacts/finalize_compact_wild.py"
    saved.write_bytes(Path(__file__).read_bytes())
    print(json.dumps({"gc": report["gc_applied"], "strip": report["optional_stripping"]}, indent=2))


if __name__ == "__main__":
    main()
