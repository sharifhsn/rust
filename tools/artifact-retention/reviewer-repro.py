#!/usr/bin/env python3
"""Small offline check/build/test retention reproduction; accepts a built patched Cargo."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cargo', type=Path, required=True)
    parser.add_argument('--toolchain', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out = args.out.resolve()
    args.cargo = args.cargo.resolve()
    args.toolchain = args.toolchain.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out/'src').mkdir()
    (args.out/'shared/src').mkdir(parents=True)
    (args.out/'Cargo.toml').write_text('''[workspace]
[package]
name="reviewer_repro"
version="0.1.0"
edition="2024"
[features]
first=[]
second=[]
[dependencies]
shared={path="shared"}
[profile.dev]
incremental=true
debug=2
''')
    (args.out/'shared/Cargo.toml').write_text('[package]\nname="shared"\nversion="0.1.0"\nedition="2024"\n')
    (args.out/'shared/src/lib.rs').write_text('pub fn answer()->u32 {42}\n')
    (args.out/'src/lib.rs').write_text('pub fn answer()->u32 {shared::answer()}\n#[test] fn runtime_oracle(){assert_eq!(answer(),42)}\n')
    (args.out/'src/main.rs').write_text('fn main(){println!("{}:{}",if cfg!(feature="second"){"second"}else{"first"},reviewer_repro::answer())}\n')
    env = {k:v for k,v in os.environ.items() if not k.startswith('CARGO_')
           and k not in {'RUSTFLAGS','RUSTDOCFLAGS','RUSTC_WRAPPER','RUSTC_WORKSPACE_WRAPPER'}}
    suffix = '.exe' if os.name=='nt' else ''
    env.update(CARGO_HOME=str(args.out/'cargo-home'), CARGO_TARGET_DIR=str(args.out/'target'),
               CARGO_TERM_COLOR='never', RUSTC=str(args.toolchain/('rustc'+suffix)),
               RUSTDOC=str(args.toolchain/('rustdoc'+suffix)),
               PATH=str(args.toolchain)+os.pathsep+env.get('PATH',''),
               __CARGO_TEST_CHANNEL_OVERRIDE_DO_NOT_USE_THIS='nightly')
    state_path=args.out/'target/.cargo-active-artifacts/state.json'
    def state(): return json.loads(state_path.read_text())
    def units(value): return set().union(*map(set,value['sessions']['reviewer']['slots'].values()))
    rows=[]
    def run(command, feature, warm=False):
        argv=[str(args.cargo),command,'--features',feature,'--offline','-Zactive-artifacts',
              '--config','build.artifact-session="reviewer"','--message-format=json']
        result=subprocess.run(argv,cwd=args.out,env=env,text=True,capture_output=True,timeout=120)
        name=f'{len(rows):02d}-{feature}-{command}'
        (args.out/(name+'.stdout')).write_text(result.stdout)
        (args.out/(name+'.stderr')).write_text(result.stderr)
        artifacts=[]
        for line in result.stdout.splitlines():
            try: value=json.loads(line)
            except ValueError: continue
            if isinstance(value,dict) and value.get('reason')=='compiler-artifact': artifacts.append(value)
        row=dict(command=argv,returncode=result.returncode,artifacts=len(artifacts),
                 fresh=sum(a['fresh'] for a in artifacts),warm=warm)
        rows.append(row)
        (args.out/'commands.json').write_text(json.dumps(rows,indent=2)+'\n')
        assert result.returncode==0,result.stderr
        assert artifacts
        if warm: assert all(a['fresh'] for a in artifacts),row
        return artifacts
    snapshots=[]
    for feature in ['first','second']:
        for command in ['check','build','test']:
            run(command,feature)
        for command in ['check','build','test']:
            run(command,feature,warm=True)
        binary=args.out/('target/debug/reviewer_repro'+suffix)
        assert subprocess.check_output([binary],text=True)==feature+':42\n'
        snapshots.append(state())
        (args.out/(feature+'-state.json')).write_text(json.dumps(snapshots[-1],indent=2)+'\n')
    retired=units(snapshots[0])-units(snapshots[1])
    assert retired and all(not (args.out/'target'/p).exists() for p in retired)
    returned=run('check','first')
    assert any(not a['fresh'] for a in returned)
    run('check','first',warm=True)
    receipt=dict(cargo_sha256=hashlib.sha256(args.cargo.read_bytes()).hexdigest(),
                 harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                 rustc=subprocess.check_output([args.toolchain/('rustc'+suffix),'-vV'],text=True),
                 commands=len(rows),all_six_warm_commands_fresh=True,
                 retired_units=len(retired),retired_directories_removed=True,
                 return_recompiled=True,runtime_oracles=['first:42','second:42'])
    (args.out/'summary.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt,indent=2))


if __name__=='__main__':
    main()
