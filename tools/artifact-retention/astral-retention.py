#!/usr/bin/env python3
"""Apply the pinned retention driver to Ruff and uv."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import large

WORKLOADS = {
    "ruff": {
        "repo": "astral-sh/ruff", "revision": "1df6db3e463ffa1b587dcf47f25360d40389b0f7",
        "probe": "crates/ruff/src/main.rs", "manifest": None,
        "selection": ["-p", "ruff", "--bin", "ruff"],
        "tests": ["-p", "ruff", "--lib"],
        "wide": ["-p", "ruff", "--bin", "ruff"],
        "features": [None, None, None],
        "oracle": "ruff: clean=0 undefined=F821", "version": "ruff 0.16.10",
    },
    "uv": {
        "repo": "astral-sh/uv", "revision": "46b84fd0bfec23b72f29e8e2185ba68a65052f48",
        "probe": "crates/uv/src/bin/uv.rs", "manifest": None,
        "selection": ["-p", "uv", "--bin", "uv"],
        "tests": ["-p", "uv", "--lib"],
        "wide": ["-p", "uv", "--bin", "uv"],
        "features": [None, None, None],
        "oracle": "uv: offline-venv python=42", "version": "uv 0.12.23",
    },
}


class Experiment(large.Experiment):
    def status(self, value):
        large.atomic_json(self.args.out / "status.json", value)
        bucket = os.environ.get("BENCH_BUCKET")
        if bucket:
            subprocess.run(["aws", "s3", "cp", str(self.args.out / "status.json"),
                            f"s3://{bucket}/status.json", "--only-show-errors"], check=True)

    def checkpoint(self):
        # The final publisher uploads all receipts once and checks readback.
        # Command receipts remain durable on EBS throughout this finite run.
        return

    def measure(self, name, root, arm, cfg, label, command, **kwargs):
        cargo = self.args.cargo
        try:
            if label == "actual-tests":
                self.args.cargo = Path(__file__).with_name("astral-test-cargo.sh")
            row = super().measure(name, root, arm, cfg, label, command, **kwargs)
        finally:
            self.args.cargo = cargo
        if label == "actual-tests":
            row["test_environment"] = {"RUST_BACKTRACE": "0",
                                       "disabled_linux_capabilities": ["dac_override", "dac_read_search"]}
            large.atomic_json(self.args.out / "commands" / row["log"] / "result.json", row)
            large.atomic_json(self.args.out / "results.json", self.rows)
        return row

    def oracle(self, name, root, arm, cfg):
        binary = self.args.sources / "targets" / arm / name / "debug" / name
        row = self.measure(name, root, arm, cfg, "runtime-oracle",
                           [sys.executable, str(Path(__file__).with_name("astral-oracle.py")),
                            name, str(binary), WORKLOADS[name]["version"]],
                           census=False, runtime=True)
        row["runtime_binary_sha256"] = hashlib.file_digest(binary.open("rb"), "sha256").hexdigest()
        large.atomic_json(self.args.out / "commands" / row["log"] / "result.json", row)
        large.atomic_json(self.args.out / "results.json", self.rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cargo", type=Path, required=True)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    args.projects = list(WORKLOADS)
    args.resolve_locks = False
    assert args.resume or not args.out.exists(), "Preserve an earlier attempt and choose a fresh result path"
    large.WORKLOADS = WORKLOADS
    Experiment(args).run()


if __name__ == "__main__":
    main()
