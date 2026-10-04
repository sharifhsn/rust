"""Native compiler/metadata/incremental/Cargo/FFI regression tests on Linux."""
import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

BASE = Path(tempfile.mkdtemp(prefix='rust-tests-', dir='/tmp/compact-lab'))
OUT = Path(os.environ.get('COMPACT_LAB_OUTPUT',
    '/work/rust/docs/target-directory-size/measurements/2026-09-27-compact-wild'))
RUSTC = '/tmp/rust-compact-bootstrap/aarch64-unknown-linux-gnu/stage1/bin/rustc'
CARGO = '/tmp/rust-compact-bootstrap/aarch64-unknown-linux-gnu/stage0/bin/cargo'
WILD = '/tmp/wild-target/release/wild'
BASE.mkdir(parents=True, exist_ok=True)
records = []
env = {**os.environ, 'RUSTC_BOOTSTRAP': '1', 'RUSTC': RUSTC, 'WILD_COMPACT_STATS': '1',
       'RUSTC_COMPACT_STATS': '1'}


def run(command, cwd=BASE, extra=None, fail=False):
    p = subprocess.run(list(map(str, command)), cwd=cwd, env={**env, **(extra or {})}, text=True, capture_output=True)
    records.append(dict(command=list(map(str, command)), code=p.returncode, stdout=p.stdout, stderr=p.stderr))
    assert (p.returncode != 0) if fail else (p.returncode == 0), (command, p.stderr)
    return p


def main():
    (BASE / 'lib.rs').write_text('#[inline(never)] pub fn base()->u64 {2}\npub fn answer<T: Into<u64>>(x:T)->u64 { x.into()+base() }\n')
    (BASE / 'main.rs').write_text('fn main(){assert_eq!(compact_dep::answer(40u32),42);}\n')
    flags = ['-Zcompact-artifact-store=' + str(BASE / 'store'), '-Zcompress-incremental=yes',
             '-Zembed-metadata=no', '-Cdebuginfo=0', '-Clto=no', '-Cembed-bitcode=no',
             '-Clinker=clang', f'-Clink-arg=-fuse-ld={WILD}']
    library_command = [RUSTC, 'lib.rs', '--crate-name', 'compact_dep', '--crate-type', 'rlib', '--emit=link,metadata',
                       '-Cincremental=' + str(BASE / 'inc-lib'), *flags]
    run(library_command)
    assert (BASE / 'libcompact_dep.rlib').read_bytes().startswith(b'RCLIB001')
    assert (BASE / 'libcompact_dep.rmeta').read_bytes().startswith(b'RUSTZRL1')
    # Green metadata should retain its compressed inode across compiler sessions.
    metadata = BASE / 'libcompact_dep.rmeta'
    metadata_inode = metadata.stat().st_ino
    run(library_command)
    assert metadata.stat().st_ino == metadata_inode, 'reused metadata was unpacked or rewritten'
    metadata_aliases = [str(p) for p in (BASE / 'inc-lib').rglob('metadata.rmeta')
                        if p.stat().st_ino == metadata_inode]
    assert metadata_aliases, 'emitted and cached metadata do not share bytes'
    records.append({'reused_metadata_inode': metadata_inode, 'metadata_cache_aliases': metadata_aliases})
    # The manifest supplies its metadata reference even with only --extern=.rlib.
    command = [RUSTC, 'main.rs', '--extern', 'compact_dep=libcompact_dep.rlib',
               '-Cincremental=' + str(BASE / 'inc-bin'), *flags, '-o', 'app']
    run(command)
    run([BASE / 'app'])
    (BASE / 'main.rs').write_text('fn main(){assert_eq!(compact_dep::answer(41u32),43);}\n')
    run(command)
    run([BASE / 'app'])
    # Immutable compact bytes are shared with incremental work products.
    store_inodes = {p.stat().st_ino for p in (BASE / 'store').glob('*.rco')}
    shared = [str(p) for p in BASE.glob('inc-*/*/*/*.o') if p.stat().st_ino in store_inodes]
    assert shared, 'no shared compact/incremental inodes'
    records.append({'shared_incremental_objects': shared})
    # A copied/uplifted manifest retains its object and metadata references.
    alias = BASE / 'alias'; alias.mkdir(exist_ok=True)
    shutil.copyfile(BASE / 'libcompact_dep.rlib', alias / 'libcompact_dep.rlib')
    run([RUSTC, 'main.rs', '--extern', 'compact_dep=alias/libcompact_dep.rlib', *flags, '-o', 'alias-app'])
    run([BASE / 'alias-app'])
    # Actual Cargo FFI + proc-macro graph, with full debug information.
    fixture = BASE / 'fixture'
    if not fixture.exists():
        shutil.copytree('/work/rust/tools/compressed-artifacts/benchmark-fixtures/native-proc-macro', fixture)
    debug_flags = [x for x in flags if x != '-Cdebuginfo=0'] + ['-Cdebuginfo=2', '-Clink-arg=-Wl,--compress-debug-sections=zstd']
    cargo_env = {'CARGO_TARGET_DIR': str(BASE / 'cargo-target'), 'CARGO_INCREMENTAL': '1',
                 'CARGO_ENCODED_RUSTFLAGS': '\x1f'.join(debug_flags)}
    run([CARGO, '-Zno-embed-metadata', 'test', '--locked', '--all-targets', '-j2'], cwd=fixture, extra=cargo_env)
    run([CARGO, '-Zno-embed-metadata', 'build', '--locked', '-j2'], cwd=fixture, extra=cargo_env)
    run([BASE / 'cargo-target/debug/compression-fixture'])
    result = run(['gdb', '-batch', '-ex', 'info line compression_fixture::native_value',
                  BASE / 'cargo-target/debug/compression-fixture'])
    assert 'Line ' in result.stdout and 'src/lib.rs' in result.stdout, result.stdout
    # Unsupported LTO receives a diagnostic, rather than an incorrect artifact.
    p = run([RUSTC, 'lib.rs', '--crate-name', 'unsupported', '--crate-type', 'rlib',
             *[f for f in flags if f not in ('-Clto=no', '-Cembed-bitcode=no')], '-Clto=fat'], fail=True)
    assert 'require cross-crate LTO' in p.stderr, p.stderr
    # Quiescent GC keeps shared work products and drops a now-unreferenced entry.
    spec = importlib.util.spec_from_file_location('store_gc', Path(__file__).with_name('compact_store.py'))
    gc = importlib.util.module_from_spec(spec); spec.loader.exec_module(gc)
    orphan = BASE / 'store' / ('0' * 64 + '.rco')
    shutil.copyfile(next((BASE / 'store').glob('*.rco')), orphan)
    protected = BASE / 'protected.o'
    os.link(orphan, protected)
    before = gc.collect(BASE, BASE / 'store', '/tmp/wild-target/release/wild-pack', True)
    assert orphan.exists()
    protected.unlink()
    after = gc.collect(BASE, BASE / 'store', '/tmp/wild-target/release/wild-pack', True)
    assert not orphan.exists()
    records.append({'gc_with_hardlink': before, 'gc_after_unlink': after})
    run([BASE / 'app'])


try:
    main()
finally:
    (OUT / 'rust-integration.json').write_text(json.dumps(records, indent=2) + '\n')
print(f'{len(records)} compiler/Cargo checks passed')
