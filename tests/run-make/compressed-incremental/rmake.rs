//@ ignore-cross-compile
//@ ignore-backends: gcc

use std::path::{Path, PathBuf};

use run_make_support::rfs::create_dir;
use run_make_support::{run, rustc};

fn main() {
    // Cache compression must not leak into emitted metadata when artifact compression is off,
    // including a build that reuses the cache's packed metadata work product.
    create_dir("incr-metadata");
    create_dir("metadata-output");
    for state in ["not-loaded", "loaded"] {
        rustc()
            .input("metadata.rs")
            .crate_name("metadata_cache")
            .crate_type("rlib")
            .arg("--emit=link,metadata")
            .arg("--out-dir=metadata-output")
            .incremental("incr-metadata")
            .arg("-Cdebuginfo=0")
            .arg("-Zcompress-incremental")
            .arg("-Zcompress-artifacts=no")
            .arg(format!("-Zassert-incr-state={state}"))
            .run();
        let bytes = std::fs::read("metadata-output/libmetadata_cache.rmeta").unwrap();
        assert!(!bytes.starts_with(b"RUSTZRL1"), "cache compression leaked into emitted metadata");
        assert_packed_extension("incr-metadata", "rmeta");
    }

    // Check the same reuse assertions against ordinary raw cache files first.
    create_dir("incr-raw");
    compile("incr-raw", "first", "not-loaded", false, None, "raw-output");
    assert_raw_record("incr-raw", "dep-graph.bin");
    assert_graph_pages("incr-raw", false);
    compile("incr-raw", "second", "loaded", false, None, "raw-output");
    run("raw-output");

    create_dir("incr");
    compile("incr", "first", "not-loaded", true, None, "compressed-output");
    assert_raw_record("incr", "dep-graph.bin");
    assert_graph_pages("incr", true);
    assert_no_graph_index("incr");
    assert_packed_record("incr", "query-cache.bin");
    assert_packed_extension("incr", "o");
    let unchanged_object = find_file_matching("incr", &|path| {
        path.extension().is_some_and(|ext| ext == "o")
            && path.file_name().unwrap().to_string_lossy().contains("-y.")
    });
    assert_packed(&unchanged_object);

    // Changing x must regenerate its CGU while retaining and reusing y's packed workproduct.
    compile("incr", "second", "loaded", true, None, "compressed-output");
    run("compressed-output");

    // A damaged graph page stays lazy; only nodes in that page miss and recompute.
    corrupt_latest_graph_page("incr");
    compile("incr", "recovery", "loaded", true, None, "compressed-output");
    run("compressed-output");

    // A damaged dep-graph manifest is rejected; rustc falls back to a clean incremental state.
    corrupt_latest_record("incr", "dep-graph.bin");
    compile("incr", "recovery", "not-loaded", true, None, "compressed-output");
    run("compressed-output");

    // ThinLTO also reuses the unchanged CGU. FatLTO does not retain an individual work product
    // for y even in the raw control, so that arm checks packed bitcode reads and correct output.
    for lto in ["thin", "fat"] {
        let incremental_dir = format!("incr-{lto}");
        let output = format!("{lto}-output");
        create_dir(&incremental_dir);
        compile(&incremental_dir, "first", "not-loaded", true, Some(lto), &output);
        assert_packed_extension(&incremental_dir, "bc");
        let previous_session = preserve_lto_aliases(&incremental_dir, lto);
        let revision = if lto == "fat" { "fat_second" } else { "second" };
        compile(&incremental_dir, revision, "loaded", true, Some(lto), &output);
        for (alias, bytes) in previous_session {
            assert_eq!(
                std::fs::read(&alias).unwrap(),
                bytes,
                "previous cache file changed: {alias:?}"
            );
        }
        run(&output);
    }
}

fn compile(
    incremental_dir: &str,
    revision: &str,
    state: &str,
    compress: bool,
    lto: Option<&str>,
    output: &str,
) {
    let mut compiler = rustc();
    compiler
        .input("client.rs")
        .crate_name("compressed_incremental_test")
        .output(output)
        .incremental(incremental_dir)
        .arg(format!("--cfg={revision}"))
        .arg("-Cdebuginfo=0")
        .arg("-Ccodegen-units=16")
        .arg("-Zhuman-readable-cgu-names")
        .arg("-Zquery-dep-graph")
        .arg(format!("-Zassert-incr-state={state}"));
    if compress {
        compiler.arg("-Zcompress-incremental");
    }
    if let Some(lto) = lto {
        compiler.arg("-Copt-level=2").arg(format!("-Clto={lto}"));
    }
    compiler.run();
}

fn preserve_lto_aliases(incremental_dir: &str, lto: &str) -> Vec<(PathBuf, Vec<u8>)> {
    let paths = find_all_matching(incremental_dir, &|path| {
        path.to_string_lossy().ends_with(".pre-lto.bc")
            || path.file_name().is_some_and(|name| name == "thin-lto-past-keys.bin")
    });
    assert!(!paths.is_empty(), "expected persistent LTO work products");
    if lto == "thin" {
        assert!(
            paths.iter().any(|path| {
                path.file_name().is_some_and(|name| name == "thin-lto-past-keys.bin")
            })
        );
    }
    paths
        .into_iter()
        .enumerate()
        .map(|(index, path)| {
            let alias = PathBuf::from(format!("{lto}-previous-{index}"));
            std::fs::hard_link(&path, &alias).unwrap();
            let bytes = std::fs::read(&alias).unwrap();
            (alias, bytes)
        })
        .collect()
}

fn assert_packed_record(incremental_dir: &str, file_name: &str) {
    let path = find_named_file(incremental_dir, file_name);
    assert_packed(&path);
}

fn assert_raw_record(incremental_dir: &str, file_name: &str) {
    let path = find_named_file(incremental_dir, file_name);
    let bytes = std::fs::read(&path).unwrap();
    assert!(bytes.starts_with(b"RSIC"), "{} is missing the raw record header", path.display());
    assert!(!bytes.starts_with(b"RUSTZRL1"), "{} should remain raw", path.display());
}

fn assert_graph_pages(incremental_dir: &str, packed: bool) {
    let pages = find_all_matching(incremental_dir, &|path| {
        path.file_name().is_some_and(|name| name.to_string_lossy().starts_with("dep-graph-page-"))
    });
    assert!(!pages.is_empty(), "no paged dep-graph files found in {incremental_dir}");
    for path in pages {
        let bytes = std::fs::read(&path).unwrap();
        if packed {
            assert!(bytes.starts_with(b"RUSTZRL1"), "{} is not packed", path.display());
        } else {
            assert!(bytes.starts_with(b"RSDGPAG2"), "{} is not a graph page", path.display());
        }
    }
}

/// The reverse index is rebuilt in memory from the pages, so no index files are written.
fn assert_no_graph_index(incremental_dir: &str) {
    let buckets = find_all_matching(incremental_dir, &|path| {
        path.file_name().is_some_and(|name| name.to_string_lossy().starts_with("dep-graph-index-"))
    });
    assert!(buckets.is_empty(), "unexpected dep-graph index files: {buckets:?}");
}

fn corrupt_latest_graph_page(incremental_dir: &str) {
    let manifest = latest_named_file(incremental_dir, "dep-graph.bin");
    let path = std::fs::read_dir(manifest.parent().unwrap())
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| {
            path.file_name()
                .is_some_and(|name| name.to_string_lossy().starts_with("dep-graph-page-"))
        })
        .expect("matching incremental dep-graph page not found");
    let mut bytes = std::fs::read(&path).unwrap();
    *bytes.last_mut().expect("dep-graph page should not be empty") ^= 1;
    std::fs::write(path, bytes).unwrap();
}

fn assert_packed_extension(incremental_dir: &str, extension: &str) {
    let path = find_file_matching(incremental_dir, &|path| {
        path.extension().is_some_and(|found| found == extension)
            && std::fs::read(path).is_ok_and(|bytes| bytes.starts_with(b"RUSTZRL1"))
    });
    assert_packed(&path);
}

fn assert_packed(path: &Path) {
    let bytes = std::fs::read(path).unwrap();
    assert!(bytes.starts_with(b"RUSTZRL1"), "{} was not packed", path.display());
}

fn corrupt_latest_record(incremental_dir: &str, file_name: &str) {
    let path = latest_named_file(incremental_dir, file_name);
    assert_raw_record(incremental_dir, file_name);
    let mut bytes = std::fs::read(&path).unwrap();
    *bytes.last_mut().expect("packed cache record should not be empty") ^= 1;
    std::fs::write(path, bytes).unwrap();
}

fn find_named_file(incremental_dir: &str, file_name: &str) -> PathBuf {
    find_file_matching(incremental_dir, &|path| {
        path.file_name().is_some_and(|found| found == file_name)
    })
}

fn latest_named_file(incremental_dir: &str, file_name: &str) -> PathBuf {
    let files = find_all_matching(incremental_dir, &|path| {
        path.file_name().is_some_and(|found| found == file_name)
    });
    files
        .into_iter()
        .max_by_key(|path| {
            (
                std::fs::metadata(path).and_then(|metadata| metadata.modified()).ok(),
                path.parent().map(Path::to_path_buf),
            )
        })
        .expect("matching incremental cache file not found")
}

fn find_file_matching(incremental_dir: &str, matches: &dyn Fn(&Path) -> bool) -> PathBuf {
    find_all_matching(incremental_dir, matches)
        .into_iter()
        .next()
        .expect("matching incremental cache file not found")
}

fn find_all_matching(incremental_dir: &str, matches: &dyn Fn(&Path) -> bool) -> Vec<PathBuf> {
    let mut matches_found = Vec::new();
    let mut directories = vec![PathBuf::from(incremental_dir)];
    while let Some(directory) = directories.pop() {
        for entry in std::fs::read_dir(directory).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                directories.push(path);
            } else if matches(&path) {
                matches_found.push(path);
            }
        }
    }
    matches_found
}
