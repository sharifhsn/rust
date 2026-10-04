"""Build the native prototype and record exact source/tool provenance."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BUILD = Path('/tmp/rust-compressed-artifacts-bootstrap')
HOST = 'aarch64-apple-darwin'
SOURCES = [
    'compiler/rustc_data_structures/src/artifact_compression.rs',
    'compiler/rustc_data_structures/src/lib.rs',
    'compiler/rustc_data_structures/src/memmap.rs',
    'compiler/rustc_metadata/src/fs.rs',
    'compiler/rustc_metadata/src/locator.rs',
    'compiler/rustc_metadata/src/rmeta/encoder.rs',
    'compiler/rustc_session/src/options.rs',
    'compiler/rustc_session/src/config.rs',
    'compiler/rustc_codegen_ssa/src/back/link.rs',
    'compiler/rustc_codegen_ssa/src/back/write.rs',
    'compiler/rustc_codegen_ssa/src/back/lto.rs',
    'compiler/rustc_interface/src/tests.rs',
    'compiler/rustc_codegen_ssa/src/back/metadata.rs',
    'compiler/rustc_codegen_ssa/src/back/rmeta_link.rs',
    'compiler/rustc_codegen_ssa/src/diagnostics.rs',
    'compiler/rustc_codegen_llvm/src/back/lto.rs',
    'tools/compressed-artifacts/bootstrap.toml',
]

SOURCES += [str(p.relative_to(ROOT)) for p in sorted((ROOT / "compiler/rustc_incremental").rglob("*.rs"))]


def sha(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(data)
    return result.hexdigest()


def snapshot():
    return {name: sha(ROOT / name) for name in SOURCES}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    before = snapshot()
    env = os.environ.copy()
    removed = []
    for key in list(env):
        if key in {'RUSTFLAGS', 'CARGO_ENCODED_RUSTFLAGS', 'RUSTC', 'RUSTDOC',
                   'RUSTC_WRAPPER', 'RUSTC_WORKSPACE_WRAPPER', 'CARGO_TARGET_DIR',
                   'CARGO_BUILD_TARGET', 'CARGO_BUILD_RUSTFLAGS', 'CARGO_INCREMENTAL'}:
            removed.append(key)
            del env[key]
    command = [sys.executable, str(ROOT / 'x.py'), 'build', '--config',
               str(HERE / 'bootstrap.toml'), '--stage', '1', 'compiler/rustc', 'library/std']
    record = {'started_utc': datetime.now(timezone.utc).isoformat(), 'command': command,
              'cwd': str(ROOT), 'git_head': subprocess.check_output(
                  ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'source_sha256_before': before, 'removed_environment_names': removed,
              'python': sys.version, 'log': str(out / 'build.log')}
    (out / 'build.json').write_text(json.dumps(record, indent=2) + '\n')
    patch = subprocess.check_output(['git', 'diff', '--', *SOURCES], cwd=ROOT)
    (out / 'tracked-source.patch').write_bytes(patch)
    print(f'Building native compiler; log: {out / "build.log"}', flush=True)
    start = time.monotonic()
    with (out / 'build.log').open('wb') as log:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    record.update(exit_code=result.returncode, wall_seconds=time.monotonic() - start,
                  source_sha256_after=snapshot(), finished_utc=datetime.now(timezone.utc).isoformat())
    record['sources_unchanged_during_build'] = record['source_sha256_after'] == before
    if result.returncode == 0:
        compiler = BUILD / HOST / 'stage1/bin/rustc'
        record['rustc'] = str(compiler)
        record['rustc_version'] = subprocess.check_output([compiler, '-vV'], text=True)
        record['rustc_launcher_sha256'] = sha(compiler)
        record['driver_libraries'] = {str(p): sha(p) for p in
                                      (BUILD / HOST / 'stage1/lib').glob('*rustc_driver*')}
    (out / 'build.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({k: record[k] for k in
                      ('exit_code', 'wall_seconds', 'sources_unchanged_during_build')}, indent=2))
    if result.returncode:
        print('\n'.join((out / 'build.log').read_text(errors='replace').splitlines()[-70:]))
    return result.returncode or (0 if record['sources_unchanged_during_build'] else 2)


if __name__ == '__main__':
    raise SystemExit(main())
