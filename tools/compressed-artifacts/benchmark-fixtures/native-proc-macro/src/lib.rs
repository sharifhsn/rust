use fixture_attribute::keep;

unsafe extern "C" {
    fn fixture_answer() -> u32;
}

#[keep]
pub fn macro_value() -> &'static str {
    "ok"
}

pub fn native_value() -> u32 {
    unsafe { fixture_answer() }
}

#[cfg(test)]
mod tests {
    #[test]
    fn unit_test_runs() {
        assert_eq!(super::macro_value(), "ok");
        println!("fixture-test: unit");
    }
}
