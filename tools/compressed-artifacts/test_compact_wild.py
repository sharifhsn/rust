"""End-to-end tests of direct compact inputs. Run inside the ARM Linux container."""
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile

ROOT = Path(tempfile.mkdtemp(prefix='wild-tests-', dir='/tmp/compact-lab'))
WILD = '/tmp/wild-target/release/wild'
PACK = '/tmp/wild-target/release/wild-pack'
OUT = Path('/work/rust/docs/target-directory-size/measurements/2026-09-27-compact-wild')
ROOT.mkdir(parents=True, exist_ok=True)
records = []


def run(args, *, fail=False, env=None):
    p = subprocess.run(list(map(str, args)), cwd=ROOT, text=True, capture_output=True,
                       env={**os.environ, **(env or {})})
    records.append(dict(command=list(map(str, args)), code=p.returncode,
                        stdout=p.stdout, stderr=p.stderr))
    if fail:
        assert p.returncode != 0, args
    else:
        assert p.returncode == 0, (args, p.stderr)
    return p


def write(name, value):
    (ROOT / name).write_text(value)


def compile(name, language='c'):
    run(['clang++' if language == 'cpp' else 'clang', '-g', '-O0', '-ffunction-sections',
         '-fdata-sections', '-c', f'{name}.{language}', '-o', f'{name}.o'])


def pack(name):
    run([PACK, 'object', f'{name}.o', f'{name}.rco'], env={'COMPACT_BLOCK_SIZE': '1024'})


def link(name, inputs, extras=(), language='c', fail=False):
    p = run(['clang++' if language == 'cpp' else 'clang', f'-fuse-ld={WILD}',
             *inputs, '-Wl,--gc-sections,--threads=4', *extras, '-o', name], fail=fail,
            env={'WILD_COMPACT_STATS': '1'})
    if not fail:
        run([ROOT / name])
    return p


def main():
    write('used.c', '__thread int tls = 33;\n'
          '__attribute__((weak)) int weak(void) { return 0; }\n'
          'int used(void) { return tls + weak(); }\n')
    write('unused.c', 'const char unused_data[100000] = {1,2,3};\n'
          'int unused(void) { return unused_data[0]; }\n')
    write('main.c', 'int used(void); int weak(void){return 9;}\n'
          'int main(void){return used()!=42;}\n')
    for name in ('used', 'unused', 'main'):
        compile(name)
        pack(name)
    run(['ar', 'crs', 'raw.a', 'used.o', 'unused.o'])
    run([PACK, 'archive', 'raw.a', 'compact.rlib', 'store'], env={'COMPACT_BLOCK_SIZE': '1024'})
    link('raw', ['main.o', 'raw.a'])
    link('compact', ['main.rco', 'compact.rlib'])
    link('whole', ['main.rco', '-Wl,--whole-archive', 'compact.rlib', '-Wl,--no-whole-archive'])
    link('zstd-debug', ['main.rco', 'compact.rlib'], ['-Wl,--compress-debug-sections=zstd'])
    link('zlib-debug', ['main.rco', 'compact.rlib'], ['-Wl,--compress-debug-sections=zlib'])
    for name in ('compact', 'zstd-debug', 'zlib-debug'):
        p = run(['gdb', '-batch', '-ex', 'info line used', name])
        assert 'Line ' in p.stdout and 'used.c' in p.stdout, p.stdout
    records.append({'output_logical_bytes': {name: (ROOT / name).stat().st_size
                    for name in ('raw', 'compact', 'zstd-debug', 'zlib-debug')}})
    # Native compressed DWARF nested in the new container.
    run(['objcopy', '--compress-debug-sections=zlib', 'used.o', 'nested.o'])
    pack('nested')
    link('nested', ['main.rco', 'nested.rco'])
    # C++ tests COMDAT and actual exception unwinding through compact .eh_frame.
    write('exceptions.cpp', '#include <stdexcept>\n'
          'template<class T> __attribute__((noinline)) int twice(T x){return x+x;}\n'
          'int main(){try{if(twice(21)==42)throw std::runtime_error("ok");}'
          'catch(const std::exception &e){return e.what()[0]!=\'o\';}return 1;}\n')
    compile('exceptions', 'cpp')
    pack('exceptions')
    link('exceptions', ['exceptions.rco'], language='cpp')
    # Corrupt every payload byte of the unused member, leaving its index intact.
    candidate = None
    for path in (ROOT / 'store').glob('*.rco'):
        data = bytearray(path.read_bytes())
        if struct.unpack_from('<Q', data, 16)[0] == (ROOT / 'unused.o').stat().st_size:
            candidate = path
            payload = 96 + struct.unpack_from('<Q', data, 32)[0] + struct.unpack_from('<Q', data, 56)[0]
            for i in range(payload, len(data)):
                data[i] ^= 255
            path.write_bytes(data)
    assert candidate is not None
    link('unused-corrupt', ['main.rco', 'compact.rlib'])
    p = link('used-corrupt', ['main.rco', '-Wl,--whole-archive', 'compact.rlib',
                           '-Wl,--no-whole-archive'], fail=True)
    assert any(word in p.stderr.lower() for word in ('checksum', 'frame', 'corrupt', 'unknown')), p.stderr


try:
    main()
finally:
    (OUT / 'integration.json').write_text(json.dumps(records, indent=2) + '\n')
print(f'{len(records)} commands passed; debug symbols, TLS, weak symbols, COMDAT, exceptions and lazy corruption checks passed')
