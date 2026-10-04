#[doc = include_str!("large-doc.txt")]
#[repr(C)]
pub struct ArrayAndTag<T, const N: usize> {
    pub values: [T; N],
    pub tag: u8,
}

pub const fn array_size<T, const N: usize>() -> usize {
    core::mem::size_of::<[T; N]>()
}

#[inline(never)]
pub fn archived_value(value: u64) -> u64 {
    value.wrapping_mul(17).wrapping_add(23)
}
