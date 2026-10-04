#[test]
fn proc_macro_expansion_runs() {
    assert_eq!(compression_fixture::macro_value(), "ok");
    println!("fixture-test: proc-macro");
}
