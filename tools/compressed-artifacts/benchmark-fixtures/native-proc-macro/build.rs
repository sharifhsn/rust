use std::env;
use std::path::PathBuf;
use std::process::Command;

fn run(command: &mut Command) {
    let status = command.status().expect("start native tool");
    assert!(status.success(), "native tool failed: {command:?}");
}

fn main() {
    let out = PathBuf::from(env::var_os("OUT_DIR").expect("OUT_DIR"));
    let object = out.join("answer.o");
    let archive = out.join("libfixture_native.a");
    run(Command::new("clang").arg("-c").arg("-O2").arg("native/answer.c").arg("-o").arg(&object));
    run(Command::new("ar").arg("crs").arg(&archive).arg(&object));
    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=fixture_native");
    println!("cargo:rerun-if-changed=native/answer.c");
}
