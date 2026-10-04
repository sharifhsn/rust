//! Functions for saving and removing intermediate [work products].
//!
//! [work products]: WorkProduct

use std::fs as std_fs;
use std::path::{Path, PathBuf};

use rustc_data_structures::unord::UnordMap;
use rustc_fs_util::link_or_copy;
use rustc_middle::dep_graph::{WorkProduct, WorkProductId};
use rustc_session::Session;
use tracing::debug;

use crate::diagnostics;
use crate::persist::file_format;
use crate::persist::fs::*;

/// Copies a CGU work product to the incremental compilation directory, so next compilation can
/// find and reuse it.
///
/// Panics when incr comp is disabled.
pub fn copy_cgu_workproduct_to_incr_comp_cache_dir(
    sess: &Session,
    cgu_name: &str,
    files: &[(&'static str, &Path)],
    known_links: &[PathBuf],
) -> (WorkProductId, WorkProduct) {
    debug!(?cgu_name, ?files);
    assert!(sess.opts.incremental.is_some());

    let mut saved_files = UnordMap::default();
    for (ext, path) in files {
        let file_name = format!("{cgu_name}.{ext}");
        let path_in_incr_dir = in_incr_comp_dir_sess(sess, &file_name);
        if known_links.contains(&path_in_incr_dir) {
            let _ = saved_files.insert(ext.to_string(), file_name);
            continue;
        }
        if *ext == "o"
            && let Some(store) = &sess.opts.unstable_opts.compact_artifact_store
        {
            use rustc_data_structures::compact_artifact as compact;
            let result = (|| -> std::io::Result<()> {
                let bytes = std_fs::read(path)?;
                let object = if sess.opts.unstable_opts.compact_artifact_objects == "elf" {
                    compact::store_raw_object(&bytes, store)?
                } else {
                    let profile = sess.opts.unstable_opts.artifact_compression_options();
                    compact::store_object(
                        &bytes,
                        store,
                        compact::Options { level: profile.level, block_size: profile.chunk_size },
                    )?
                };
                compact::link_object(&object, path)?;
                compact::link_object(&object, &path_in_incr_dir)
            })();
            match result {
                Ok(()) => {
                    let _ = saved_files.insert(ext.to_string(), file_name);
                }
                Err(err) => {
                    sess.dcx().emit_warn(diagnostics::CopyWorkProductToCache {
                        from: path,
                        to: &path_in_incr_dir,
                        err,
                    });
                }
            }
            continue;
        }
        match link_or_copy(path, &path_in_incr_dir) {
            Ok(_) => {
                // A `.dwo` stays hard-linked to the copy debuggers read from the output
                // directory; packing it would store its DWARF twice.
                if *ext != "dwo" {
                    file_format::compress_incremental_artifact(
                        sess,
                        &path_in_incr_dir,
                        "compress_incremental_work_product",
                    );
                }
                let _ = saved_files.insert(ext.to_string(), file_name);
            }
            Err(err) => {
                sess.dcx().emit_warn(diagnostics::CopyWorkProductToCache {
                    from: path,
                    to: &path_in_incr_dir,
                    err,
                });
            }
        }
    }

    let work_product = WorkProduct { cgu_name: cgu_name.to_string(), saved_files };
    debug!(?work_product);
    let work_product_id = WorkProductId::from_cgu_name(cgu_name);
    (work_product_id, work_product)
}

/// Removes files for a given work product.
pub(crate) fn delete_workproduct_files(sess: &Session, work_product: &WorkProduct) {
    for (_, path) in work_product.saved_files.items().into_sorted_stable_ord() {
        let path = in_incr_comp_dir_sess(sess, path);
        if let Err(err) = std_fs::remove_file(&path) {
            sess.dcx().emit_warn(diagnostics::DeleteWorkProduct { path: &path, err });
        }
    }
}
