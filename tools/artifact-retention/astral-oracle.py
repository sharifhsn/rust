#!/usr/bin/env python3
"""Run deterministic, offline application checks against a measured binary."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def run(argv, **kwargs):
    return subprocess.run(argv, text=True, capture_output=True, timeout=60, **kwargs)


def main():
    name, binary, expected_version = sys.argv[1:]
    version = run([binary, "--version"])
    assert version.returncode == 0 and version.stdout.strip().startswith(expected_version), version
    if name == "ruff":
        args = [binary, "check", "--isolated", "--select", "F821", "--output-format=json", "-"]
        clean = run(args, input="print(42)\n")
        assert clean.returncode == 0 and json.loads(clean.stdout) == [], clean
        invalid = run(args, input="print(missing_name)\n")
        messages = json.loads(invalid.stdout)
        assert invalid.returncode == 1 and len(messages) == 1, invalid
        assert messages[0]["code"] == "F821" and messages[0]["location"] == {"row": 1, "column": 7}, messages
        print("ruff: clean=0 undefined=F821")
    elif name == "uv":
        with tempfile.TemporaryDirectory(prefix="uv-retention-oracle-") as temporary:
            root = Path(temporary)
            result = run([binary, "--offline", "--no-config", "venv", "--no-project",
                          "--no-python-downloads", "--python", "/usr/bin/python3", str(root / "venv")])
            assert result.returncode == 0, result
            python = run([str(root / "venv/bin/python"), "-c", "print(sum([10, 12, 20]))"])
            assert python.returncode == 0 and python.stdout.strip() == "42", python
        print("uv: offline-venv python=42")
    else:
        raise ValueError(name)


if __name__ == "__main__":
    main()
