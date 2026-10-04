//! Demand-read compressed compiler metadata.

use std::io;
use std::sync::OnceLock;

use crate::artifact_compression::{
    Container, MAGIC, ZstdDCtx, decode_chunk_into, parse_container_reader,
};
use crate::marker::IntoDynSyncSend;

/// A mapped compressed artifact with independently pinned decoded chunks. This
/// preserves borrowed strings while avoiding whole-file inflation for metadata.
pub struct IndexedArtifact {
    source: crate::owned_slice::OwnedSlice,
    container: Container,
    chunks: Vec<OnceLock<Result<Box<[u8]>, String>>>,
    joined: IntoDynSyncSend<elsa::sync::FrozenMap<(usize, usize), Box<[u8]>>>,
}

impl IndexedArtifact {
    pub fn from_slice(source: crate::owned_slice::OwnedSlice) -> io::Result<Option<Self>> {
        if !source.starts_with(MAGIC) {
            return Ok(None);
        }
        let container =
            parse_container_reader(&mut io::Cursor::new(&*source), source.len() as u64)?;
        let chunks = (0..container.entries.len()).map(|_| OnceLock::new()).collect();
        Ok(Some(Self {
            source,
            container,
            chunks,
            joined: IntoDynSyncSend(elsa::sync::FrozenMap::new()),
        }))
    }

    fn chunk(&self, index: usize) -> &[u8] {
        self.chunks[index]
            .get_or_init(|| {
                let entry = self.container.entries[index];
                let mut raw = vec![0; entry.raw_len as usize];
                let mut source = &self.source[entry.offset as usize..][..entry.stored_len as usize];
                let result = (|| {
                    decode_chunk_into(
                        &mut source,
                        entry,
                        &mut Vec::new(),
                        &mut ZstdDCtx::new()?,
                        &mut raw,
                    )?;
                    Ok::<_, io::Error>(raw.into_boxed_slice())
                })();
                result.map_err(|e| e.to_string())
            })
            .as_ref()
            .unwrap_or_else(|e| panic!("corrupt compressed metadata: {e}"))
            .as_ref()
    }

    pub fn range(&self, offset: usize, len: usize) -> &[u8] {
        assert!(offset.checked_add(len).is_some_and(|end| end <= self.container.raw_len as usize));
        if len == 0 {
            return &[];
        }
        let size = self.container.chunk_size;
        let first = offset / size;
        let last = (offset + len - 1) / size;
        if first == last {
            return &self.chunk(first)[offset % size..][..len];
        }
        if let Some(bytes) = self.joined.get(&(offset, len)) {
            return bytes;
        }
        let mut bytes = Vec::with_capacity(len);
        for index in first..=last {
            let chunk = self.chunk(index);
            let start = offset.saturating_sub(index * size);
            let end = (offset + len - index * size).min(chunk.len());
            bytes.extend_from_slice(&chunk[start..end]);
        }
        self.joined.insert((offset, len), bytes.into_boxed_slice())
    }
}

impl rustc_serialize::opaque::DecoderSource for IndexedArtifact {
    fn len(&self) -> usize {
        self.container.raw_len as usize
    }
    fn read_at(&self, offset: usize, len: usize) -> &[u8] {
        self.range(offset, len)
    }
    fn window(&self, offset: usize) -> (usize, &[u8]) {
        if offset == self.container.raw_len as usize {
            return (offset, &[]);
        }
        let index = offset / self.container.chunk_size;
        (index * self.container.chunk_size, self.chunk(index))
    }
}

impl Drop for IndexedArtifact {
    fn drop(&mut self) {
        if std::env::var_os("RUSTC_COMPACT_STATS").is_some() {
            let read = self.chunks.iter().filter(|c| c.get().is_some()).count();
            let bytes: usize = self
                .chunks
                .iter()
                .zip(&self.container.entries)
                .filter(|(c, _)| c.get().is_some())
                .map(|(_, e)| e.raw_len as usize)
                .sum();
            eprintln!(
                "RUSTC_COMPACT_METADATA {{\"logical_bytes\":{},\"decoded_bytes\":{},\"chunks\":{},\"read_chunks\":{}}}",
                self.container.raw_len,
                bytes,
                self.chunks.len(),
                read
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::artifact_compression::{CompressionOptions, pack_with_options};
    use crate::owned_slice::slice_owned;
    #[test]
    fn decodes_only_requested_chunks_and_pins_cross_chunk_ranges() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("metadata");
        let raw: Vec<u8> = (0..65536).map(|i| (i % 251) as u8).collect();
        std::fs::write(&path, &raw).unwrap();
        pack_with_options(&path, CompressionOptions { level: 3, chunk_size: 16384 }).unwrap();
        let source = slice_owned(std::fs::read(&path).unwrap(), |x| &x[..]);
        let reader = IndexedArtifact::from_slice(source).unwrap().unwrap();
        assert_eq!(reader.chunks.iter().filter(|c| c.get().is_some()).count(), 0);
        assert_eq!(reader.range(100, 20), &raw[100..120]);
        assert_eq!(reader.chunks.iter().filter(|c| c.get().is_some()).count(), 1);
        let crossing = reader.range(16380, 20);
        assert_eq!(crossing, &raw[16380..16400]);
        assert!(std::ptr::eq(crossing, reader.range(16380, 20)));
        assert_eq!(reader.chunks.iter().filter(|c| c.get().is_some()).count(), 2);
    }
}
