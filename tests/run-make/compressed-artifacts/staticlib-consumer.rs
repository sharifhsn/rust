#[unsafe(no_mangle)]
pub extern "C" fn compressed_staticlib_value(value: u64) -> u64 {
    dep::archived_value(value)
}
