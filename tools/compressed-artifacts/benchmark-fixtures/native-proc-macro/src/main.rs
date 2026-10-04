fn main() {
    assert_eq!(compression_fixture::native_value(), 42);
    assert_eq!(compression_fixture::macro_value(), "ok");
    println!("compression-fixture: native=42 proc-macro=ok");
}
