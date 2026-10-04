//! Small standalone driver for the experimental compressed-artifact codec.
//!
//! Build with:
//! `rustc --edition=2024 tools/compressed-artifacts/codec_cli.rs -L native=/opt/homebrew/lib -o /tmp/artifact-codec`

#[allow(dead_code)]
#[path = "../../compiler/rustc_data_structures/src/artifact_compression.rs"]
mod artifact_compression;

use std::ffi::OsString;
use std::io::{self, Write};
use std::path::Path;
use std::{env, fs};

fn main() {
    if let Err(error) = run() {
        eprintln!("artifact-codec: {error}");
        std::process::exit(1);
    }
}

fn run() -> io::Result<()> {
    let args = env::args_os().skip(1).collect::<Vec<_>>();
    let Some(command) = args.first().and_then(|arg| arg.to_str()) else {
        return usage();
    };
    match command {
        "pack" if args.len() == 2 => {
            let path = Path::new(&args[1]);
            let stats = artifact_compression::pack(path)?;
            println!(
                "{}: {} -> {} bytes, {} chunks, {}",
                path.display(),
                stats.original_bytes,
                stats.packed_bytes,
                stats.chunks,
                if stats.compressed { "packed" } else { "left raw (no size reduction)" }
            );
            Ok(())
        }
        "unpack" if args.len() == 3 => {
            artifact_compression::unpack_to(Path::new(&args[1]), Path::new(&args[2]))
        }
        "range" if (4..=5).contains(&args.len()) => {
            let path = Path::new(&args[1]);
            let offset = parse_number(&args[2], "offset")?;
            let length = usize::try_from(parse_number(&args[3], "length")?)
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "length is too large"))?;
            let bytes = artifact_compression::read_range(path, offset, length)?;
            if let Some(output) = args.get(4) {
                fs::write(output, bytes)
            } else {
                io::stdout().lock().write_all(&bytes)
            }
        }
        "cat" if args.len() == 2 => {
            let bytes = artifact_compression::read_all(Path::new(&args[1]))?;
            io::stdout().lock().write_all(&bytes)
        }
        "is-compressed" if args.len() == 2 => {
            println!("{}", artifact_compression::is_compressed(Path::new(&args[1]))?);
            Ok(())
        }
        _ => usage(),
    }
}

fn parse_number(value: &OsString, name: &str) -> io::Result<u64> {
    value
        .to_str()
        .and_then(|value| value.parse().ok())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, format!("invalid {name}")))
}

fn usage<T>() -> io::Result<T> {
    Err(io::Error::new(
        io::ErrorKind::InvalidInput,
        "usage: artifact-codec pack PATH | unpack SOURCE DEST | range PATH OFFSET LENGTH [OUTPUT] | cat PATH | is-compressed PATH",
    ))
}
