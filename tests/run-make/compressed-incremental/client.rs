#![feature(rustc_attrs)]
#![allow(internal_features)]
#![rustc_partition_codegened(module = "compressed_incremental_test-x", cfg = "second")]
#![rustc_partition_reused(module = "compressed_incremental_test-y", cfg = "second")]

mod x {
    #[cfg(first)]
    #[inline(never)]
    pub fn value() -> u64 {
        1
    }

    #[cfg(any(second, fat_second, recovery))]
    #[inline(never)]
    pub fn value() -> u64 {
        2
    }
}

mod y {
    #[used]
    static LARGE: [u8; 131_072] = [0x5a; 131_072];

    #[inline(never)]
    pub fn value() -> u64 {
        std::hint::black_box(&LARGE);
        40
    }
}

fn main() {
    assert_eq!(x::value() + y::value(), 42);
}
