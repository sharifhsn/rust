#[test]
fn native_archive_links() {
    assert_eq!(compression_fixture::native_value(), 42);
    println!("fixture-test: native");
}
