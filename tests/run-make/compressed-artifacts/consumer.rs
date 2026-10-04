const _: [(); 16] = [(); dep::array_size::<u32, 4>()];
const _: [(); 20] = [(); core::mem::size_of::<dep::ArrayAndTag<u32, 4>>()];

fn main() {
    assert_eq!(dep::array_size::<u32, 4>(), 16);
    assert_eq!(core::mem::size_of::<dep::ArrayAndTag<u32, 4>>(), 20);
    let value = dep::archived_value(std::hint::black_box(5));
    assert_eq!(std::hint::black_box(value), 108);
}
