//! A small, versioned container for independently compressed compiler artifacts.
//!
//! This is intentionally separate from the archive format understood by linkers. A packed
//! artifact keeps its original path, but must be decoded before it is passed to a system linker.
//! The format uses independently compressed Zstandard frames so callers can read a range without
//! decompressing unrelated chunks. The standalone codec defaults to 64 KiB frames; callers can
//! choose a supported chunk size. The codec links to the system `libzstd` through its C API.

use std::ffi::{CStr, c_char, c_int, c_void};
use std::fs::{self, File, FileTimes, OpenOptions, Permissions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
#[cfg(target_arch = "aarch64")]
use std::sync::OnceLock;
use std::sync::atomic::{AtomicU64, Ordering};

pub(super) const MAGIC: &[u8; 8] = b"RUSTZRL1";
const VERSION: u32 = 1;
const HEADER_LEN: u64 = 64;
const INDEX_ENTRY_LEN: u64 = 24;
pub const DEFAULT_CHUNK_SIZE: usize = 64 * 1024;
pub const MIN_CHUNK_SIZE: usize = 16 * 1024;
pub const MAX_CHUNK_SIZE: usize = 4 * 1024 * 1024;
pub const DEFAULT_COMPRESSION_LEVEL: i32 = 3;
const RAW_CHUNK: u32 = 1;
const MINIMUM_PACKABLE_SIZE: u64 = HEADER_LEN + INDEX_ENTRY_LEN;

const HEADER_CRC_OFFSET: usize = 56;
const INDEX_CRC_OFFSET: usize = 60;

static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

#[link(name = "zstd")]
unsafe extern "C" {
    fn ZSTD_compressBound(src_size: usize) -> usize;
    fn ZSTD_createCCtx() -> *mut c_void;
    fn ZSTD_freeCCtx(cctx: *mut c_void) -> usize;
    fn ZSTD_compressCCtx(
        cctx: *mut c_void,
        dst: *mut c_void,
        dst_capacity: usize,
        src: *const c_void,
        src_size: usize,
        compression_level: c_int,
    ) -> usize;
    fn ZSTD_createDCtx() -> *mut c_void;
    fn ZSTD_freeDCtx(dctx: *mut c_void) -> usize;
    fn ZSTD_decompressDCtx(
        dctx: *mut c_void,
        dst: *mut c_void,
        dst_capacity: usize,
        src: *const c_void,
        compressed_size: usize,
    ) -> usize;
    fn ZSTD_isError(code: usize) -> u32;
    fn ZSTD_getErrorName(code: usize) -> *const c_char;
}

/// Compression settings for new artifact containers.
///
/// The chunk size is stored in the existing v1 header field, so readers can decode containers
/// produced with any supported chunk size. The default preserves the original 64 KiB format.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CompressionOptions {
    pub level: i32,
    pub chunk_size: usize,
}

impl Default for CompressionOptions {
    fn default() -> Self {
        Self { level: DEFAULT_COMPRESSION_LEVEL, chunk_size: DEFAULT_CHUNK_SIZE }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PackStats {
    /// Number of bytes in the original, uncompressed file.
    pub original_bytes: u64,
    /// Number of bytes stored at the path after packing. This equals `original_bytes` when
    /// packing did not reduce the file size and the original file was left in place.
    pub packed_bytes: u64,
    /// Number of independently decodable chunks in the packed representation.
    pub chunks: u64,
    /// Whether the path contains the compressed representation after this call.
    pub compressed: bool,
}

#[derive(Clone, Copy, Debug)]
pub(super) struct ChunkEntry {
    pub(super) offset: u64,
    pub(super) stored_len: u32,
    pub(super) raw_len: u32,
    pub(super) checksum: u32,
    pub(super) flags: u32,
}

pub(super) struct Container {
    pub(super) raw_len: u64,
    pub(super) chunk_size: usize,
    pub(super) entries: Vec<ChunkEntry>,
    pub(super) packed_len: u64,
}

/// Returns whether a file begins with this module's packed-artifact magic.
///
/// A matching magic identifies the format; callers that need to consume the artifact should use
/// `read_all`, `read_range`, or `unpack_to`, which also validate the complete header and index.
pub fn is_compressed(path: &Path) -> io::Result<bool> {
    let mut file = File::open(path)?;
    let mut magic = [0; MAGIC.len()];
    let mut read = 0;
    while read < magic.len() {
        match file.read(&mut magic[read..])? {
            0 => return Ok(false),
            n => read += n,
        }
    }
    Ok(&magic == MAGIC)
}

/// Compresses `path` in place, replacing it atomically only when compression reduces its size.
///
/// The temporary packed file is created beside the source so the final rename stays on the same
/// filesystem. Any failure before the rename leaves the original file intact and removes the
/// temporary file on normal error unwinding. The caller must ensure that no other process writes
/// the source while packing it; the length is checked for concurrent growth, but same-length
/// concurrent modifications cannot be detected. The source modification time is preserved so
/// artifact consumers that use timestamps for freshness checks do not see a synthetic rebuild.
pub fn pack(path: &Path) -> io::Result<PackStats> {
    pack_with_options(path, CompressionOptions::default())
}

/// Compresses `path` in place using `options`, replacing it atomically only when compression
/// reduces its size. Existing packed files are left unchanged, regardless of `options`.
///
/// The temporary packed file is created beside the source so the final rename stays on the same
/// filesystem. Any failure before the rename leaves the original file intact and removes the
/// temporary file on normal error unwinding. The caller must ensure that no other process writes
/// the source while packing it; the length is checked for concurrent growth, but same-length
/// concurrent modifications cannot be detected. The source modification time is preserved so
/// artifact consumers that use timestamps for freshness checks do not see a synthetic rebuild.
pub fn pack_with_options(path: &Path, options: CompressionOptions) -> io::Result<PackStats> {
    validate_options(options)?;

    let mut input = File::open(path)?;
    if has_magic(&mut input)? {
        let container = parse_container_from_file(&mut input)?;
        return Ok(PackStats {
            original_bytes: container.raw_len,
            packed_bytes: container.packed_len,
            chunks: container.entries.len() as u64,
            compressed: true,
        });
    }

    input.seek(SeekFrom::Start(0))?;
    let metadata = input.metadata()?;
    let raw_len = metadata.len();
    let chunks = chunk_count(raw_len, options.chunk_size)?;

    // A v1 container needs a header, an index entry, and at least one payload byte. For files no
    // larger than that fixed overhead, compression cannot save space, so avoid creating a temp
    // file, buffers, or a Zstandard context.
    if raw_len <= MINIMUM_PACKABLE_SIZE {
        return Ok(PackStats {
            original_bytes: raw_len,
            packed_bytes: raw_len,
            chunks,
            compressed: false,
        });
    }

    let mut compressor = ZstdCCtx::new()?;
    // Large presets should not allocate multi-megabyte scratch buffers for short artifacts.
    // The on-disk chunk geometry still uses the configured size; only scratch storage shrinks.
    let buffer_size = usize::try_from(raw_len.min(options.chunk_size as u64)).unwrap();
    let mut raw = allocate_zeroed(buffer_size, "artifact chunk buffer is too large")?;
    let compressed_capacity = unsafe { ZSTD_compressBound(buffer_size) };
    let mut compressed =
        allocate_zeroed(compressed_capacity, "compressed artifact buffer is too large")?;
    let mut output = TempOutput::new(path)?;
    let count = usize::try_from(chunks)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "too many artifact chunks"))?;
    let mut entries = Vec::new();
    entries
        .try_reserve_exact(count)
        .map_err(|_| invalid_data("artifact chunk index is too large"))?;
    let mut remaining = raw_len;

    output.file_mut().write_all(&[0; HEADER_LEN as usize])?;
    // The payload size is known before writing, so track the offset instead of asking the
    // filesystem for the current position on every chunk.
    let mut output_offset = HEADER_LEN;
    while remaining != 0 {
        let raw_size = usize::try_from(remaining.min(options.chunk_size as u64)).unwrap();
        input.read_exact(&mut raw[..raw_size])?;

        let compressed_size =
            compressor.compress(&raw[..raw_size], &mut compressed, options.level)?;
        let (bytes, flags) = if compressed_size < raw_size {
            (&compressed[..compressed_size], 0)
        } else {
            (&raw[..raw_size], RAW_CHUNK)
        };
        let offset = output_offset;
        let stored_len = u32::try_from(bytes.len()).map_err(|_| {
            io::Error::new(io::ErrorKind::InvalidData, "compressed chunk too large")
        })?;
        let raw_len_u32 = u32::try_from(raw_size)
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "raw chunk too large"))?;
        output.file_mut().write_all(&bytes)?;
        output_offset = output_offset
            .checked_add(u64::from(stored_len))
            .ok_or_else(|| invalid_data("compressed artifact offset overflow"))?;
        entries.push(ChunkEntry {
            offset,
            stored_len,
            raw_len: raw_len_u32,
            checksum: crc32(&raw[..raw_size]),
            flags,
        });
        remaining -= raw_size as u64;
    }
    if input.read(&mut [0; 1])? != 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "artifact changed while it was being packed",
        ));
    }

    let index_offset = output_offset;
    let index = encode_index(&entries)?;
    output.file_mut().write_all(&index)?;
    let index_checksum = crc32(&index);
    let mut header = encode_header(
        raw_len,
        chunks,
        options.chunk_size,
        index_offset,
        index.len() as u64,
        index_checksum,
    );
    let header_checksum = crc32(&header);
    header[HEADER_CRC_OFFSET..HEADER_CRC_OFFSET + 4]
        .copy_from_slice(&header_checksum.to_le_bytes());
    output.file_mut().seek(SeekFrom::Start(0))?;
    output.file_mut().write_all(&header)?;

    let packed_len = output.file_mut().metadata()?.len();
    if packed_len >= raw_len {
        return Ok(PackStats {
            original_bytes: raw_len,
            packed_bytes: raw_len,
            chunks,
            compressed: false,
        });
    }

    // Validate the temporary header and index before replacing the source. Payload checksums are
    // verified by readers when they decode each chunk.
    output.file_mut().flush()?;
    let container = parse_container_from_file(output.file_mut())?;
    let permissions = metadata.permissions();
    drop(input);
    let modified = metadata.modified()?;
    output.commit(path, Some(permissions), Some(modified))?;
    Ok(PackStats {
        original_bytes: container.raw_len,
        packed_bytes: container.packed_len,
        chunks: container.entries.len() as u64,
        compressed: true,
    })
}

/// Reads a complete raw or packed file into memory.
pub fn read_all(path: &Path) -> io::Result<Vec<u8>> {
    let mut file = File::open(path)?;
    if !has_magic(&mut file)? {
        file.seek(SeekFrom::Start(0))?;
        let mut output = Vec::new();
        file.read_to_end(&mut output)?;
        return Ok(output);
    }

    let container = parse_container_from_file(&mut file)?;
    let capacity = usize::try_from(container.raw_len)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "artifact is too large"))?;
    let mut output = allocate_zeroed(capacity, "decoded artifact is too large")?;
    if container.entries.is_empty() {
        return Ok(output);
    }

    let mut decompressor = ZstdDCtx::new()?;
    let mut compressed = Vec::new();
    file.seek(SeekFrom::Start(HEADER_LEN))?;
    let mut output_offset = 0;
    for entry in &container.entries {
        let end = output_offset + entry.raw_len as usize;
        decode_chunk_into(
            &mut file,
            *entry,
            &mut compressed,
            &mut decompressor,
            &mut output[output_offset..end],
        )?;
        output_offset = end;
    }
    if output_offset != capacity {
        return Err(invalid_data("decoded artifact length does not match header"));
    }
    Ok(output)
}

/// Reads `len` bytes beginning at `offset`, decompressing only chunks that overlap the range.
/// Their checksums are verified; chunks outside the requested range are not read or checked.
pub fn read_range(path: &Path, offset: u64, len: usize) -> io::Result<Vec<u8>> {
    let len_u64 = u64::try_from(len)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "range length is too large"))?;
    let end = offset
        .checked_add(len_u64)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "range overflows"))?;

    let mut file = File::open(path)?;
    if !has_magic(&mut file)? {
        let file_len = file.metadata()?.len();
        if end > file_len {
            return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "range exceeds file length"));
        }
        let mut output = Vec::new();
        output.try_reserve_exact(len).map_err(|_| invalid_data("requested range is too large"))?;
        output.resize(len, 0);
        file.seek(SeekFrom::Start(offset))?;
        file.read_exact(&mut output)?;
        return Ok(output);
    }

    let container = parse_container_from_file(&mut file)?;
    if end > container.raw_len {
        return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "range exceeds artifact length"));
    }
    if len == 0 {
        return Ok(Vec::new());
    }

    let mut output = Vec::new();
    output.try_reserve_exact(len).map_err(|_| invalid_data("requested range is too large"))?;
    let first_chunk = offset / container.chunk_size as u64;
    let last_chunk = (end - 1) / container.chunk_size as u64;
    let first_entry = container
        .entries
        .get(usize::try_from(first_chunk).unwrap())
        .ok_or_else(|| invalid_data("range points beyond chunk index"))?;
    file.seek(SeekFrom::Start(first_entry.offset))?;
    let mut decompressor = ZstdDCtx::new()?;
    let mut compressed = Vec::new();
    let mut raw = Vec::new();
    for chunk_index in first_chunk..=last_chunk {
        let entry = *container
            .entries
            .get(usize::try_from(chunk_index).unwrap())
            .ok_or_else(|| invalid_data("range points beyond chunk index"))?;
        raw.resize(entry.raw_len as usize, 0);
        decode_chunk_into(&mut file, entry, &mut compressed, &mut decompressor, &mut raw)?;
        let chunk_start = chunk_index * container.chunk_size as u64;
        let overlap_start = offset.max(chunk_start) - chunk_start;
        let overlap_end = end.min(chunk_start + entry.raw_len as u64) - chunk_start;
        output.extend_from_slice(&raw[overlap_start as usize..overlap_end as usize]);
    }
    if output.len() != len {
        return Err(invalid_data("decoded range length does not match request"));
    }
    Ok(output)
}

/// Streams a raw or packed source to an uncompressed destination.
///
/// The destination is written to a temporary sibling and replaced only after the full copy and
/// all chunk checksums succeed. If source and destination are the same path, the packed file is
/// replaced with its decoded bytes.
pub fn unpack_to(source: &Path, dest: &Path) -> io::Result<()> {
    let source_metadata = fs::metadata(source)?;
    let permissions = source_metadata.permissions();
    let modified = source_metadata.modified()?;
    let mut input = File::open(source)?;
    let compressed = has_magic(&mut input)?;
    let mut output = TempOutput::new(dest)?;
    if compressed {
        let container = parse_container_from_file(&mut input)?;
        input.seek(SeekFrom::Start(HEADER_LEN))?;
        let mut decompressor = ZstdDCtx::new()?;
        let mut compressed = Vec::new();
        let mut raw = Vec::new();
        for entry in &container.entries {
            raw.resize(entry.raw_len as usize, 0);
            decode_chunk_into(&mut input, *entry, &mut compressed, &mut decompressor, &mut raw)?;
            output.file_mut().write_all(&raw)?;
        }
    } else {
        input.seek(SeekFrom::Start(0))?;
        io::copy(&mut input, output.file_mut())?;
    }
    drop(input);
    output.commit(dest, Some(permissions), Some(modified))
}

#[cfg(test)]
fn parse_container(path: &Path) -> io::Result<Container> {
    let mut file = File::open(path)?;
    parse_container_from_file(&mut file)
}

fn parse_container_from_file(file: &mut File) -> io::Result<Container> {
    let packed_len = file.metadata()?.len();
    parse_container_reader(file, packed_len)
}

pub(super) fn parse_container_reader(
    file: &mut (impl Read + Seek),
    packed_len: u64,
) -> io::Result<Container> {
    if packed_len < HEADER_LEN {
        return Err(invalid_data("truncated compressed-artifact header"));
    }

    file.seek(SeekFrom::Start(0))?;
    let mut header = [0; HEADER_LEN as usize];
    file.read_exact(&mut header)?;
    if &header[..8] != MAGIC {
        return Err(invalid_data("invalid compressed-artifact magic"));
    }
    if read_u32(&header, 8)? != VERSION {
        return Err(invalid_data("unsupported compressed-artifact version"));
    }
    let chunk_size = read_u32(&header, 16)? as usize;
    if read_u32(&header, 12)? as u64 != HEADER_LEN || read_u32(&header, 20)? != 0 {
        return Err(invalid_data("invalid compressed-artifact header fields"));
    }
    if !valid_chunk_size(chunk_size) {
        return Err(invalid_data("unsupported compressed-artifact chunk size"));
    }

    let raw_len = read_u64(&header, 24)?;
    let count = read_u64(&header, 32)?;
    let index_offset = read_u64(&header, 40)?;
    let index_len = read_u64(&header, 48)?;
    let expected_header_crc = read_u32(&header, HEADER_CRC_OFFSET)?;
    let expected_index_crc = read_u32(&header, INDEX_CRC_OFFSET)?;
    let mut header_for_crc = header;
    header_for_crc[HEADER_CRC_OFFSET..HEADER_CRC_OFFSET + 4].fill(0);
    if crc32(&header_for_crc) != expected_header_crc {
        return Err(invalid_data("compressed-artifact header checksum mismatch"));
    }

    if count != chunk_count(raw_len, chunk_size)? {
        return Err(invalid_data("compressed-artifact chunk count is inconsistent"));
    }
    let expected_index_len = count
        .checked_mul(INDEX_ENTRY_LEN)
        .ok_or_else(|| invalid_data("compressed-artifact index length overflows"))?;
    if index_len != expected_index_len {
        return Err(invalid_data("compressed-artifact index length is inconsistent"));
    }
    if index_offset < HEADER_LEN
        || index_offset.checked_add(index_len) != Some(packed_len)
        || index_offset > packed_len
    {
        return Err(invalid_data("compressed-artifact index is outside the file"));
    }

    let index_size = usize::try_from(index_len)
        .map_err(|_| invalid_data("compressed-artifact index is too large"))?;
    file.seek(SeekFrom::Start(index_offset))?;
    let mut index = Vec::new();
    index
        .try_reserve_exact(index_size)
        .map_err(|_| invalid_data("compressed-artifact index is too large"))?;
    index.resize(index_size, 0);
    file.read_exact(&mut index)?;
    if crc32(&index) != expected_index_crc {
        return Err(invalid_data("compressed-artifact index checksum mismatch"));
    }

    let count_usize = usize::try_from(count)
        .map_err(|_| invalid_data("compressed-artifact has too many chunks"))?;
    let mut entries = Vec::new();
    entries
        .try_reserve_exact(count_usize)
        .map_err(|_| invalid_data("compressed-artifact chunk index is too large"))?;
    let mut next_offset = HEADER_LEN;
    let mut raw_sum = 0u64;
    for bytes in index.chunks_exact(INDEX_ENTRY_LEN as usize) {
        let entry = ChunkEntry {
            offset: read_u64(bytes, 0)?,
            stored_len: read_u32(bytes, 8)?,
            raw_len: read_u32(bytes, 12)?,
            checksum: read_u32(bytes, 16)?,
            flags: read_u32(bytes, 20)?,
        };
        let expected_raw_len = (raw_len - raw_sum).min(chunk_size as u64) as u32;
        if entry.offset != next_offset || entry.raw_len != expected_raw_len || entry.stored_len == 0
        {
            return Err(invalid_data("invalid compressed-artifact chunk bounds"));
        }
        match entry.flags {
            RAW_CHUNK if entry.stored_len == entry.raw_len => {}
            0 if entry.stored_len < entry.raw_len => {}
            _ => return Err(invalid_data("invalid compressed-artifact chunk flags or lengths")),
        }
        next_offset = next_offset
            .checked_add(entry.stored_len as u64)
            .ok_or_else(|| invalid_data("compressed-artifact chunk offset overflows"))?;
        raw_sum = raw_sum
            .checked_add(entry.raw_len as u64)
            .ok_or_else(|| invalid_data("compressed-artifact raw length overflows"))?;
        if next_offset > index_offset {
            return Err(invalid_data("compressed-artifact chunk overlaps its index"));
        }
        entries.push(entry);
    }
    if next_offset != index_offset || raw_sum != raw_len {
        return Err(invalid_data("compressed-artifact chunk index does not cover payload"));
    }

    Ok(Container { raw_len, chunk_size, entries, packed_len })
}

pub(super) fn decode_chunk_into(
    file: &mut impl Read,
    entry: ChunkEntry,
    compressed: &mut Vec<u8>,
    decompressor: &mut ZstdDCtx,
    raw: &mut [u8],
) -> io::Result<()> {
    if raw.len() != entry.raw_len as usize {
        return Err(invalid_data("decoded chunk buffer has an unexpected length"));
    }
    if entry.flags == RAW_CHUNK {
        file.read_exact(raw)?;
    } else {
        compressed.clear();
        compressed
            .try_reserve(entry.stored_len as usize)
            .map_err(|_| invalid_data("compressed chunk is too large"))?;
        compressed.resize(entry.stored_len as usize, 0);
        file.read_exact(compressed)?;
        decompressor.decompress(compressed, raw)?;
    }
    if crc32(raw) != entry.checksum {
        return Err(invalid_data("compressed-artifact chunk checksum mismatch"));
    }
    Ok(())
}

fn chunk_count(raw_len: u64, chunk_size: usize) -> io::Result<u64> {
    if raw_len == 0 {
        return Ok(0);
    }
    raw_len
        .checked_add(chunk_size as u64 - 1)
        .map(|n| n / chunk_size as u64)
        .ok_or_else(|| invalid_data("artifact chunk count overflows"))
}

fn encode_header(
    raw_len: u64,
    count: u64,
    chunk_size: usize,
    index_offset: u64,
    index_len: u64,
    index_crc: u32,
) -> [u8; HEADER_LEN as usize] {
    let mut header = [0; HEADER_LEN as usize];
    header[..8].copy_from_slice(MAGIC);
    put_u32(&mut header, 8, VERSION);
    put_u32(&mut header, 12, HEADER_LEN as u32);
    put_u32(&mut header, 16, chunk_size as u32);
    put_u32(&mut header, 20, 0);
    put_u64(&mut header, 24, raw_len);
    put_u64(&mut header, 32, count);
    put_u64(&mut header, 40, index_offset);
    put_u64(&mut header, 48, index_len);
    put_u32(&mut header, HEADER_CRC_OFFSET, 0);
    put_u32(&mut header, INDEX_CRC_OFFSET, index_crc);
    header
}

fn check_zstd(result: usize) -> io::Result<()> {
    if unsafe { ZSTD_isError(result) } == 0 {
        return Ok(());
    }
    let message = unsafe {
        let name = ZSTD_getErrorName(result);
        if name.is_null() {
            "unknown Zstandard error".to_owned()
        } else {
            CStr::from_ptr(name).to_string_lossy().into_owned()
        }
    };
    Err(io::Error::new(io::ErrorKind::InvalidData, format!("Zstandard error: {message}")))
}

fn allocate_zeroed(len: usize, message: &'static str) -> io::Result<Vec<u8>> {
    let mut output = Vec::new();
    output.try_reserve_exact(len).map_err(|_| invalid_data(message))?;
    output.resize(len, 0);
    Ok(output)
}

fn valid_chunk_size(chunk_size: usize) -> bool {
    (MIN_CHUNK_SIZE..=MAX_CHUNK_SIZE).contains(&chunk_size) && chunk_size.is_power_of_two()
}

fn validate_options(options: CompressionOptions) -> io::Result<()> {
    if !valid_chunk_size(options.chunk_size) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "artifact chunk size must be a power of two from 16 KiB to 4 MiB",
        ));
    }
    Ok(())
}

struct ZstdCCtx(*mut c_void);

impl ZstdCCtx {
    fn new() -> io::Result<Self> {
        let context = unsafe { ZSTD_createCCtx() };
        if context.is_null() {
            Err(io::Error::new(
                io::ErrorKind::Other,
                "could not allocate Zstandard compression context",
            ))
        } else {
            Ok(Self(context))
        }
    }

    fn compress(&mut self, input: &[u8], output: &mut [u8], level: i32) -> io::Result<usize> {
        let result = unsafe {
            ZSTD_compressCCtx(
                self.0,
                output.as_mut_ptr().cast(),
                output.len(),
                input.as_ptr().cast(),
                input.len(),
                level as c_int,
            )
        };
        check_zstd(result)?;
        Ok(result)
    }
}

impl Drop for ZstdCCtx {
    fn drop(&mut self) {
        unsafe {
            ZSTD_freeCCtx(self.0);
        }
    }
}

pub(super) struct ZstdDCtx(*mut c_void);

impl ZstdDCtx {
    pub(super) fn new() -> io::Result<Self> {
        let context = unsafe { ZSTD_createDCtx() };
        if context.is_null() {
            Err(io::Error::new(
                io::ErrorKind::Other,
                "could not allocate Zstandard decompression context",
            ))
        } else {
            Ok(Self(context))
        }
    }

    fn decompress(&mut self, input: &[u8], output: &mut [u8]) -> io::Result<()> {
        let result = unsafe {
            ZSTD_decompressDCtx(
                self.0,
                output.as_mut_ptr().cast(),
                output.len(),
                input.as_ptr().cast(),
                input.len(),
            )
        };
        check_zstd(result)?;
        if result != output.len() {
            return Err(invalid_data("Zstandard frame has an unexpected decoded length"));
        }
        Ok(())
    }
}

impl Drop for ZstdDCtx {
    fn drop(&mut self) {
        unsafe {
            ZSTD_freeDCtx(self.0);
        }
    }
}

fn encode_index(entries: &[ChunkEntry]) -> io::Result<Vec<u8>> {
    let capacity = entries
        .len()
        .checked_mul(INDEX_ENTRY_LEN as usize)
        .ok_or_else(|| invalid_data("compressed-artifact index length overflows"))?;
    let mut index = Vec::new();
    index
        .try_reserve_exact(capacity)
        .map_err(|_| invalid_data("compressed-artifact index is too large"))?;
    for entry in entries {
        index.extend_from_slice(&entry.offset.to_le_bytes());
        index.extend_from_slice(&entry.stored_len.to_le_bytes());
        index.extend_from_slice(&entry.raw_len.to_le_bytes());
        index.extend_from_slice(&entry.checksum.to_le_bytes());
        index.extend_from_slice(&entry.flags.to_le_bytes());
    }
    Ok(index)
}

fn read_u32(bytes: &[u8], offset: usize) -> io::Result<u32> {
    let bytes = bytes
        .get(offset..offset + 4)
        .ok_or_else(|| invalid_data("truncated compressed-artifact field"))?;
    Ok(u32::from_le_bytes(bytes.try_into().unwrap()))
}

fn read_u64(bytes: &[u8], offset: usize) -> io::Result<u64> {
    let bytes = bytes
        .get(offset..offset + 8)
        .ok_or_else(|| invalid_data("truncated compressed-artifact field"))?;
    Ok(u64::from_le_bytes(bytes.try_into().unwrap()))
}

fn put_u32(bytes: &mut [u8], offset: usize, value: u32) {
    bytes[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
}

fn put_u64(bytes: &mut [u8], offset: usize, value: u64) {
    bytes[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
}

const fn make_crc32_tables() -> [[u32; 256]; 8] {
    let mut tables = [[0; 256]; 8];
    let mut i = 0;
    while i < 256 {
        let mut value = i as u32;
        let mut bit = 0;
        while bit < 8 {
            value = if value & 1 == 1 { (value >> 1) ^ 0xedb8_8320 } else { value >> 1 };
            bit += 1;
        }
        tables[0][i] = value;
        i += 1;
    }

    let mut slice = 1;
    while slice < 8 {
        let mut i = 0;
        while i < 256 {
            let previous = tables[slice - 1][i];
            tables[slice][i] = tables[0][(previous & 0xff) as usize] ^ (previous >> 8);
            i += 1;
        }
        slice += 1;
    }

    tables
}

const CRC32_TABLES: [[u32; 256]; 8] = make_crc32_tables();

#[cfg(target_arch = "aarch64")]
static CRC32_IMPLEMENTATION: OnceLock<fn(&[u8]) -> u32> = OnceLock::new();

#[cfg(target_arch = "aarch64")]
fn crc32(bytes: &[u8]) -> u32 {
    let implementation = CRC32_IMPLEMENTATION.get_or_init(|| {
        if std::arch::is_aarch64_feature_detected!("crc") {
            crc32_aarch64_dispatch
        } else {
            crc32_slicing_by_eight
        }
    });
    implementation(bytes)
}

#[cfg(not(target_arch = "aarch64"))]
fn crc32(bytes: &[u8]) -> u32 {
    crc32_slicing_by_eight(bytes)
}

#[cfg(target_arch = "aarch64")]
fn crc32_aarch64_dispatch(bytes: &[u8]) -> u32 {
    // The function is selected only after runtime detection confirms the CPU feature.
    unsafe { crc32_aarch64(bytes) }
}

#[cfg(target_arch = "aarch64")]
#[target_feature(enable = "crc")]
unsafe fn crc32_aarch64(bytes: &[u8]) -> u32 {
    use std::arch::aarch64::{__crc32b, __crc32d, __crc32h, __crc32w};

    let mut crc = !0u32;
    let mut offset = 0;
    while bytes.len() - offset >= 8 {
        let word = u64::from_le_bytes(bytes[offset..offset + 8].try_into().unwrap());
        crc = __crc32d(crc, word);
        offset += 8;
    }
    if bytes.len() - offset >= 4 {
        let word = u32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap());
        crc = __crc32w(crc, word);
        offset += 4;
    }
    if bytes.len() - offset >= 2 {
        let half = u16::from_le_bytes(bytes[offset..offset + 2].try_into().unwrap());
        crc = __crc32h(crc, half);
        offset += 2;
    }
    if offset < bytes.len() {
        crc = __crc32b(crc, bytes[offset]);
    }
    !crc
}

fn crc32_slicing_by_eight(mut bytes: &[u8]) -> u32 {
    let mut crc = !0u32;
    while bytes.len() >= 8 {
        let first = u32::from_le_bytes(bytes[..4].try_into().unwrap()) ^ crc;
        crc = CRC32_TABLES[7][(first & 0xff) as usize]
            ^ CRC32_TABLES[6][((first >> 8) & 0xff) as usize]
            ^ CRC32_TABLES[5][((first >> 16) & 0xff) as usize]
            ^ CRC32_TABLES[4][((first >> 24) & 0xff) as usize]
            ^ CRC32_TABLES[3][bytes[4] as usize]
            ^ CRC32_TABLES[2][bytes[5] as usize]
            ^ CRC32_TABLES[1][bytes[6] as usize]
            ^ CRC32_TABLES[0][bytes[7] as usize];
        bytes = &bytes[8..];
    }
    for byte in bytes {
        let index = ((crc ^ u32::from(*byte)) & 0xff) as usize;
        crc = CRC32_TABLES[0][index] ^ (crc >> 8);
    }
    !crc
}

fn has_magic(file: &mut File) -> io::Result<bool> {
    file.seek(SeekFrom::Start(0))?;
    let mut magic = [0; MAGIC.len()];
    let mut read = 0;
    while read < magic.len() {
        match file.read(&mut magic[read..])? {
            0 => return Ok(false),
            n => read += n,
        }
    }
    Ok(&magic == MAGIC)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

struct TempOutput {
    path: PathBuf,
    file: Option<File>,
    committed: bool,
}

impl TempOutput {
    fn new(destination: &Path) -> io::Result<Self> {
        let parent =
            destination.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or(Path::new("."));
        let name = destination.file_name().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "destination has no file name")
        })?;
        for _ in 0..128 {
            let id = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
            let temp_path = parent.join(format!(
                ".{}.artifact-compression-{}-{id}.tmp",
                name.to_string_lossy(),
                std::process::id()
            ));
            match OpenOptions::new().write(true).read(true).create_new(true).open(&temp_path) {
                Ok(file) => {
                    return Ok(Self { path: temp_path, file: Some(file), committed: false });
                }
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error),
            }
        }
        Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "could not allocate temporary artifact file",
        ))
    }

    fn file_mut(&mut self) -> &mut File {
        self.file.as_mut().expect("temporary output file already committed")
    }

    fn commit(
        mut self,
        destination: &Path,
        permissions: Option<Permissions>,
        modified: Option<std::time::SystemTime>,
    ) -> io::Result<()> {
        let mut file = self.file.take().expect("temporary output file already committed");
        file.flush()?;
        if let Some(permissions) = permissions {
            file.set_permissions(permissions)?;
        }
        if let Some(modified) = modified {
            file.set_times(FileTimes::new().set_modified(modified))?;
        }
        drop(file);
        replace_file(&self.path, destination)?;
        self.committed = true;
        Ok(())
    }
}

impl Drop for TempOutput {
    fn drop(&mut self) {
        if !self.committed {
            self.file.take();
            let _ = fs::remove_file(&self.path);
        }
    }
}

#[cfg(not(windows))]
fn replace_file(source: &Path, destination: &Path) -> io::Result<()> {
    fs::rename(source, destination)
}

#[cfg(windows)]
fn replace_file(source: &Path, destination: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn MoveFileExW(existing: *const u16, new: *const u16, flags: u32) -> i32;
    }

    const MOVEFILE_REPLACE_EXISTING: u32 = 0x1;
    let existing = source.as_os_str().encode_wide().chain(Some(0)).collect::<Vec<_>>();
    let new = destination.as_os_str().encode_wide().chain(Some(0)).collect::<Vec<_>>();
    let result = unsafe { MoveFileExW(existing.as_ptr(), new.as_ptr(), MOVEFILE_REPLACE_EXISTING) };
    if result == 0 { Err(io::Error::last_os_error()) } else { Ok(()) }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicU64, Ordering};

    use super::*;

    static TEST_COUNTER: AtomicU64 = AtomicU64::new(0);

    struct TestFile(PathBuf);

    impl TestFile {
        fn new(bytes: &[u8]) -> Self {
            let id = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir()
                .join(format!("rust-artifact-compression-test-{}-{id}.bin", std::process::id()));
            fs::write(&path, bytes).unwrap();
            Self(path)
        }
    }

    impl Drop for TestFile {
        fn drop(&mut self) {
            let _ = fs::remove_file(&self.0);
        }
    }

    fn repetitive_bytes(len: usize) -> Vec<u8> {
        (0..len).map(|i| b'a' + ((i / 1024) % 5) as u8).collect()
    }

    #[test]
    fn crc32_slicing_by_eight_matches_reference_for_tails_and_alignments() {
        assert_eq!(crc32(b"123456789"), 0xcbf4_3926);

        let mut state = 0x1234_5678u32;
        let bytes = (0..DEFAULT_CHUNK_SIZE + 8)
            .map(|_| {
                state ^= state << 13;
                state ^= state >> 17;
                state ^= state << 5;
                state as u8
            })
            .collect::<Vec<_>>();
        let lengths = [
            0,
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            15,
            16,
            17,
            31,
            32,
            33,
            63,
            64,
            65,
            255,
            256,
            257,
            DEFAULT_CHUNK_SIZE - 1,
            DEFAULT_CHUNK_SIZE,
            DEFAULT_CHUNK_SIZE + 1,
        ];
        for offset in 0..8 {
            for len in lengths {
                let slice = &bytes[offset..offset + len];
                assert_eq!(crc32(slice), crc32_bytewise(slice), "offset={offset}, len={len}");
            }
        }
    }

    #[cfg(target_arch = "aarch64")]
    #[test]
    fn crc32_hardware_matches_portable_when_supported() {
        if !std::arch::is_aarch64_feature_detected!("crc") {
            return;
        }
        let mut state = 0xa5a5_1234u32;
        let bytes = (0..DEFAULT_CHUNK_SIZE + 17)
            .map(|_| {
                state ^= state << 13;
                state ^= state >> 17;
                state ^= state << 5;
                state as u8
            })
            .collect::<Vec<_>>();
        for offset in 0..8 {
            for len in [0, 1, 2, 3, 4, 7, 8, 9, 31, 1024, DEFAULT_CHUNK_SIZE + 1] {
                let slice = &bytes[offset..offset + len];
                assert_eq!(
                    unsafe { crc32_aarch64(slice) },
                    crc32_slicing_by_eight(slice),
                    "offset={offset}, len={len}"
                );
            }
        }
    }

    fn crc32_bytewise(bytes: &[u8]) -> u32 {
        let mut crc = !0u32;
        for byte in bytes {
            let index = ((crc ^ u32::from(*byte)) & 0xff) as usize;
            crc = CRC32_TABLES[0][index] ^ (crc >> 8);
        }
        !crc
    }

    #[test]
    fn round_trip_pack_and_unpack() {
        let bytes = repetitive_bytes(DEFAULT_CHUNK_SIZE * 3 + 17);
        let source = TestFile::new(&bytes);
        let destination = TestFile::new(b"old destination");

        let stats = pack(&source.0).unwrap();
        assert!(stats.compressed);
        assert_eq!(stats.original_bytes, bytes.len() as u64);
        assert!(stats.packed_bytes < stats.original_bytes);
        assert!(is_compressed(&source.0).unwrap());
        assert_eq!(read_all(&source.0).unwrap(), bytes);

        unpack_to(&source.0, &destination.0).unwrap();
        assert_eq!(fs::read(&destination.0).unwrap(), bytes);
        assert!(is_compressed(&source.0).unwrap());
        assert_eq!(pack(&source.0).unwrap(), stats);
    }

    #[test]
    fn configurable_chunks_round_trip_and_preserve_range_reads() {
        let chunk_size = 256 * 1024;
        let bytes = repetitive_bytes(chunk_size * 2 + 19);
        let file = TestFile::new(&bytes);
        let options = CompressionOptions { level: 3, chunk_size };

        let stats = pack_with_options(&file.0, options).unwrap();
        assert!(stats.compressed);
        let container = parse_container(&file.0).unwrap();
        assert_eq!(container.chunk_size, chunk_size);
        assert_eq!(container.entries.len(), 3);
        assert_eq!(read_all(&file.0).unwrap(), bytes);
        for (offset, len) in [
            (0, 9),
            ((chunk_size - 3) as u64, 11),
            ((chunk_size + 41) as u64, 999),
            ((bytes.len() - 7) as u64, 7),
        ] {
            assert_eq!(
                read_range(&file.0, offset, len).unwrap(),
                bytes[offset as usize..offset as usize + len]
            );
        }

        let destination = TestFile::new(b"old destination");
        unpack_to(&file.0, &destination.0).unwrap();
        assert_eq!(fs::read(&destination.0).unwrap(), bytes);
        assert_eq!(pack_with_options(&file.0, CompressionOptions::default()).unwrap(), stats);
    }

    #[test]
    fn chunk_size_options_are_validated_before_writing() {
        let file = TestFile::new(&repetitive_bytes(DEFAULT_CHUNK_SIZE * 2));
        let original = fs::read(&file.0).unwrap();
        for chunk_size in [0, 8 * 1024, 24 * 1024, MAX_CHUNK_SIZE * 2] {
            let error = pack_with_options(&file.0, CompressionOptions { level: 3, chunk_size })
                .unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
            assert_eq!(fs::read(&file.0).unwrap(), original);
        }
    }

    #[test]
    fn files_no_larger_than_container_overhead_skip_compression() {
        for len in [0, MINIMUM_PACKABLE_SIZE as usize] {
            let bytes = repetitive_bytes(len);
            let file = TestFile::new(&bytes);
            let stats = pack(&file.0).unwrap();
            assert!(!stats.compressed);
            assert_eq!(stats.original_bytes, len as u64);
            assert_eq!(fs::read(&file.0).unwrap(), bytes);
        }
    }

    #[test]
    fn range_reads_only_the_requested_bytes() {
        let bytes = repetitive_bytes(DEFAULT_CHUNK_SIZE * 4 + 91);
        let file = TestFile::new(&bytes);
        assert!(pack(&file.0).unwrap().compressed);

        for (offset, len) in [
            (0, 1),
            ((DEFAULT_CHUNK_SIZE - 2) as u64, 9),
            ((DEFAULT_CHUNK_SIZE * 2 + 101) as u64, 700),
            ((bytes.len() - 33) as u64, 33),
            (bytes.len() as u64, 0),
        ] {
            assert_eq!(
                read_range(&file.0, offset, len).unwrap(),
                bytes[offset as usize..offset as usize + len]
            );
        }
    }

    #[test]
    fn mixed_chunks_use_raw_fallback_and_round_trip() {
        let mut bytes = Vec::with_capacity(DEFAULT_CHUNK_SIZE * 2);
        let mut state = 0x1234_5678u32;
        for _ in 0..DEFAULT_CHUNK_SIZE {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            bytes.push(state as u8);
        }
        bytes.extend(std::iter::repeat_n(b'R', DEFAULT_CHUNK_SIZE));
        let file = TestFile::new(&bytes);

        let stats = pack(&file.0).unwrap();
        assert!(stats.compressed);
        let container = parse_container(&file.0).unwrap();
        assert_eq!(container.entries.len(), 2);
        assert_eq!(container.entries[0].flags, RAW_CHUNK);
        assert_eq!(container.entries[1].flags, 0);
        assert_eq!(read_all(&file.0).unwrap(), bytes);
    }

    #[test]
    fn range_reads_validate_only_chunks_they_touch() {
        let bytes = repetitive_bytes(DEFAULT_CHUNK_SIZE * 2);
        let file = TestFile::new(&bytes);
        assert!(pack(&file.0).unwrap().compressed);

        flip_byte(&file.0, HEADER_LEN + 1);
        assert_eq!(
            read_range(&file.0, DEFAULT_CHUNK_SIZE as u64, 1).unwrap(),
            bytes[DEFAULT_CHUNK_SIZE..DEFAULT_CHUNK_SIZE + 1]
        );
        assert_eq!(read_range(&file.0, 0, 1).unwrap_err().kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn malformed_truncated_and_corrupt_files_are_rejected() {
        let original = repetitive_bytes(DEFAULT_CHUNK_SIZE * 2);
        let truncated = TestFile::new(&original);
        assert!(pack(&truncated.0).unwrap().compressed);
        let length = fs::metadata(&truncated.0).unwrap().len();
        OpenOptions::new().write(true).open(&truncated.0).unwrap().set_len(length - 3).unwrap();
        assert_eq!(read_all(&truncated.0).unwrap_err().kind(), io::ErrorKind::InvalidData);

        let header_corrupt = TestFile::new(&original);
        pack(&header_corrupt.0).unwrap();
        flip_byte(&header_corrupt.0, 24);
        assert_eq!(read_all(&header_corrupt.0).unwrap_err().kind(), io::ErrorKind::InvalidData);

        let chunk_corrupt = TestFile::new(&original);
        pack(&chunk_corrupt.0).unwrap();
        flip_byte(&chunk_corrupt.0, HEADER_LEN + 1);
        assert_eq!(read_all(&chunk_corrupt.0).unwrap_err().kind(), io::ErrorKind::InvalidData);

        let length_corrupt = TestFile::new(&original);
        pack(&length_corrupt.0).unwrap();
        let mut header = [0; HEADER_LEN as usize];
        let mut file = OpenOptions::new().read(true).write(true).open(&length_corrupt.0).unwrap();
        file.read_exact(&mut header).unwrap();
        put_u64(&mut header, 24, 1);
        put_u32(&mut header, HEADER_CRC_OFFSET, 0);
        let header_checksum = crc32(&header);
        put_u32(&mut header, HEADER_CRC_OFFSET, header_checksum);
        file.seek(SeekFrom::Start(0)).unwrap();
        file.write_all(&header).unwrap();
        assert_eq!(read_all(&length_corrupt.0).unwrap_err().kind(), io::ErrorKind::InvalidData);

        let index_corrupt = TestFile::new(&original);
        pack(&index_corrupt.0).unwrap();
        let last_byte = fs::metadata(&index_corrupt.0).unwrap().len() - 1;
        flip_byte(&index_corrupt.0, last_byte);
        assert_eq!(read_all(&index_corrupt.0).unwrap_err().kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn no_savings_leave_original_file_untouched() {
        let mut state = 0x8765_4321u32;
        let bytes = (0..DEFAULT_CHUNK_SIZE)
            .map(|_| {
                state ^= state << 13;
                state ^= state >> 17;
                state ^= state << 5;
                state as u8
            })
            .collect::<Vec<_>>();
        let file = TestFile::new(&bytes);
        let stats = pack(&file.0).unwrap();
        assert!(!stats.compressed);
        assert_eq!(stats.original_bytes, bytes.len() as u64);
        assert_eq!(fs::read(&file.0).unwrap(), bytes);
    }

    #[test]
    fn packing_and_unpacking_preserve_modification_time() {
        let bytes = repetitive_bytes(DEFAULT_CHUNK_SIZE * 2);
        let packed = TestFile::new(&bytes);
        let unpacked = TestFile::new(b"old");
        let expected = std::time::UNIX_EPOCH + std::time::Duration::from_secs(1_234_567_890);
        File::options()
            .write(true)
            .open(&packed.0)
            .unwrap()
            .set_times(FileTimes::new().set_modified(expected))
            .unwrap();

        assert!(pack(&packed.0).unwrap().compressed);
        assert_eq!(fs::metadata(&packed.0).unwrap().modified().unwrap(), expected);
        unpack_to(&packed.0, &unpacked.0).unwrap();
        assert_eq!(fs::metadata(&unpacked.0).unwrap().modified().unwrap(), expected);
    }

    fn flip_byte(path: &Path, offset: u64) {
        let mut file = OpenOptions::new().read(true).write(true).open(path).unwrap();
        file.seek(SeekFrom::Start(offset)).unwrap();
        let mut byte = [0];
        file.read_exact(&mut byte).unwrap();
        byte[0] ^= 0x80;
        file.seek(SeekFrom::Start(offset)).unwrap();
        file.write_all(&byte).unwrap();
    }
}
