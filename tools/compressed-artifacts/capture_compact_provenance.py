"""Record exact public source/binary inputs for this two-repository experiment."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

OUT = Path(os.environ.get('COMPACT_LAB_OUTPUT',
    '/work/rust/docs/target-directory-size/measurements/2026-09-27-compact-wild'))
OUT.mkdir(parents=True, exist_ok=True)


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


record = {}
for name in ('rust', 'wild'):
    root = Path('/work') / name
    files = subprocess.check_output(['git', '-C', root, 'diff', '--name-only'], text=True).splitlines()
    if name == 'rust':
        files = [p for p in files if not p.startswith('compiler/rustc_codegen_cranelift/')]
        files += ['compiler/rustc_data_structures/src/artifact_compression.rs',
                  'compiler/rustc_data_structures/src/indexed_artifact.rs',
                  'tools/compressed-artifacts/bootstrap-linux.toml',
                  *['tools/compressed-artifacts/' + p for p in (
                      'benchmark_compact_wild.py', 'benchmark.py',
                      'summarize_compact_wild.py', 'test_compact_rust.py',
                      'test_compact_wild.py', 'compact_store.py',
                      'capture_compact_provenance.py', 'large-fixtures/bevy.rs')]]
    else:
        files += [str(p.relative_to(root)) for p in (root / 'compact-artifact').rglob('*') if p.is_file()]
    files = sorted(set(files))
    record[name] = {'head': subprocess.check_output(['git', '-C', root, 'rev-parse', 'HEAD'], text=True).strip(),
                    'source_sha256': {p: sha(root / p) for p in files}}
    (OUT / f'{name}-source.patch').write_bytes(subprocess.check_output(['git', '-C', root, 'diff', '--', *files]))
    for p in files:
        dest = OUT / 'source' / name / p
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((root / p).read_bytes())
rustc = Path('/tmp/rust-compact-bootstrap/aarch64-unknown-linux-gnu/stage1/bin/rustc')
record['rustc_version'] = subprocess.check_output([rustc, '-vV'], text=True)
record['binary_sha256'] = {str(p): sha(p) for p in [rustc,
    *list((rustc.parent.parent / 'lib').glob('*rustc_driver*')),
    Path('/tmp/wild-target/release/wild'), Path('/tmp/wild-target/release/wild-pack')]}
record['uname'] = subprocess.check_output(['uname', '-a'], text=True).strip()
(OUT / 'provenance.json').write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps({k: v['head'] for k, v in record.items() if k in ('rust', 'wild')}, indent=2))
