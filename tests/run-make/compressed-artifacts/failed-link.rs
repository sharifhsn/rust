unsafe extern "C" {
    fn rustc_compressed_artifacts_missing_symbol(value: u64);
}

fn main() {
    let value = dep::archived_value(std::hint::black_box(5));
    unsafe { rustc_compressed_artifacts_missing_symbol(value) };
}
