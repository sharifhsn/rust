//@ ignore-cross-compile
//@ needs-crate-type: proc-macro

use run_make_support::rfs::create_dir;
use run_make_support::{
    cc, dynamic_lib_name, extra_c_flags, path, run, rust_lib_name, rustc, static_lib_name,
};

fn main() {
    std::fs::write(
        "large-doc.txt",
        "Repeated crate documentation makes the metadata artifact compressible.\n".repeat(8192),
    )
    .unwrap();

    create_dir("plain-rmeta");
    rustc()
        .input("dep.rs")
        .crate_name("dep")
        .crate_type("rlib")
        .emit("metadata")
        .out_dir("plain-rmeta")
        .run();
    compile_with_rmeta("plain-rmeta/libdep.rmeta", "plain-rmeta-client.rmeta");

    create_dir("compressed-rmeta");
    rustc()
        .input("dep.rs")
        .crate_name("dep")
        .crate_type("rlib")
        .emit("metadata")
        .out_dir("compressed-rmeta")
        .arg("-Zcompress-artifacts")
        .run();
    compile_with_rmeta("compressed-rmeta/libdep.rmeta", "compressed-rmeta-client.rmeta");

    for profile in ["fast", "balanced", "small", "legacy"] {
        let directory = format!("profile-{profile}");
        create_dir(&directory);
        rustc()
            .input("dep.rs")
            .crate_name("dep")
            .crate_type("rlib")
            .emit("metadata")
            .out_dir(&directory)
            .arg("-Zcompress-artifacts")
            .arg(format!("-Zartifact-compression-profile={profile}"))
            .run();
        compile_with_rmeta(
            &format!("{directory}/libdep.rmeta"),
            &format!("{directory}-client.rmeta"),
        );
    }
    create_dir("profile-custom");
    rustc()
        .input("dep.rs")
        .crate_name("dep")
        .crate_type("rlib")
        .emit("metadata")
        .out_dir("profile-custom")
        .arg("-Zcompress-artifacts")
        .arg("-Zartifact-compression-level=-1")
        .arg("-Zartifact-compression-chunk-size=16384")
        .run();
    let bytes = std::fs::read("profile-custom/libdep.rmeta").unwrap();
    assert_eq!(u32::from_le_bytes(bytes[16..20].try_into().unwrap()), 16384);
    compile_with_rmeta("profile-custom/libdep.rmeta", "profile-custom-client.rmeta");
    for option in [
        "artifact-compression-profile=unknown",
        "artifact-compression-level=20",
        "artifact-compression-chunk-size=65535",
    ] {
        rustc()
            .input("dep.rs")
            .arg(format!("-Z{option}"))
            .run_fail()
            .assert_stderr_contains("incorrect value");
    }

    // Corrupt the final index byte while preserving the packed-format header. The reader
    // should report the codec error instead of silently treating the file as raw metadata.
    let rmeta_path = path("compressed-rmeta/libdep.rmeta");
    let mut bytes = std::fs::read(&rmeta_path).unwrap();
    *bytes.last_mut().unwrap() ^= 1;
    std::fs::write(&rmeta_path, bytes).unwrap();
    rustc()
        .input("consumer.rs")
        .extern_("dep", &rmeta_path)
        .emit("metadata")
        .output("corrupt-rmeta-client")
        .run_fail()
        .assert_stderr_contains("corrupt metadata encountered")
        .assert_stderr_contains("libdep.rmeta");

    create_dir("plain-rlib");
    rustc()
        .input("dep.rs")
        .crate_name("dep")
        .crate_type("rlib")
        .emit("link")
        .arg("-Cembed-bitcode=yes")
        .out_dir("plain-rlib")
        .run();

    create_dir("compressed-rlib");
    rustc()
        .input("dep.rs")
        .crate_name("dep")
        .crate_type("rlib")
        .emit("link")
        .arg("-Cembed-bitcode=yes")
        .out_dir("compressed-rlib")
        .arg("-Zcompress-artifacts")
        .run();

    let plain_rlib = path("plain-rlib").join(rust_lib_name("dep"));
    let compressed_rlib = path("compressed-rlib").join(rust_lib_name("dep"));
    let plain_size = std::fs::metadata(plain_rlib).unwrap().len();
    let compressed_size = std::fs::metadata(&compressed_rlib).unwrap().len();
    assert!(
        compressed_size < plain_size,
        "compressed rlib should be smaller: {compressed_size} >= {plain_size}"
    );
    compile_and_run_rlib(&compressed_rlib, "compressed-rlib-client");
    for lto in ["thin", "fat"] {
        let output = format!("compressed-rlib-{lto}-lto-client");
        rustc()
            .input("consumer.rs")
            .extern_("dep", &compressed_rlib)
            .arg(format!("-Clto={lto}"))
            .output(&output)
            .run();
        run(&output);
    }

    create_dir("compressed-staticlib");
    let staticlib = path("compressed-staticlib").join(static_lib_name("compressed_consumer"));
    rustc()
        .input("staticlib-consumer.rs")
        .crate_name("compressed_consumer")
        .crate_type("staticlib")
        .extern_("dep", &compressed_rlib)
        .out_dir("compressed-staticlib")
        .arg("-Zcompress-artifacts")
        .run();
    let archive = std::fs::read(&staticlib).unwrap();
    assert!(archive.starts_with(b"!<arch>\n"), "staticlib should be a conventional ar archive");
    cc().input("staticlib-client.c")
        .input(&staticlib)
        .out_exe("compressed-staticlib-client")
        .args(extra_c_flags())
        .run();
    run("compressed-staticlib-client");

    assert_no_artifact_tempdirs();
    rustc()
        .input("failed-link.rs")
        .extern_("dep", &compressed_rlib)
        .output("failed-compressed-link")
        .run_fail()
        .assert_stderr_contains("rustc_compressed_artifacts_missing_symbol");
    assert_no_artifact_tempdirs();

    create_dir("plain-proc-macro");
    rustc()
        .input("macro.rs")
        .crate_name("test_macro")
        .crate_type("proc-macro")
        .emit("metadata,link")
        .out_dir("plain-proc-macro")
        .run();
    let plain_proc_macro = path("plain-proc-macro").join(dynamic_lib_name("test_macro"));
    compile_and_run_proc_macro(&plain_proc_macro, "plain-proc-macro-client");

    create_dir("compressed-proc-macro");
    rustc()
        .input("macro.rs")
        .crate_name("test_macro")
        .crate_type("proc-macro")
        .emit("metadata,link")
        .out_dir("compressed-proc-macro")
        .arg("-Zcompress-artifacts")
        .run();
    let compressed_proc_macro = path("compressed-proc-macro").join(dynamic_lib_name("test_macro"));
    compile_and_run_proc_macro(&compressed_proc_macro, "compressed-proc-macro-client");
}

fn compile_and_run_proc_macro(library: &std::path::Path, output: &str) {
    rustc().input("macro-consumer.rs").extern_("test_macro", library).output(output).run();
    run(output);
}

fn compile_with_rmeta(rmeta: &str, output: &str) {
    rustc().input("consumer.rs").extern_("dep", path(rmeta)).emit("metadata").output(output).run();
}

fn compile_and_run_rlib(rlib: &std::path::Path, output: &str) {
    rustc().input("consumer.rs").extern_("dep", rlib).output(output).run();
    run(output);
}

fn assert_no_artifact_tempdirs() {
    let tempdirs: Vec<_> = std::fs::read_dir(".")
        .unwrap()
        .map(|entry| entry.unwrap().file_name())
        .filter(|name| name.to_string_lossy().starts_with("rustc-artifacts"))
        .collect();
    assert!(tempdirs.is_empty(), "temporary artifact directories remain: {tempdirs:?}");
}
