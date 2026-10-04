#![allow(dead_code)]

#[path = "../../compiler/rustc_data_structures/src/artifact_compression.rs"]
mod artifact_compression;

use std::hint::black_box;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::Instant;
use std::{fs, io};

const MAGIC: &[u8; 8] = b"RUSTZRL1";
const HEADER_LEN: usize = 64;
const ENTRY_LEN: usize = 24;
const CHUNK_SIZE: usize = 64 * 1024;
const RAW_CHUNK: u32 = 1;
const MIN_CHUNK_SIZE: usize = 16 * 1024;
const MAX_CHUNK_SIZE: usize = 4 * 1024 * 1024;
static SLICING_TABLES: OnceLock<[[u32; 256]; 8]> = OnceLock::new();
#[cfg(target_arch = "aarch64")]
static HARDWARE_CRC_AVAILABLE: OnceLock<bool> = OnceLock::new();

#[link(name = "zstd")]
unsafe extern "C" {
    fn ZSTD_compressBound(src_size: usize) -> usize;
    fn ZSTD_compress(
        dst: *mut std::ffi::c_void,
        dst_capacity: usize,
        src: *const std::ffi::c_void,
        src_size: usize,
        compression_level: i32,
    ) -> usize;
    fn ZSTD_createCCtx() -> *mut std::ffi::c_void;
    fn ZSTD_freeCCtx(cctx: *mut std::ffi::c_void) -> usize;
    fn ZSTD_compressCCtx(
        cctx: *mut std::ffi::c_void,
        dst: *mut std::ffi::c_void,
        dst_capacity: usize,
        src: *const std::ffi::c_void,
        src_size: usize,
        compression_level: i32,
    ) -> usize;
    fn ZSTD_decompress(
        dst: *mut std::ffi::c_void,
        dst_capacity: usize,
        src: *const std::ffi::c_void,
        compressed_size: usize,
    ) -> usize;
    fn ZSTD_createDCtx() -> *mut std::ffi::c_void;
    fn ZSTD_freeDCtx(dctx: *mut std::ffi::c_void) -> usize;
    fn ZSTD_decompressDCtx(
        dctx: *mut std::ffi::c_void,
        dst: *mut std::ffi::c_void,
        dst_capacity: usize,
        src: *const std::ffi::c_void,
        compressed_size: usize,
    ) -> usize;
    fn ZSTD_isError(code: usize) -> u32;
    fn ZSTD_getErrorName(code: usize) -> *const std::ffi::c_char;
}

#[derive(Clone, Copy)]
struct Chunk {
    offset: usize,
    stored_len: usize,
    raw_len: usize,
    checksum: u32,
    flags: u32,
}

struct Sample {
    path: PathBuf,
    packed: Vec<u8>,
    raw: Vec<u8>,
    chunks: Vec<Chunk>,
}

#[derive(Clone, Copy)]
enum CrcMode {
    None,
    Table,
    Slice8,
    Hardware,
}

#[derive(Clone, Copy)]
enum DecoderMode {
    OneShot,
    ReusedContext,
}

#[derive(Clone, Copy)]
enum EncoderMode {
    OneShot,
    ReusedContext,
}

struct ReusedCCtx(*mut std::ffi::c_void);

impl ReusedCCtx {
    fn new() -> io::Result<Self> {
        let context = unsafe { ZSTD_createCCtx() };
        if context.is_null() {
            Err(io::Error::new(io::ErrorKind::Other, "could not allocate Zstandard CCtx"))
        } else {
            Ok(Self(context))
        }
    }
}

impl Drop for ReusedCCtx {
    fn drop(&mut self) {
        unsafe {
            ZSTD_freeCCtx(self.0);
        }
    }
}

struct ReusedDCtx(*mut std::ffi::c_void);

impl ReusedDCtx {
    fn new() -> io::Result<Self> {
        let context = unsafe { ZSTD_createDCtx() };
        if context.is_null() {
            Err(io::Error::new(io::ErrorKind::Other, "could not allocate Zstandard DCtx"))
        } else {
            Ok(Self(context))
        }
    }
}

impl Drop for ReusedDCtx {
    fn drop(&mut self) {
        unsafe {
            ZSTD_freeDCtx(self.0);
        }
    }
}

fn main() -> io::Result<()> {
    let mut args = std::env::args_os().skip(1);
    let mut root = None;
    let mut loops = 20usize;
    let mut verify_only = false;
    while let Some(arg) = args.next() {
        let text = arg.to_string_lossy();
        if text == "--verify-only" {
            verify_only = true;
        } else if let Some(value) = text.strip_prefix("--loops=") {
            loops = value.parse().map_err(|_| invalid("invalid --loops value"))?;
        } else if root.is_none() {
            root = Some(PathBuf::from(arg));
        } else {
            return Err(invalid("usage: codec-performance DIRECTORY [--loops=N] [--verify-only]"));
        }
    }
    let root = root
        .ok_or_else(|| invalid("usage: codec-performance DIRECTORY [--loops=N] [--verify-only]"))?;
    let samples = load_samples(&root)?;
    if samples.is_empty() {
        return Err(invalid("no packed .rlib or .rmeta samples found"));
    }

    verify_crc_implementations();
    verify_samples(&samples)?;
    let raw_total = samples.iter().map(|sample| sample.raw.len() as u64).sum::<u64>();
    let packed_total = samples.iter().map(|sample| sample.packed.len() as u64).sum::<u64>();
    eprintln!(
        "{} files: {:.2} MiB decoded, {:.2} MiB packed",
        samples.len(),
        raw_total as f64 / 1024.0 / 1024.0,
        packed_total as f64 / 1024.0 / 1024.0,
    );
    if verify_only {
        println!("correctness passed for {} packed files", samples.len());
        return Ok(());
    }

    println!("loops={loops}; MiB/s is decoded bytes divided by wall time");
    timed(
        "codec read_all (filesystem + container + production decoder)",
        raw_total,
        loops,
        || {
            for sample in &samples {
                black_box(artifact_compression::read_all(&sample.path)?);
            }
            Ok(())
        },
    )?;
    timed("direct one-shot compressor + IEEE CRC (64 KiB, level 3)", raw_total, loops, || {
        for sample in &samples {
            black_box(compress_sample(sample, EncoderMode::OneShot)?);
        }
        Ok(())
    })?;
    timed("direct reused CCtx + same IEEE CRC (64 KiB, level 3)", raw_total, loops, || {
        for sample in &samples {
            black_box(compress_sample(sample, EncoderMode::ReusedContext)?);
        }
        Ok(())
    })?;
    timed("direct one-shot zstd only (preloaded input)", raw_total, loops, || {
        for sample in &samples {
            black_box(decode(sample, CrcMode::None, DecoderMode::OneShot)?);
        }
        Ok(())
    })?;
    timed("direct one-shot zstd + table CRC (preloaded input)", raw_total, loops, || {
        for sample in &samples {
            black_box(decode(sample, CrcMode::Table, DecoderMode::OneShot)?);
        }
        Ok(())
    })?;
    timed("direct reused DCtx + table CRC (preloaded input)", raw_total, loops, || {
        for sample in &samples {
            black_box(decode(sample, CrcMode::Table, DecoderMode::ReusedContext)?);
        }
        Ok(())
    })?;
    timed("direct reused DCtx + slicing-by-8 CRC (preloaded input)", raw_total, loops, || {
        for sample in &samples {
            black_box(decode(sample, CrcMode::Slice8, DecoderMode::ReusedContext)?);
        }
        Ok(())
    })?;
    timed(
        "direct reused DCtx + hardware/fallback IEEE CRC (preloaded input)",
        raw_total,
        loops,
        || {
            for sample in &samples {
                black_box(decode(sample, CrcMode::Hardware, DecoderMode::ReusedContext)?);
            }
            Ok(())
        },
    )?;
    timed("table CRC only (predecoded bytes)", raw_total, loops, || {
        for sample in &samples {
            crc_only(sample, CrcMode::Table)?;
        }
        Ok(())
    })?;
    timed("slicing-by-8 CRC only (predecoded bytes)", raw_total, loops, || {
        for sample in &samples {
            crc_only(sample, CrcMode::Slice8)?;
        }
        Ok(())
    })?;
    timed("hardware/fallback IEEE CRC only (predecoded bytes)", raw_total, loops, || {
        for sample in &samples {
            crc_only(sample, CrcMode::Hardware)?;
        }
        Ok(())
    })?;
    Ok(())
}

fn load_samples(root: &Path) -> io::Result<Vec<Sample>> {
    let mut paths = fs::read_dir(root)?
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| {
            matches!(path.extension().and_then(|ext| ext.to_str()), Some("rlib" | "rmeta"))
        })
        .collect::<Vec<_>>();
    paths.sort();
    let mut samples = Vec::new();
    for path in paths {
        if !artifact_compression::is_compressed(&path)? {
            continue;
        }
        let packed = fs::read(&path)?;
        let (raw_len, chunks) = parse_container(&packed)?;
        let raw = artifact_compression::read_all(&path)?;
        if raw.len() != raw_len {
            return Err(invalid("codec output length differs from parsed header"));
        }
        samples.push(Sample { path, packed, raw, chunks });
    }
    Ok(samples)
}

fn parse_container(bytes: &[u8]) -> io::Result<(usize, Vec<Chunk>)> {
    if bytes.len() < HEADER_LEN || &bytes[..8] != MAGIC {
        return Err(invalid("not a packed-artifact file"));
    }
    let raw_len =
        usize::try_from(get_u64(bytes, 24)?).map_err(|_| invalid("artifact too large"))?;
    let count =
        usize::try_from(get_u64(bytes, 32)?).map_err(|_| invalid("chunk count too large"))?;
    let chunk_size = get_u32(bytes, 16)? as usize;
    let index_offset =
        usize::try_from(get_u64(bytes, 40)?).map_err(|_| invalid("index offset too large"))?;
    let index_len = usize::try_from(get_u64(bytes, 48)?).map_err(|_| invalid("index too large"))?;
    if !(MIN_CHUNK_SIZE..=MAX_CHUNK_SIZE).contains(&chunk_size)
        || !chunk_size.is_power_of_two()
        || count.checked_mul(ENTRY_LEN) != Some(index_len)
        || index_offset.checked_add(index_len) != Some(bytes.len())
        || index_offset < HEADER_LEN
        || count != raw_len.div_ceil(chunk_size)
    {
        return Err(invalid("inconsistent index bounds"));
    }
    let mut chunks = Vec::with_capacity(count);
    let mut raw_sum = 0usize;
    for entry in bytes[index_offset..].chunks_exact(ENTRY_LEN) {
        let chunk = Chunk {
            offset: usize::try_from(get_u64(entry, 0)?)
                .map_err(|_| invalid("chunk offset too large"))?,
            stored_len: get_u32(entry, 8)? as usize,
            raw_len: get_u32(entry, 12)? as usize,
            checksum: get_u32(entry, 16)?,
            flags: get_u32(entry, 20)?,
        };
        let expected = (raw_len - raw_sum).min(chunk_size);
        if chunk.raw_len != expected
            || chunk.offset.checked_add(chunk.stored_len).is_none_or(|end| end > index_offset)
        {
            return Err(invalid("invalid chunk bounds"));
        }
        raw_sum += chunk.raw_len;
        chunks.push(chunk);
    }
    if raw_sum != raw_len {
        return Err(invalid("chunk lengths do not cover artifact"));
    }
    Ok((raw_len, chunks))
}

fn decode(sample: &Sample, crc_mode: CrcMode, decoder_mode: DecoderMode) -> io::Result<Vec<u8>> {
    let mut output = vec![0; sample.raw.len()];
    let mut output_offset = 0usize;
    let mut context = match decoder_mode {
        DecoderMode::OneShot => None,
        DecoderMode::ReusedContext => Some(ReusedDCtx::new()?),
    };
    for chunk in &sample.chunks {
        let end = output_offset + chunk.raw_len;
        let destination = &mut output[output_offset..end];
        let input_end = chunk.offset + chunk.stored_len;
        let source = &sample.packed[chunk.offset..input_end];
        if chunk.flags == RAW_CHUNK {
            destination.copy_from_slice(source);
        } else {
            let result = unsafe {
                if let Some(context) = &mut context {
                    ZSTD_decompressDCtx(
                        context.0,
                        destination.as_mut_ptr().cast(),
                        destination.len(),
                        source.as_ptr().cast(),
                        source.len(),
                    )
                } else {
                    ZSTD_decompress(
                        destination.as_mut_ptr().cast(),
                        destination.len(),
                        source.as_ptr().cast(),
                        source.len(),
                    )
                }
            };
            if unsafe { ZSTD_isError(result) } != 0 {
                return Err(zstd_error(result));
            }
            if result != destination.len() {
                return Err(invalid("zstd returned an unexpected length"));
            }
        }
        let actual = match crc_mode {
            CrcMode::None => chunk.checksum,
            CrcMode::Table => crc_table(destination),
            CrcMode::Slice8 => crc_slice8(destination, slicing_tables()),
            CrcMode::Hardware => crc_hardware(destination),
        };
        if actual != chunk.checksum {
            return Err(invalid("decoded chunk checksum mismatch"));
        }
        output_offset = end;
    }
    Ok(output)
}

fn compress_sample(sample: &Sample, mode: EncoderMode) -> io::Result<u64> {
    const LEGACY_LEVEL: i32 = 3;
    let mut context = match mode {
        EncoderMode::OneShot => None,
        EncoderMode::ReusedContext => Some(ReusedCCtx::new()?),
    };
    let mut output = vec![0; unsafe { ZSTD_compressBound(CHUNK_SIZE) }];
    let mut stored_bytes = 0u64;
    for chunk in sample.raw.chunks(CHUNK_SIZE) {
        black_box(crc_hardware(chunk));
        let compressed_size = unsafe {
            if let Some(context) = &mut context {
                ZSTD_compressCCtx(
                    context.0,
                    output.as_mut_ptr().cast(),
                    output.len(),
                    chunk.as_ptr().cast(),
                    chunk.len(),
                    LEGACY_LEVEL,
                )
            } else {
                ZSTD_compress(
                    output.as_mut_ptr().cast(),
                    output.len(),
                    chunk.as_ptr().cast(),
                    chunk.len(),
                    LEGACY_LEVEL,
                )
            }
        };
        if unsafe { ZSTD_isError(compressed_size) } != 0 {
            return Err(zstd_error(compressed_size));
        }
        black_box(&output[..compressed_size]);
        stored_bytes += compressed_size.min(chunk.len()) as u64;
    }
    Ok(stored_bytes)
}

fn crc_only(sample: &Sample, mode: CrcMode) -> io::Result<()> {
    let mut offset = 0usize;
    for chunk in &sample.chunks {
        let bytes = &sample.raw[offset..offset + chunk.raw_len];
        let actual = match mode {
            CrcMode::Table => crc_table(bytes),
            CrcMode::Slice8 => crc_slice8(bytes, slicing_tables()),
            CrcMode::Hardware => crc_hardware(bytes),
            CrcMode::None => unreachable!(),
        };
        if actual != chunk.checksum {
            return Err(invalid("predecoded chunk checksum mismatch"));
        }
        offset += chunk.raw_len;
    }
    Ok(())
}

fn timed(
    name: &str,
    raw_bytes: u64,
    loops: usize,
    mut run: impl FnMut() -> io::Result<()>,
) -> io::Result<()> {
    let start = Instant::now();
    for _ in 0..loops {
        run()?;
    }
    let elapsed = start.elapsed();
    let mib = (raw_bytes as f64 * loops as f64) / 1024.0 / 1024.0;
    println!("{name}: {:.1} MiB/s ({:.3}s)", mib / elapsed.as_secs_f64(), elapsed.as_secs_f64());
    Ok(())
}

fn verify_samples(samples: &[Sample]) -> io::Result<()> {
    for sample in samples {
        let one_shot = decode(sample, CrcMode::Table, DecoderMode::OneShot)?;
        let reused_table = decode(sample, CrcMode::Table, DecoderMode::ReusedContext)?;
        let slice8 = decode(sample, CrcMode::Slice8, DecoderMode::ReusedContext)?;
        let hardware = decode(sample, CrcMode::Hardware, DecoderMode::ReusedContext)?;
        let no_crc = decode(sample, CrcMode::None, DecoderMode::OneShot)?;
        if one_shot != sample.raw
            || reused_table != sample.raw
            || slice8 != sample.raw
            || hardware != sample.raw
            || no_crc != sample.raw
        {
            return Err(invalid("direct decoder disagrees with codec read_all"));
        }
        crc_only(sample, CrcMode::Table)?;
        crc_only(sample, CrcMode::Slice8)?;
        crc_only(sample, CrcMode::Hardware)?;
    }
    Ok(())
}

fn verify_crc_implementations() {
    assert_eq!(crc_table(b"123456789"), 0xcbf4_3926);
    assert_eq!(crc_slice8(b"123456789", &slicing_tables()), 0xcbf4_3926);
    let mut state = 0x1234_5678u32;
    let bytes = (0..CHUNK_SIZE + 13)
        .map(|_| {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            state as u8
        })
        .collect::<Vec<_>>();
    let tables = slicing_tables();
    for len in
        (0..=64).chain([127, 255, 1023, CHUNK_SIZE - 1, CHUNK_SIZE, CHUNK_SIZE + 1, bytes.len()])
    {
        assert_eq!(crc_table(&bytes[..len]), crc_slice8(&bytes[..len], &tables));
        assert_eq!(crc_table(&bytes[..len]), crc_hardware(&bytes[..len]));
    }
}

fn crc_table(bytes: &[u8]) -> u32 {
    let mut crc = !0u32;
    for byte in bytes {
        crc = CRC_TABLE[((crc ^ u32::from(*byte)) & 0xff) as usize] ^ (crc >> 8);
    }
    !crc
}

fn slicing_tables() -> &'static [[u32; 256]; 8] {
    SLICING_TABLES.get_or_init(|| {
        let mut tables = [[0; 256]; 8];
        tables[0] = CRC_TABLE;
        for slice in 1..8 {
            for i in 0..256 {
                let prior = tables[slice - 1][i];
                tables[slice][i] = CRC_TABLE[(prior & 0xff) as usize] ^ (prior >> 8);
            }
        }
        tables
    })
}

fn crc_slice8(mut bytes: &[u8], tables: &[[u32; 256]; 8]) -> u32 {
    let mut crc = !0u32;
    while bytes.len() >= 8 {
        let first = u32::from_le_bytes(bytes[..4].try_into().unwrap()) ^ crc;
        crc = tables[7][(first & 0xff) as usize]
            ^ tables[6][((first >> 8) & 0xff) as usize]
            ^ tables[5][((first >> 16) & 0xff) as usize]
            ^ tables[4][((first >> 24) & 0xff) as usize]
            ^ tables[3][bytes[4] as usize]
            ^ tables[2][bytes[5] as usize]
            ^ tables[1][bytes[6] as usize]
            ^ tables[0][bytes[7] as usize];
        bytes = &bytes[8..];
    }
    for byte in bytes {
        crc = CRC_TABLE[((crc ^ u32::from(*byte)) & 0xff) as usize] ^ (crc >> 8);
    }
    !crc
}

#[cfg(target_arch = "aarch64")]
fn crc_hardware(bytes: &[u8]) -> u32 {
    if *HARDWARE_CRC_AVAILABLE.get_or_init(|| std::arch::is_aarch64_feature_detected!("crc")) {
        // Feature detection above guards this target-feature function.
        unsafe { crc_hardware_aarch64(bytes) }
    } else {
        crc_slice8(bytes, slicing_tables())
    }
}

#[cfg(not(target_arch = "aarch64"))]
fn crc_hardware(bytes: &[u8]) -> u32 {
    crc_slice8(bytes, slicing_tables())
}

#[cfg(target_arch = "aarch64")]
#[target_feature(enable = "crc")]
unsafe fn crc_hardware_aarch64(bytes: &[u8]) -> u32 {
    use std::arch::aarch64::{__crc32b, __crc32d, __crc32h, __crc32w};

    let mut crc = !0u32;
    let mut offset = 0usize;
    while bytes.len() - offset >= 8 {
        crc = __crc32d(crc, u64::from_le_bytes(bytes[offset..offset + 8].try_into().unwrap()));
        offset += 8;
    }
    if bytes.len() - offset >= 4 {
        crc = __crc32w(crc, u32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap()));
        offset += 4;
    }
    if bytes.len() - offset >= 2 {
        crc = __crc32h(crc, u16::from_le_bytes(bytes[offset..offset + 2].try_into().unwrap()));
        offset += 2;
    }
    if offset < bytes.len() {
        crc = __crc32b(crc, bytes[offset]);
    }
    !crc
}

const fn make_crc_table() -> [u32; 256] {
    let mut table = [0; 256];
    let mut i = 0;
    while i < 256 {
        let mut value = i as u32;
        let mut bit = 0;
        while bit < 8 {
            value = if value & 1 == 1 { (value >> 1) ^ 0xedb8_8320 } else { value >> 1 };
            bit += 1;
        }
        table[i] = value;
        i += 1;
    }
    table
}

const CRC_TABLE: [u32; 256] = make_crc_table();

fn get_u32(bytes: &[u8], offset: usize) -> io::Result<u32> {
    let field = bytes.get(offset..offset + 4).ok_or_else(|| invalid("short integer field"))?;
    Ok(u32::from_le_bytes(field.try_into().unwrap()))
}

fn get_u64(bytes: &[u8], offset: usize) -> io::Result<u64> {
    let field = bytes.get(offset..offset + 8).ok_or_else(|| invalid("short integer field"))?;
    Ok(u64::from_le_bytes(field.try_into().unwrap()))
}

fn zstd_error(code: usize) -> io::Error {
    let message = unsafe {
        let ptr = ZSTD_getErrorName(code);
        if ptr.is_null() {
            "unknown zstd error"
        } else {
            std::ffi::CStr::from_ptr(ptr).to_str().unwrap_or("zstd error")
        }
    };
    invalid(message)
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
