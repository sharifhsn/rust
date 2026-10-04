pub const LARGE: [u8; 131_072] = [0x5a; 131_072];

pub fn value() -> usize {
    LARGE.len()
}
