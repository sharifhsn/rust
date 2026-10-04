"""Upload benchmark receipts, excluding build trees, outside command timings."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

VOLATILE = {"status.json", "week-state.json", "week-service.stdout", "week-service.stderr"}


def receipt_files(root):
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git" and not d.startswith("target")]
        for name in files:
            path = Path(base) / name
            if not path.is_symlink() and name != "manifest.json" and not name.endswith(".tmp"):
                yield path


def manifest(root):
    result = {}
    for path in receipt_files(root):
        if path.name in VOLATILE:
            continue
        with path.open("rb") as stream:
            result[str(path.relative_to(root))] = hashlib.file_digest(stream, "sha256").hexdigest()
    tmp = root / "manifest.json.tmp"
    tmp.write_text(json.dumps(result, indent=2) + "\n")
    tmp.replace(root / "manifest.json")
    return result


def checkpoint(root, prefix=None):
    bucket = os.environ.get("BENCH_BUCKET")
    if not bucket:
        return
    root = Path(root)
    manifest(root)
    prefix = prefix or os.environ.get("BENCH_CHECKPOINT_PREFIX", "results/large")
    destination = f"s3://{bucket}/{prefix.strip('/')}/"
    # Content can change without increasing size or mtime, especially after a
    # restart. Force receipt copies and publish their checksum manifest last.
    exclusions = [value for name in sorted(VOLATILE) for pattern in [name, "*/" + name]
                  for value in ["--exclude", pattern]]
    subprocess.run(["aws", "s3", "cp", str(root), destination, "--recursive",
                    "--exclude", "*target*/*", "--exclude", "*/.git/*", "--exclude", "*.tmp",
                    "--exclude", "manifest.json",
                    *exclusions, "--only-show-errors"], check=True)
    # A writer can change Content-Length while the CLI reads a live file. Upload
    # immutable copies of status files; they remain outside the receipt manifest.
    with tempfile.TemporaryDirectory(prefix="checkpoint-status-", dir=root.parent) as temporary:
        staging = Path(temporary)
        for path in receipt_files(root):
            if path.name not in VOLATILE:
                continue
            try:
                content = path.read_bytes()
            except FileNotFoundError:
                continue
            if path.suffix == ".json":
                try:
                    json.loads(content)
                except ValueError:
                    continue
            copy = staging / path.relative_to(root)
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(content)
        subprocess.run(["aws", "s3", "cp", str(staging), destination, "--recursive",
                        "--only-show-errors"], check=True)
    subprocess.run(["aws", "s3", "cp", str(root / "manifest.json"), destination + "manifest.json",
                    "--only-show-errors"], check=True)
