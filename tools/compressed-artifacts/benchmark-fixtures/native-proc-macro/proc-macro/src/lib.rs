use proc_macro::TokenStream;

#[proc_macro_attribute]
pub fn keep(_attributes: TokenStream, item: TokenStream) -> TokenStream {
    item
}
