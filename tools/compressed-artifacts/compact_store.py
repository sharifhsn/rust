"""Mark/sweep a compact store inside ONE quiescent target directory.

Default is a read-only report. --delete-quiescent requires that all compiler,
Cargo and linker processes using this target have finished. Online GC needs a
producer/GC lease protocol and is deliberately not implemented here.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess


def collect(target, store, pack, delete=False):
    target, store = target.resolve(strict=True), store.resolve(strict=True)
    assert store.is_relative_to(target) and store != target, 'store must belong to this target'
    reachable = set()
    manifests = 0
    for current, dirs, files in os.walk(target, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink()]
        for name in files:
            path = Path(current) / name
            if not name.endswith('.rlib') or path.is_symlink():
                continue
            with path.open('rb') as f:
                magic = f.read(8)
            if magic != b'RCLIB001':
                continue
            # Rust reader validates the complete manifest checksum and structure.
            output = subprocess.check_output([pack, 'references', path])
            refs = [Path(os.fsdecode(p)).resolve(strict=True) for p in output.split(b'\0') if p]
            reachable.update(refs)
            manifests += 1
    candidates = []
    retained_links = 0
    for path in store.iterdir():
        if path.is_symlink() or not re.fullmatch(r'[a-f0-9]{64}\.rco', path.name):
            continue
        stat = path.stat()
        if stat.st_nlink > 1:
            retained_links += 1
            continue
        if path in reachable:
            continue
        candidates.append((path, stat))
    report = {'manifests': manifests, 'referenced_objects': len(reachable),
              'objects_with_other_hardlinks': retained_links,
              'orphan_objects': len(candidates),
              'orphan_logical_bytes': sum(s.st_size for _, s in candidates),
              'orphan_allocated_bytes': sum(s.st_blocks * 512 for _, s in candidates),
              'deleted': delete}
    if delete:
        for path, before in candidates:
            after = path.stat()
            assert (after.st_ino, after.st_nlink, after.st_mtime_ns, after.st_size) == (
                before.st_ino, 1, before.st_mtime_ns, before.st_size), 'store changed during GC'
            path.unlink()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', type=Path)
    parser.add_argument('store', type=Path)
    parser.add_argument('--pack', default='/tmp/wild-target/release/wild-pack')
    parser.add_argument('--delete-quiescent', action='store_true')
    args = parser.parse_args()
    print(json.dumps(collect(args.target, args.store, args.pack, args.delete_quiescent), indent=2))
