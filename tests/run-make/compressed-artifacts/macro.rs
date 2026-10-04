extern crate proc_macro;

use proc_macro::TokenStream;

#[doc = include_str!("large-doc.txt")]
#[proc_macro]
pub fn identity(input: TokenStream) -> TokenStream {
    input
}
