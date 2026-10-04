"""Controlled Linux Bevy comparison: ordinary, whole-archive, direct compact inputs.

One fresh build per arm and three semantic edits; report individual samples.
Targets, shared object stores, and controlled scratch are all counted by inode.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import resource

HERE = Path(__file__).resolve().parent
OUT = Path(os.environ.get('COMPACT_LAB_OUTPUT',
    HERE.parents[1] / 'docs/target-directory-size/measurements/2026-09-27-compact-wild'))
BASE = Path(os.environ.get('COMPACT_BENCH_BASE', '/tmp/compact-bench'))
RUSTC = Path('/tmp/rust-compact-bootstrap/aarch64-unknown-linux-gnu/stage1/bin/rustc')
WILD = Path('/tmp/wild-target/release/wild')
CARGO = Path('/tmp/rust-compact-bootstrap/aarch64-unknown-linux-gnu/stage0/bin/cargo')
spec = importlib.util.spec_from_file_location('bench', HERE / 'benchmark.py')
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def main():
    BASE.mkdir(exist_ok=True)
    source = BASE / 'bevy'
    if not source.exists():
        shutil.copytree('/work/sources/bevy', source, symlinks=True,
                        ignore=shutil.ignore_patterns('.git', 'target'))
    main = source / 'examples/compression_probe.rs'
    original = (HERE / 'large-fixtures/bevy.rs').read_text()
    main.write_text(original)
    # Fetch before measuring; both the lockfile and source are then held fixed.
    subprocess.run([CARGO, 'fetch', '--locked'], cwd=source, check=True)
    linker = BASE / 'clang-wild'
    linker.write_text(f'#!/bin/sh\nexec clang -fuse-ld={WILD} "$@"\n')
    linker.chmod(0o755)
    identity = {'rustc_version': subprocess.check_output([RUSTC, '-vV'], text=True),
                'rustc_sha256': bench.sha256_file(RUSTC), 'wild_sha256': bench.sha256_file(WILD),
                'rustc_driver_sha256': {str(p): bench.sha256_file(p) for p in
                    (RUSTC.parent.parent / 'lib').glob('*rustc_driver*')},
                'lock_sha256': bench.sha256_file(source / 'Cargo.lock'),
                'bevy_source_revision': subprocess.check_output(
                    ['git', '-C', '/work/sources/bevy', 'rev-parse', 'HEAD'], text=True).strip(),
                'probe_source_sha256': bench.sha256_file(main),
                'uname': subprocess.check_output(['uname', '-a'], text=True),
                'notes': 'ARM64 OrbStack container, 4 CPU quota, 12 GiB cap. Warm host/cache, one clean build per arm.'}
    results = {'identity': identity, 'arms': []}
    configurations = {}
    for arm in ('ordinary', 'wrapper', 'compact'):
        directory = OUT / 'bevy' / arm
        directory.mkdir(parents=True, exist_ok=False)
        target, scratch = BASE / arm / 'target', BASE / arm / 'tmp'
        scratch.mkdir(parents=True)
        main.write_text(original)
        flags = ['-Zembed-metadata=no', '-Cdebuginfo=0', '-Clto=no', '-Cembed-bitcode=no',
                 f'-Clinker={linker}', '-Zartifact-compression-profile=balanced',
                 f'--remap-path-prefix={source}=/src/bevy']
        if arm == 'wrapper':
            flags += ['-Zcompress-artifacts=yes', '-Zcompress-incremental=yes']
        if arm == 'compact':
            flags += [f'-Zcompact-artifact-store={target / "compact-store"}', '-Zcompress-incremental=yes']
        env = {k: v for k, v in os.environ.items() if k not in
               {'RUSTFLAGS', 'CARGO_ENCODED_RUSTFLAGS', 'RUSTC_WRAPPER', 'RUSTC_WORKSPACE_WRAPPER'}}
        env.update(RUSTC=str(RUSTC), RUSTC_BOOTSTRAP='1', CARGO_TARGET_DIR=str(target),
                   CARGO_INCREMENTAL='1', CARGO_PROFILE_DEV_DEBUG='0',
                   CARGO_ENCODED_RUSTFLAGS='\x1f'.join(flags), TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch))
        record = {'arm': arm, 'flags': flags, 'phases': []}
        configurations[arm] = (env, target, directory)
        for step in range(5):
            name = ('clean', 'noop', 'edit1', 'edit2', 'edit3')[step]
            if step >= 2:
                main.write_text(original.replace('fn main() {', f'fn main() {{\n    println!("edit:{step}");', 1))
            command = [str(CARGO), '-Zno-embed-metadata', 'build', '--offline', '--locked', '-j4', '-v',
                       '-p', 'bevy', '--example', 'compression_probe']
            print(f'{arm}/{name}', flush=True)
            measurement = bench.run_measured(command, cwd=source, env=env, target=target,
                temp_dir=scratch, log_dir=directory / name, label=name, interval=0.25, timeout=None)
            (directory / f'{name}.json').write_text(json.dumps(measurement, indent=2) + '\n')
            assert measurement['exit_code'] == 0, directory / name
            binary = target / 'debug/examples/compression_probe'
            p = subprocess.run([binary], cwd=source, capture_output=True, text=True, env=env)
            expected = (f'edit:{step}\n' if step >= 2 else '') + 'bevy: entities=1000 updates=30 sum=559500\n'
            assert p.returncode == 0 and p.stdout == expected, p
            row = {'phase': name, 'measurement': measurement,
                   'census': bench.census([target, scratch]),
                   'runtime': {'exit': p.returncode, 'stdout': p.stdout, 'sha256': bench.sha256_file(binary)}}
            record['phases'].append(row)
            (directory / 'run.json').write_text(json.dumps(record, indent=2) + '\n')
            print(f"{arm}/{name}: {measurement['wall_seconds']:.3f}s, {row['census']['allocated_unique_inode_bytes']/2**30:.3f} GiB", flush=True)
        census = bench.census([target, scratch], with_files=True)
        (directory / 'files.json').write_text(json.dumps(census, indent=2) + '\n')
        assert not any(scratch.iterdir()), list(scratch.iterdir())
        # Separate untimed attribution run, with per-process metadata/link counters.
        main.write_text(original.replace('fn main() {', 'fn main() {\n    println!("stats");', 1))
        with (directory / 'stats.log').open('w') as log:
            subprocess.run(command, cwd=source, env={**env, 'RUSTC_COMPACT_STATS':'1', 'WILD_COMPACT_STATS':'1'},
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        results['arms'].append(record)
        (OUT / 'bevy-summary.json').write_text(json.dumps(results, indent=2) + '\n')
    # Rotate order and omit the disk sampler to check that an edit-time result
    # is not simply monitor overhead or which arm happened to run first.
    confirmation = []
    orders = [('ordinary', 'wrapper', 'compact'), ('compact', 'wrapper', 'ordinary'),
              ('wrapper', 'ordinary', 'compact'), ('compact', 'ordinary', 'wrapper')] * 2
    for trial, order in enumerate(orders):
        main.write_text(original.replace('fn main() {', f'fn main() {{\n    println!("confirm:{trial}");', 1))
        for arm in order:
            env, target, directory = configurations[arm]
            command = [str(CARGO), '-Zno-embed-metadata', 'build', '--offline', '--locked', '-j4',
                       '-p', 'bevy', '--example', 'compression_probe']
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
            started = time.monotonic()
            with (directory / f'confirm-{trial}.log').open('w') as log:
                p = subprocess.run(command, cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT)
            elapsed = time.monotonic() - started
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            assert p.returncode == 0
            p = subprocess.run([target / 'debug/examples/compression_probe'], cwd=source, env=env,
                               text=True, capture_output=True)
            assert p.returncode == 0 and p.stdout == f'confirm:{trial}\nbevy: entities=1000 updates=30 sum=559500\n'
            confirmation.append({'trial': trial, 'order': order, 'arm': arm, 'wall_seconds': elapsed,
                'user_seconds': after.ru_utime - before.ru_utime, 'system_seconds': after.ru_stime - before.ru_stime,
                'runtime_stdout': p.stdout})
            (OUT / 'bevy-confirmation.json').write_text(json.dumps(confirmation, indent=2) + '\n')
            print(f'{arm}/confirm-{trial}: {elapsed:.3f}s', flush=True)
    main.write_text(original)


if __name__ == '__main__':
    main()
