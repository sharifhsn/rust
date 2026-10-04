//! The current session is recorded as a bounded sequential spool, so worker threads
//! do not need to retain the whole graph. At finish, the spool is rewritten into
//! stable-ID pages. Each page has a local fingerprint dictionary, compact edge
//! encoding, checksum, and optional compression; a small manifest names the live
//! pages. Unchanged pages are reused across incremental sessions, and holes left by
//! removed nodes can be reclaimed or compacted.
//!
//! The page layout reduces persistent bytes and rewrite work. Loading the previous
//! graph reads only the manifest of page descriptors; pages are decoded on first access
//! and stay cached until the session ends. The reverse lookup from a `DepNode` to its
//! stable ID is built in memory from the pages on first use rather than persisted, since a
//! recompiling crate loads every page anyway. Graph access returns copied nodes and edge
//! iterators that own a shared backing buffer, so callers are not tied to the graph's
//! lifetime.
//!
//! `SerializedNodeHeader` remains the fixed-size record format for the temporary
//! spool. Dep-graph indices are bulk allocated to workers while recording so they
//! do not contend on one shared counter.

use std::cell::RefCell;
use std::cmp::max;
use std::fs::{self, File};
use std::hash::Hasher;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::sync::{Arc, OnceLock};
use std::{iter, mem};

use rustc_data_structures::artifact_compression::{self, CompressionOptions};
use rustc_data_structures::fingerprint::{Fingerprint, PackedFingerprint};
use rustc_data_structures::fx::{FxHashMap, FxHashSet};
use rustc_data_structures::memmap::Mmap;
use rustc_data_structures::outline;
use rustc_data_structures::profiling::SelfProfilerRef;
use rustc_data_structures::stable_hash::StableHasher;
use rustc_data_structures::sync::{AtomicU64, Lock, WorkerLocal, broadcast};
use rustc_serialize::opaque::mem_encoder::MemEncoder;
use rustc_serialize::opaque::{
    FileEncodeResult, FileEncoder, IntEncodedWithFixedSize, MAGIC_END_BYTES, MemDecoder,
};
use rustc_serialize::{Decodable, Decoder, Encodable, Encoder};
use rustc_session::Session;
use tracing::{debug, instrument, warn};

use super::graph::{CurrentDepGraph, DepNodeColorMap, DesiredColor, TrySetColorResult};
use super::retained::RetainedDepGraph;
use super::{DepKind, DepNode, DepNodeIndex};

// The maximum value of `SerializedDepNodeIndex` leaves the upper two bits
// unused so that we can store multiple index types in `CompressedHybridIndex`,
// and use those bits to encode which index type it contains.
rustc_index::newtype_index! {
    #[encodable]
    #[max = 0x7FFF_FFFF]
    pub struct SerializedDepNodeIndex {}
}

const DEP_NODE_SIZE: usize = size_of::<SerializedDepNodeIndex>();
#[inline]
fn mask(bits: usize) -> usize {
    usize::MAX >> ((size_of::<usize>() * 8) - bits)
}
/// Number of bits we need to store the number of used bytes in a SerializedDepNodeIndex.
/// Note that wherever we encode byte widths like this we actually store the number of bytes used
/// minus 1; for a 4-byte value we technically would have 5 widths to store, but using one byte to
/// store zeroes (which are relatively rare) is a decent tradeoff to save a bit in our bitfields.
const DEP_NODE_WIDTH_BITS: usize = DEP_NODE_SIZE / 2;

/// Data for use when recompiling the **current crate**.
///
/// There may be unused indices with DepKind::Null in this graph due to batch allocation of
/// indices to threads.
#[derive(Default)]
pub struct SerializedDepGraph {
    /// Stable-ID pages, decoded on first access and cached for the session.
    page_store: Option<Arc<GraphPageStore>>,
    node_max: usize,
    node_count: usize,
    /// Maps a [`DepNode`] back to its [`SerializedDepNodeIndex`]; built on first use.
    reverse_index: OnceLock<ReverseIndex>,
    /// The number of previous compilation sessions. This is used to generate
    /// unique anon dep nodes per session.
    session_count: u64,
    /// Raw-page fingerprints for the stable page files referenced by this graph.
    /// The next generation uses them to leave identical hard-linked pages in
    /// place instead of rewriting and recompressing them.
    page_hashes: FxHashMap<u32, Fingerprint>,
}

// `SelfProfilerRef` is not `Debug`, so we can't derive this.
impl std::fmt::Debug for SerializedDepGraph {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SerializedDepGraph")
            .field("node_max", &self.node_max)
            .field("node_count", &self.node_count)
            .field("session_count", &self.session_count)
            .field("page_hashes", &self.page_hashes.len())
            .finish_non_exhaustive()
    }
}

impl SerializedDepGraph {
    #[inline]
    pub fn edge_targets_from(&self, source: SerializedDepNodeIndex) -> Option<GraphEdgeTargets> {
        let page = self.page_store.as_ref()?.page_for_index(source)?;
        let slot = (source.as_u32() % PAGE_NODE_CAPACITY) as u16;
        let record = *page.node_for_slot(slot)?;
        Some(GraphEdgeTargets::new(page, source.as_u32(), &record))
    }

    #[inline]
    pub fn index_to_node(&self, dep_node_index: SerializedDepNodeIndex) -> Option<DepNode> {
        let page = self.page_store.as_ref()?.page_for_index(dep_node_index)?;
        page.node_for_slot((dep_node_index.as_u32() % PAGE_NODE_CAPACITY) as u16)
            .map(|record| record.node)
    }

    #[inline]
    pub fn node_to_index_opt(&self, dep_node: &DepNode) -> Option<SerializedDepNodeIndex> {
        let page_store = self.page_store.as_ref()?;
        self.reverse_index
            .get_or_init(|| ReverseIndex::build(page_store, self.node_count))
            .index_for(dep_node, page_store)
    }

    #[inline]
    pub fn value_fingerprint_for_index(
        &self,
        dep_node_index: SerializedDepNodeIndex,
    ) -> Option<Fingerprint> {
        let page = self.page_store.as_ref()?.page_for_index(dep_node_index)?;
        page.node_for_slot((dep_node_index.as_u32() % PAGE_NODE_CAPACITY) as u16)
            .map(|record| record.value_fingerprint)
    }

    #[inline]
    pub fn node_count(&self) -> usize {
        self.node_count
    }

    /// The length of the stable-ID space, including holes left by nodes removed
    /// since the previous compilation. Consumers that index per-node state by
    /// `SerializedDepNodeIndex` must size it to this value, not `node_count()`.
    #[inline]
    pub fn index_space_len(&self) -> usize {
        self.node_max
    }

    #[inline]
    fn node_max(&self) -> usize {
        self.node_max
    }

    #[inline]
    pub fn session_count(&self) -> u64 {
        self.session_count
    }
}

const PAGED_GRAPH_MAGIC: &[u8; 8] = b"RSDGPG01";
const PAGE_MAGIC: &[u8; 8] = b"RSDGPAG2";
const PAGED_GRAPH_VERSION: u32 = 4;
const PAGE_NODE_CAPACITY: u32 = 4096;
const PAGE_FILE_PREFIX: &str = "dep-graph-page-";
/// Reverse-index buckets written by earlier versions; removed when a session is saved.
const LEGACY_INDEX_FILE_PREFIX: &str = "dep-graph-index-";
const PAGE_HAS_VALUE_FINGERPRINT: u8 = 1;
const PAGE_DELTA_EDGES: u8 = 2;
const PAGE_EDGE_WIDTH_SHIFT: u8 = 2;
const PAGE_EDGE_WIDTH_MASK: u8 = 0b1100;
const PAGE_FLAGS_MASK: u8 = PAGE_HAS_VALUE_FINGERPRINT | PAGE_DELTA_EDGES | PAGE_EDGE_WIDTH_MASK;

struct StableGraphIds {
    current_to_stable: Vec<u32>,
    stable_nodes: Vec<(u32, u32)>,
    node_max: usize,
}

struct GraphSpoolIndex {
    record_offsets: Vec<usize>,
    node_count: usize,
    edge_count: u64,
    session_count: u64,
}

fn invalid_graph_spool() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "invalid dep-graph spool")
}

fn invalid_graph_spool_for(reason: String) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, format!("invalid dep-graph spool: {reason}"))
}

fn graph_page_path(dir: &Path, page_id: u32) -> PathBuf {
    dir.join(format!("{PAGE_FILE_PREFIX}{page_id:08x}.bin"))
}

fn hash_page(bytes: &[u8]) -> Fingerprint {
    let mut hasher = StableHasher::new();
    hasher.write(bytes);
    hasher.finish()
}

fn edge_index_width(max_index: u32) -> usize {
    if max_index == 0 { 1 } else { ((32 - max_index.leading_zeros()) as usize).div_ceil(8) }
}

fn unzigzag(value: u32) -> i64 {
    ((value >> 1) as i64) ^ (-((value & 1) as i64))
}

fn index_legacy_stream(d: &mut MemDecoder<'_>) -> io::Result<GraphSpoolIndex> {
    let trailer_len = 3 * IntEncodedWithFixedSize::ENCODED_SIZE;
    let trailer_start = d.len().checked_sub(trailer_len).ok_or_else(invalid_graph_spool)?;
    let (node_max, node_count, edge_count) = d.with_position(trailer_start, |d| {
        (
            IntEncodedWithFixedSize::decode(d).0 as usize,
            IntEncodedWithFixedSize::decode(d).0 as usize,
            IntEncodedWithFixedSize::decode(d).0 as usize,
        )
    });
    if node_max > SerializedDepNodeIndex::MAX_AS_U32 as usize + 1 || node_count > node_max {
        return Err(invalid_graph_spool());
    }

    let mut record_offsets = vec![usize::MAX; node_max];
    let mut kind_counts = vec![0u32; DepKind::MAX as usize + 1];
    let mut actual_edges = 0usize;
    for _ in 0..node_count {
        let offset = d.position();
        let header = SerializedNodeHeader { bytes: d.read_array() };
        let index = header.index();
        if index.as_usize() >= node_max || record_offsets[index.as_usize()] != usize::MAX {
            return Err(invalid_graph_spool());
        }
        let node = header.node();
        if node.kind == DepKind::Null {
            return Err(invalid_graph_spool());
        }
        record_offsets[index.as_usize()] = offset;
        kind_counts[node.kind.as_usize()] =
            kind_counts[node.kind.as_usize()].checked_add(1).ok_or_else(invalid_graph_spool)?;

        let num_edges = header.len().unwrap_or_else(|| d.read_u32()) as usize;
        actual_edges = actual_edges.checked_add(num_edges).ok_or_else(invalid_graph_spool)?;
        let edge_bytes = header
            .bytes_per_index()
            .checked_mul(num_edges)
            .filter(|&len| len <= d.remaining())
            .ok_or_else(invalid_graph_spool)?;
        d.read_raw_bytes(edge_bytes);
    }

    for expected_count in kind_counts {
        let encoded_count = read_graph_u32(d).map_err(|_| invalid_graph_spool())?;
        if encoded_count != expected_count {
            return Err(invalid_graph_spool());
        }
    }
    let session_count = read_graph_u64(d).map_err(|_| invalid_graph_spool())?;
    if d.remaining() != trailer_len || actual_edges != edge_count {
        return Err(invalid_graph_spool());
    }

    Ok(GraphSpoolIndex { record_offsets, node_count, edge_count: edge_count as u64, session_count })
}

fn read_spool_u32(spool: &[u8], position: &mut usize) -> io::Result<u32> {
    let mut value = 0u32;
    for shift in (0..35).step_by(7) {
        let byte = *spool.get(*position).ok_or_else(invalid_graph_spool)?;
        *position += 1;
        if shift == 28 && byte > 0x0f {
            return Err(invalid_graph_spool());
        }
        value |= u32::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            return if shift == 0 || byte != 0 { Ok(value) } else { Err(invalid_graph_spool()) };
        }
    }
    Err(invalid_graph_spool())
}

fn spool_node_record<'a>(
    spool: &'a [u8],
    offset: usize,
) -> io::Result<(SerializedNodeHeader, u32, &'a [u8])> {
    let header_end =
        offset.checked_add(size_of::<SerializedNodeHeader>()).ok_or_else(invalid_graph_spool)?;
    let bytes = spool.get(offset..header_end).ok_or_else(invalid_graph_spool)?;
    let header = SerializedNodeHeader { bytes: bytes.try_into().unwrap() };
    let mut edges_start = header_end;
    let num_edges = match header.len() {
        Some(len) => len,
        None => read_spool_u32(spool, &mut edges_start)?,
    };
    let edge_bytes =
        header.bytes_per_index().checked_mul(num_edges as usize).ok_or_else(invalid_graph_spool)?;
    let edges_end = edges_start.checked_add(edge_bytes).ok_or_else(invalid_graph_spool)?;
    let edges = spool.get(edges_start..edges_end).ok_or_else(invalid_graph_spool)?;
    Ok((header, num_edges, edges))
}

fn assign_stable_graph_ids(
    spool: &[u8],
    graph: &GraphSpoolIndex,
    previous: &SerializedDepGraph,
) -> io::Result<StableGraphIds> {
    let active_count = graph.node_count;
    let old_node_max = previous.node_max();
    let compact = old_node_max > active_count.saturating_mul(3) / 2 + PAGE_NODE_CAPACITY as usize;

    let mut current_to_stable = vec![u32::MAX; graph.record_offsets.len()];
    let mut occupied = vec![false; old_node_max.max(2)];
    // Index 1 is the fixed `Red` node used by the graph algorithms. Index 0
    // is not reserved: `AnonZeroDeps` has a session-specific key, and the old
    // sentinel can coexist with the new one while it is promoted from the prior
    // graph.
    occupied[1] = true;

    let mut fresh = Vec::new();
    for (index, &offset) in graph.record_offsets.iter().enumerate() {
        if offset == usize::MAX {
            continue;
        }
        let (header, _, _) = spool_node_record(spool, offset)?;
        let node = header.node();
        if index == 1 && node.kind == DepKind::Red {
            if current_to_stable[1] != u32::MAX {
                return Err(invalid_graph_spool_for("multiple red nodes".into()));
            }
            current_to_stable[index] = 1;
            continue;
        }

        if !compact && node.kind != DepKind::SideEffect {
            if let Some(previous_index) = previous.node_to_index_opt(&node) {
                let stable_id = previous_index.as_u32();
                if stable_id == 1 || stable_id as usize >= occupied.len() {
                    return Err(invalid_graph_spool_for(format!(
                        "current ID {index} maps to reserved previous ID {stable_id} for {node:?}"
                    )));
                }
                let slot = &mut occupied[stable_id as usize];
                if *slot {
                    return Err(invalid_graph_spool_for(format!(
                        "multiple current records map to previous ID {stable_id} for {node:?}"
                    )));
                }
                *slot = true;
                current_to_stable[index] = stable_id;
                continue;
            }
        }
        fresh.push(index as u32);
    }

    // New IDs follow a depth-first post-order over the edges among new nodes, so a node's
    // dependencies usually sit just before it and its edges encode as short deltas (graph
    // pages ~12-17% smaller on sampled Bevy crates). Roots are visited in key order, which
    // keeps IDs deterministic within a generation.
    fresh.sort_unstable_by_key(|&index| {
        let (header, _, _) = spool_node_record(spool, graph.record_offsets[index as usize])
            .expect("records were validated while indexing the current graph");
        let node = header.node();
        (Fingerprint::from(node.key_fingerprint), node.kind.as_u16(), index)
    });
    let fresh = dependency_order(spool, &graph.record_offsets, &fresh)?;
    let mut next_free = 0usize;
    for index in fresh {
        while next_free < occupied.len() && occupied[next_free] {
            next_free += 1;
        }
        if next_free == occupied.len() {
            occupied.push(true);
        } else {
            occupied[next_free] = true;
        }
        current_to_stable[index as usize] = next_free.try_into().unwrap();
        next_free += 1;
    }

    let mut stable_nodes: Vec<_> = current_to_stable
        .iter()
        .enumerate()
        .filter_map(|(current, &stable)| (stable != u32::MAX).then_some((stable, current as u32)))
        .collect();
    stable_nodes.sort_unstable_by_key(|&(stable, _)| stable);
    if stable_nodes.windows(2).any(|pair| pair[0].0 == pair[1].0) {
        return Err(invalid_graph_spool_for("stable IDs are not unique".into()));
    }
    let node_max = stable_nodes.last().map_or(2, |&(stable, _)| stable as usize + 1);
    if node_max > SerializedDepNodeIndex::MAX_AS_U32 as usize + 1 {
        return Err(invalid_graph_spool_for("stable index exceeds maximum value".into()));
    }
    Ok(StableGraphIds { current_to_stable, stable_nodes, node_max })
}

/// Orders `roots` (current indices) by an iterative depth-first post-order over the edges
/// that stay within `roots`.
fn dependency_order(spool: &[u8], record_offsets: &[usize], roots: &[u32]) -> io::Result<Vec<u32>> {
    let mut in_set = vec![false; record_offsets.len()];
    for &root in roots {
        in_set[root as usize] = true;
    }
    let mut visited = vec![false; record_offsets.len()];
    let mut order = Vec::with_capacity(roots.len());
    // (node, next edge to visit)
    let mut stack: Vec<(u32, u32)> = Vec::new();
    for &root in roots {
        if visited[root as usize] {
            continue;
        }
        visited[root as usize] = true;
        stack.push((root, 0));
        while let Some(top) = stack.last_mut() {
            let (node, next_edge) = *top;
            let (header, num_edges, edges) =
                spool_node_record(spool, record_offsets[node as usize])?;
            if next_edge == num_edges {
                order.push(node);
                stack.pop();
                continue;
            }
            top.1 += 1;
            let width = header.bytes_per_index();
            let mut target_bytes = [0u8; DEP_NODE_SIZE];
            target_bytes[..width].copy_from_slice(&edges[next_edge as usize * width..][..width]);
            let target = u32::from_le_bytes(target_bytes) as usize;
            if in_set.get(target) == Some(&true) && !visited[target] {
                visited[target] = true;
                stack.push((target as u32, 0));
            }
        }
    }
    Ok(order)
}

fn encode_graph_page(
    spool: &[u8],
    record_offsets: &[usize],
    ids: &StableGraphIds,
    page_id: u32,
    nodes: &[(u32, u32)],
) -> io::Result<Vec<u8>> {
    let mut fingerprints = Vec::with_capacity(nodes.len());
    for &(_, current_index) in nodes {
        let (header, _, _) = spool_node_record(spool, record_offsets[current_index as usize])?;
        fingerprints.push(Fingerprint::from(header.node().key_fingerprint));
    }
    fingerprints.sort_unstable();
    fingerprints.dedup();
    let use_dictionary = fingerprints.len() * 16 + nodes.len() * 2 < nodes.len() * 16;

    let mut encoder = MemEncoder::new();
    encoder.emit_raw_bytes(PAGE_MAGIC);
    encoder.emit_u32(page_id);
    encoder.emit_u32(nodes.len().try_into().unwrap());
    encoder.emit_u32(if use_dictionary { fingerprints.len().try_into().unwrap() } else { 0 });
    if use_dictionary {
        for fingerprint in &fingerprints {
            encoder.emit_raw_bytes(&fingerprint.to_le_bytes());
        }
    }

    // Each field is stored as its own column (slots, kinds, flags, keys, value fingerprints,
    // edge counts, edges). Grouping similar bytes compresses better than interleaved records.
    let mut slots = Vec::with_capacity(nodes.len() * 2);
    let mut kinds = Vec::with_capacity(nodes.len() * 2);
    let mut flag_bytes = Vec::with_capacity(nodes.len());
    let mut keys = Vec::with_capacity(nodes.len() * 2);
    let mut values = Vec::new();
    let mut counts = MemEncoder::new();
    let mut edge_bytes = Vec::new();
    for &(stable_id, current_index) in nodes {
        let slot = stable_id % PAGE_NODE_CAPACITY;
        let (header, num_edges, encoded_edges) =
            spool_node_record(spool, record_offsets[current_index as usize])?;
        let node = header.node();
        let value_fingerprint = header.value_fingerprint();
        let current_edge_width = header.bytes_per_index();
        let mut edges = Vec::with_capacity(num_edges as usize);
        for current_target in encoded_edges.chunks_exact(current_edge_width) {
            let mut target_bytes = [0u8; DEP_NODE_SIZE];
            target_bytes[..current_edge_width].copy_from_slice(current_target);
            let target = u32::from_le_bytes(target_bytes);
            let Some(&stable_target) = ids.current_to_stable.get(target as usize) else {
                return Err(invalid_graph_spool());
            };
            if stable_target == u32::MAX {
                return Err(invalid_graph_spool());
            }
            edges.push(stable_target);
        }

        let max_edge = edges.iter().copied().max().unwrap_or(0);
        let fixed_width = edge_index_width(max_edge);
        let fixed_len = fixed_width * edges.len();
        let mut deltas = MemEncoder::new();
        let mut previous_edge = stable_id;
        for &edge in &edges {
            let delta = edge as i64 - previous_edge as i64;
            let zigzag = ((delta << 1) ^ (delta >> 63)) as u32;
            deltas.emit_u32(zigzag);
            previous_edge = edge;
        }
        let use_deltas = deltas.data.len() < fixed_len;

        slots.extend_from_slice(&(slot as u16).to_le_bytes());
        kinds.extend_from_slice(&node.kind.as_u16().to_le_bytes());
        let mut flags =
            if value_fingerprint == Fingerprint::ZERO { 0 } else { PAGE_HAS_VALUE_FINGERPRINT };
        if use_deltas {
            flags |= PAGE_DELTA_EDGES;
        } else {
            flags |= ((fixed_width - 1) as u8) << PAGE_EDGE_WIDTH_SHIFT;
        }
        flag_bytes.push(flags);

        let key_fingerprint = Fingerprint::from(node.key_fingerprint);
        if use_dictionary {
            let key_index: u16 =
                fingerprints.binary_search(&key_fingerprint).unwrap().try_into().unwrap();
            keys.extend_from_slice(&key_index.to_le_bytes());
        } else {
            keys.extend_from_slice(&key_fingerprint.to_le_bytes());
        }
        if value_fingerprint != Fingerprint::ZERO {
            values.extend_from_slice(&value_fingerprint.to_le_bytes());
        }
        counts.emit_u32(edges.len().try_into().unwrap());
        if use_deltas {
            edge_bytes.extend_from_slice(&deltas.data);
        } else {
            for edge in edges {
                edge_bytes.extend_from_slice(&edge.to_le_bytes()[..fixed_width]);
            }
        }
    }
    for column in [&slots, &kinds, &flag_bytes, &keys, &values, &counts.data, &edge_bytes] {
        encoder.emit_raw_bytes(column);
    }

    Ok(encoder.finish())
}

fn write_page_file(
    path: &Path,
    page_bytes: &[u8],
    compression: Option<CompressionOptions>,
) -> io::Result<()> {
    let mut temp_path = path.to_path_buf();
    temp_path.set_extension("part");
    let mut file = File::create(&temp_path)?;
    file.write_all(page_bytes)?;
    file.write_all(MAGIC_END_BYTES)?;
    drop(file);

    if let Some(options) = compression {
        artifact_compression::pack_with_options(&temp_path, options)?;
    }

    if path.exists() {
        fs::remove_file(path)?;
    }
    if let Err(err) = fs::rename(&temp_path, path) {
        let _ = fs::remove_file(&temp_path);
        return Err(err);
    }
    Ok(())
}

fn key_hash(kind: DepKind, fingerprint: Fingerprint) -> u64 {
    let mut hasher = StableHasher::new();
    hasher.write(&kind.as_u16().to_le_bytes());
    hasher.write(&fingerprint.to_le_bytes());
    let fingerprint: Fingerprint = hasher.finish();
    let bytes = fingerprint.to_le_bytes();
    u64::from_le_bytes(bytes[..8].try_into().unwrap())
}

fn write_paged_graph(
    graph_path: &Path,
    graph_start: usize,
    previous: &SerializedDepGraph,
    compression: Option<CompressionOptions>,
) -> io::Result<(usize, Vec<u32>)> {
    // The first encoding is a bounded sequential spool. Indexing it retains only
    // one offset per node; pages are encoded by reading records back from the
    // mapping, without expanding the whole graph into temporary node and edge
    // arrays.
    // The graph is finalized while it is still at its private staging path. It is
    // renamed to `dep-graph.bin` only after the query-cache IDs have been prepared.
    let spool_path = graph_path;
    // SAFETY: the spool is private to the active session and is not mutated
    // until the mapping is dropped below.
    let spool = unsafe { Mmap::map(File::open(&spool_path)?) }?;
    let graph = {
        let mut decoder =
            MemDecoder::new(&spool, graph_start).map_err(|_| invalid_graph_spool())?;
        index_legacy_stream(&mut decoder)
            .map_err(|err| io::Error::new(err.kind(), format!("indexing graph spool: {err}")))?
    };
    let prefix = spool[..graph_start].to_vec();
    let ids = assign_stable_graph_ids(&spool, &graph, previous)
        .map_err(|err| io::Error::new(err.kind(), format!("assigning stable graph IDs: {err}")))?;
    let stable_nodes = &ids.stable_nodes;

    let parent = spool_path.parent().expect("dep-graph has a parent directory");
    let mut page_hashes = FxHashMap::default();
    let mut page_manifest = Vec::new();
    let mut page_start = 0;
    while page_start < stable_nodes.len() {
        let page_id = stable_nodes[page_start].0 / PAGE_NODE_CAPACITY;
        let mut page_end = page_start + 1;
        while page_end < stable_nodes.len()
            && stable_nodes[page_end].0 / PAGE_NODE_CAPACITY == page_id
        {
            page_end += 1;
        }
        let page_nodes = &stable_nodes[page_start..page_end];
        let bytes = encode_graph_page(&spool, &graph.record_offsets, &ids, page_id, page_nodes)
            .map_err(|err| io::Error::new(err.kind(), format!("encoding graph page: {err}")))?;
        let page_edge_count = page_nodes.iter().try_fold(0u64, |total, &(_, current_index)| {
            let (_, edges, _) =
                spool_node_record(&spool, graph.record_offsets[current_index as usize])?;
            total.checked_add(u64::from(edges)).ok_or_else(invalid_graph_spool)
        })?;
        let hash = hash_page(&bytes);
        let page_path = graph_page_path(parent, page_id);
        let unchanged = previous.page_hashes.get(&page_id) == Some(&hash) && page_path.is_file();
        if !unchanged {
            write_page_file(&page_path, &bytes, compression)?;
        }
        page_hashes.insert(page_id, hash);
        page_manifest.push((
            page_id,
            page_nodes.len() as u32,
            page_edge_count,
            bytes.len() as u64,
            hash,
        ));
        page_start = page_end;
    }

    // Remove pages that became unreachable in this graph, and reverse-index buckets from
    // earlier versions. The session is still private; older published sessions retain their
    // own hard links.
    for entry in fs::read_dir(parent)? {
        let entry = entry?;
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else { continue };
        let unreachable_page =
            parse_graph_page_name(&name).is_some_and(|page_id| !page_hashes.contains_key(&page_id));
        if unreachable_page || name.starts_with(LEGACY_INDEX_FILE_PREFIX) {
            fs::remove_file(entry.path())?;
        }
    }

    let mut manifest = MemEncoder::new();
    manifest.emit_raw_bytes(PAGED_GRAPH_MAGIC);
    manifest.emit_u32(PAGED_GRAPH_VERSION);
    manifest.emit_u32(ids.node_max.try_into().unwrap());
    manifest.emit_u32(stable_nodes.len().try_into().unwrap());
    manifest.emit_u64(graph.edge_count);
    manifest.emit_u64(graph.session_count);
    manifest.emit_u32(page_manifest.len().try_into().unwrap());
    for (page_id, node_count, edge_count, raw_len, hash) in page_manifest {
        manifest.emit_u32(page_id);
        manifest.emit_u32(node_count);
        manifest.emit_u64(edge_count);
        manifest.emit_u64(raw_len);
        manifest.emit_raw_bytes(&hash.to_le_bytes());
    }

    drop(spool);
    let mut output = File::create(&spool_path)?;
    output.write_all(&prefix)?;
    output.write_all(&manifest.data)?;
    output.write_all(MAGIC_END_BYTES)?;
    output.flush()?;
    let size = output
        .metadata()?
        .len()
        .try_into()
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "dep graph exceeds usize"))?;
    Ok((size, ids.current_to_stable))
}

fn parse_graph_page_name(name: &str) -> Option<u32> {
    let suffix = name.strip_prefix(PAGE_FILE_PREFIX)?.strip_suffix(".bin")?;
    if suffix.len() != 8 {
        return None;
    }
    u32::from_str_radix(suffix, 16).ok()
}

struct GraphPageDecoder<'a> {
    bytes: &'a [u8],
    position: usize,
}

impl<'a> GraphPageDecoder<'a> {
    fn new(bytes: &'a [u8]) -> Result<Self, ()> {
        let bytes = bytes.strip_suffix(MAGIC_END_BYTES).ok_or(())?;
        Ok(Self { bytes, position: 0 })
    }

    fn remaining(&self) -> usize {
        self.bytes.len() - self.position
    }

    fn position(&self) -> usize {
        self.position
    }

    fn read_bytes(&mut self, len: usize) -> Result<&'a [u8], ()> {
        let end = self.position.checked_add(len).filter(|&end| end <= self.bytes.len()).ok_or(())?;
        let bytes = &self.bytes[self.position..end];
        self.position = end;
        Ok(bytes)
    }

    fn read_u8(&mut self) -> Result<u8, ()> {
        Ok(self.read_bytes(1)?[0])
    }

    fn read_u32(&mut self) -> Result<u32, ()> {
        let mut value = 0u32;
        for shift in (0..35).step_by(7) {
            let byte = self.read_u8()?;
            if shift == 28 && byte > 0x0f {
                return Err(());
            }
            value |= u32::from(byte & 0x7f) << shift;
            if byte & 0x80 == 0 {
                return if shift == 0 || byte != 0 { Ok(value) } else { Err(()) };
            }
        }
        Err(())
    }

    fn read_fingerprint(&mut self) -> Result<Fingerprint, ()> {
        Ok(Fingerprint::from_le_bytes(self.read_bytes(16)?.try_into().unwrap()))
    }
}

#[derive(Clone, Copy)]
struct GraphPageDescriptor {
    node_count: u32,
    edge_count: u64,
    raw_len: usize,
    hash: Fingerprint,
}

#[derive(Clone, Copy)]
struct GraphPageNode {
    node: DepNode,
    value_fingerprint: Fingerprint,
    edge_start: usize,
    edge_count: u32,
    edge_width: u8,
    delta_edges: bool,
}

struct DecodedGraphPage {
    bytes: Mmap,
    nodes: Vec<GraphPageNode>,
    slot_to_position: Box<[u16]>,
    resident_bytes: usize,
}

impl DecodedGraphPage {
    fn node_for_slot(&self, slot: u16) -> Option<&GraphPageNode> {
        let position = *self.slot_to_position.get(slot as usize)?;
        (position != u16::MAX).then(|| &self.nodes[position as usize])
    }
}

/// Pages decoded this session. Pages load on first use and stay until the session ends: a
/// byte-limited LRU made recompiling a large crate thrash, because green-marking visits nodes
/// across pages in dependency order (a Bevy library edit took 870 s with a 64 MiB limit and
/// 12 s without one). Memory never exceeds the whole graph that an eager decoder would hold.
#[derive(Default)]
struct GraphPageCache {
    pages: FxHashMap<u32, Arc<DecodedGraphPage>>,
    invalid_pages: FxHashSet<u32>,
    resident_bytes: usize,
    page_loads: u64,
    cache_hits: u64,
}

struct GraphPageStore {
    page_dir: PathBuf,
    node_max: usize,
    pages: Vec<(u32, GraphPageDescriptor)>,
    cache: Lock<GraphPageCache>,
    profiler: SelfProfilerRef,
}

impl GraphPageStore {
    fn descriptor(&self, page_id: u32) -> Option<GraphPageDescriptor> {
        self.pages
            .binary_search_by_key(&page_id, |(id, _)| *id)
            .ok()
            .map(|index| self.pages[index].1)
    }

    fn page_for_index(&self, index: SerializedDepNodeIndex) -> Option<Arc<DecodedGraphPage>> {
        let raw = index.as_u32();
        if raw as usize >= self.node_max {
            return None;
        }
        self.load_page(raw / PAGE_NODE_CAPACITY)
    }

    #[allow(rustc::potential_query_instability)]
    fn load_page(&self, page_id: u32) -> Option<Arc<DecodedGraphPage>> {
        let descriptor = self.descriptor(page_id)?;
        {
            let mut cache = self.cache.lock();
            if cache.invalid_pages.contains(&page_id) {
                return None;
            }
            if let Some(page) = cache.pages.get(&page_id).map(Arc::clone) {
                cache.cache_hits += 1;
                return Some(page);
            }
        }

        let page_path = graph_page_path(&self.page_dir, page_id);
        // SAFETY: incremental-session locks make persisted pages immutable while they are read.
        let decoded = unsafe { Mmap::map_artifact(&page_path) }
            .map_err(|error| format!("cannot read page: {error}"))
            .and_then(|bytes| decode_graph_page(bytes, page_id, descriptor, self.node_max));

        let page = match decoded {
            Ok(page) => Arc::new(page),
            Err(error) => {
                let mut cache = self.cache.lock();
                if cache.invalid_pages.insert(page_id) {
                    warn!(page_id, %error, "ignoring corrupt incremental dependency-graph page; affected queries will be recomputed");
                }
                return None;
            }
        };

        let mut cache = self.cache.lock();
        if cache.invalid_pages.contains(&page_id) {
            return None;
        }
        // Another thread may have decoded the same page meanwhile; keep the first copy.
        if let Some(existing) = cache.pages.get(&page_id) {
            return Some(Arc::clone(existing));
        }
        cache.page_loads += 1;
        cache.resident_bytes += page.resident_bytes;
        cache.pages.insert(page_id, Arc::clone(&page));
        Some(page)
    }
}

/// Maps [`DepNode`] keys back to stable IDs. It is built from the pages on the first lookup
/// instead of being persisted: a recompiling crate loads every page anyway (measured on Bevy,
/// even for a touch), so a stored index only cost disk space.
struct ReverseIndex {
    /// Sorted key hashes, parallel to `indices`.
    hashes: Box<[u64]>,
    indices: Box<[u32]>,
}

impl ReverseIndex {
    fn build(page_store: &GraphPageStore, node_count: usize) -> Self {
        let _prof_timer =
            page_store.profiler.generic_activity("incr_comp_build_dep_graph_reverse_index");
        let mut entries = Vec::with_capacity(node_count);
        for &(page_id, _) in &page_store.pages {
            // Nodes on a corrupt page stay unresolvable; their queries are recomputed.
            let Some(page) = page_store.load_page(page_id) else { continue };
            for slot in 0..PAGE_NODE_CAPACITY as u16 {
                if let Some(record) = page.node_for_slot(slot) {
                    let hash = key_hash(record.node.kind, record.node.key_fingerprint.into());
                    entries.push((hash, page_id * PAGE_NODE_CAPACITY + u32::from(slot)));
                }
            }
        }
        entries.sort_unstable();
        let (hashes, indices) = entries.into_iter().unzip::<_, _, Vec<_>, Vec<_>>();
        Self { hashes: hashes.into_boxed_slice(), indices: indices.into_boxed_slice() }
    }

    fn index_for(
        &self,
        node: &DepNode,
        page_store: &GraphPageStore,
    ) -> Option<SerializedDepNodeIndex> {
        let fingerprint = Fingerprint::from(node.key_fingerprint);
        let hash = key_hash(node.kind, fingerprint);
        let start = self.hashes.partition_point(|&entry| entry < hash);
        let end = start + self.hashes[start..].partition_point(|&entry| entry == hash);
        let mut found = None;
        for &stable_id in &self.indices[start..end] {
            let dep_index = SerializedDepNodeIndex::from_u32(stable_id);
            let Some(page) = page_store.page_for_index(dep_index) else { continue };
            let slot = (stable_id % PAGE_NODE_CAPACITY) as u16;
            let Some(candidate) = page.node_for_slot(slot).map(|record| record.node) else {
                continue;
            };
            if candidate.kind == node.kind && candidate.key_fingerprint == node.key_fingerprint {
                if found.is_some() && node.kind != DepKind::SideEffect {
                    panic!(
                        "Error: A dep graph node ({:?}) does not have a unique index. \
                         Running a clean build on a nightly compiler with \
                         `-Z incremental-verify-ich` can help narrow down the issue for reporting. \
                         A clean build may also work around the issue.\n\nFingerprint: {fingerprint:?}",
                        node.kind
                    )
                }
                found = Some(dep_index);
            }
        }
        found
    }
}

impl Drop for GraphPageStore {
    fn drop(&mut self) {
        if std::env::var_os("RUSTC_INCREMENTAL_PAGE_STATS").is_none() {
            return;
        }
        let cache = self.cache.lock();
        eprintln!(
            "RUSTC_INCREMENTAL_PAGE_CACHE {{\"pages\":{},\"loads\":{},\"hits\":{},\"invalid\":{},\"resident_bytes\":{}}}",
            self.pages.len(),
            cache.page_loads,
            cache.cache_hits,
            cache.invalid_pages.len(),
            cache.resident_bytes,
        );
    }
}

fn decode_graph_page(
    bytes: Mmap,
    expected_page_id: u32,
    descriptor: GraphPageDescriptor,
    node_max: usize,
) -> Result<DecodedGraphPage, String> {
    let payload =
        bytes.strip_suffix(MAGIC_END_BYTES).ok_or_else(|| "page footer is missing".to_owned())?;
    if payload.len() != descriptor.raw_len || hash_page(payload) != descriptor.hash {
        return Err("page checksum or length mismatch".to_owned());
    }
    let mut page = GraphPageDecoder::new(&bytes).map_err(|_| "invalid page footer".to_owned())?;
    if page.read_bytes(PAGE_MAGIC.len()).map_err(|_| "truncated page header")? != PAGE_MAGIC
        || page.read_u32().map_err(|_| "invalid page ID")? != expected_page_id
    {
        return Err("page header does not match manifest".to_owned());
    }
    let page_node_count = page.read_u32().map_err(|_| "missing page node count")?;
    let dictionary_len = page.read_u32().map_err(|_| "missing dictionary length")? as usize;
    if page_node_count == 0
        || page_node_count > PAGE_NODE_CAPACITY
        || page_node_count != descriptor.node_count
        || dictionary_len > page_node_count as usize
    {
        return Err("invalid page node or dictionary count".to_owned());
    }
    let mut dictionary = Vec::with_capacity(dictionary_len);
    for _ in 0..dictionary_len {
        dictionary.push(page.read_fingerprint().map_err(|_| "truncated fingerprint dictionary")?);
    }
    if dictionary.windows(2).any(|pair| pair[0] >= pair[1]) {
        return Err("fingerprint dictionary is not strictly sorted".to_owned());
    }

    let node_count = page_node_count as usize;
    let key_width = if dictionary_len == 0 { 16 } else { 2 };
    let slots = page.read_bytes(node_count * 2).map_err(|_| "truncated slot column")?;
    let kinds = page.read_bytes(node_count * 2).map_err(|_| "truncated dep-kind column")?;
    let flag_bytes = page.read_bytes(node_count).map_err(|_| "truncated flag column")?;
    let keys = page.read_bytes(node_count * key_width).map_err(|_| "truncated key column")?;
    let value_count =
        flag_bytes.iter().filter(|&&flags| flags & PAGE_HAS_VALUE_FINGERPRINT != 0).count();
    let values = page.read_bytes(value_count * 16).map_err(|_| "truncated value column")?;
    let mut counts = Vec::with_capacity(node_count);
    for _ in 0..node_count {
        counts.push(page.read_u32().map_err(|_| "missing edge count")?);
    }

    let mut nodes = Vec::with_capacity(node_count);
    let mut slot_to_position = vec![u16::MAX; PAGE_NODE_CAPACITY as usize];
    let mut previous_slot = None;
    let mut edge_count = 0u64;
    let mut value_position = 0;
    for position in 0..node_count {
        let slot = u16::from_le_bytes([slots[position * 2], slots[position * 2 + 1]]);
        if u32::from(slot) >= PAGE_NODE_CAPACITY
            || previous_slot.is_some_and(|previous| slot <= previous)
        {
            return Err("page slots are not strictly increasing".to_owned());
        }
        previous_slot = Some(slot);
        let raw_index = expected_page_id
            .checked_mul(PAGE_NODE_CAPACITY)
            .and_then(|id| id.checked_add(u32::from(slot)))
            .ok_or_else(|| "page node ID overflow".to_owned())?;
        if raw_index as usize >= node_max {
            return Err("page node ID exceeds manifest bound".to_owned());
        }
        let kind_raw = u16::from_le_bytes([kinds[position * 2], kinds[position * 2 + 1]]);
        if kind_raw > DepKind::MAX || kind_raw == DepKind::Null.as_u16() {
            return Err("invalid dep-kind".to_owned());
        }
        let kind = DepKind::from_u16(kind_raw);
        let flags = flag_bytes[position];
        if flags & !PAGE_FLAGS_MASK != 0
            || flags & PAGE_DELTA_EDGES != 0 && flags & PAGE_EDGE_WIDTH_MASK != 0
        {
            return Err("invalid node flags".to_owned());
        }
        let key = &keys[position * key_width..][..key_width];
        let key_fingerprint = if dictionary_len == 0 {
            Fingerprint::from_le_bytes(key.try_into().unwrap())
        } else {
            *dictionary
                .get(u16::from_le_bytes([key[0], key[1]]) as usize)
                .ok_or_else(|| "key dictionary index is out of bounds".to_owned())?
        };
        let value_fingerprint = if flags & PAGE_HAS_VALUE_FINGERPRINT != 0 {
            let value = &values[value_position * 16..][..16];
            value_position += 1;
            Fingerprint::from_le_bytes(value.try_into().unwrap())
        } else {
            Fingerprint::ZERO
        };
        let num_edges = counts[position];
        edge_count = edge_count
            .checked_add(u64::from(num_edges))
            .ok_or_else(|| "edge count overflow".to_owned())?;
        if edge_count > descriptor.edge_count {
            return Err("page edge count exceeds manifest".to_owned());
        }
        let edge_start = page.position();
        let delta_edges = flags & PAGE_DELTA_EDGES != 0;
        let edge_width = if delta_edges {
            0
        } else {
            ((flags & PAGE_EDGE_WIDTH_MASK) >> PAGE_EDGE_WIDTH_SHIFT) + 1
        };
        let mut previous_edge = raw_index;
        if delta_edges {
            for _ in 0..num_edges {
                let target = i64::from(previous_edge)
                    .checked_add(unzigzag(page.read_u32().map_err(|_| "invalid delta edge")?))
                    .ok_or_else(|| "delta edge overflow".to_owned())?;
                if target < 0 || target >= node_max as i64 {
                    return Err("delta edge points outside graph".to_owned());
                }
                previous_edge = target as u32;
            }
        } else {
            let width = edge_width as usize;
            if num_edges as usize > page.remaining() / width {
                return Err("truncated fixed-width edge list".to_owned());
            }
            for _ in 0..num_edges {
                let mut raw = [0; 4];
                raw[..width].copy_from_slice(page.read_bytes(width).map_err(|_| "truncated edge")?);
                if u32::from_le_bytes(raw) as usize >= node_max {
                    return Err("edge points outside graph".to_owned());
                }
            }
        }
        slot_to_position[slot as usize] = nodes.len() as u16;
        nodes.push(GraphPageNode {
            node: DepNode { kind, key_fingerprint: key_fingerprint.into() },
            value_fingerprint,
            edge_start,
            edge_count: num_edges,
            edge_width,
            delta_edges,
        });
    }
    if page.remaining() != 0 || edge_count != descriptor.edge_count {
        return Err("page payload does not match manifest counts".to_owned());
    }
    let resident_bytes = bytes.len()
        + nodes.capacity() * mem::size_of::<GraphPageNode>()
        + slot_to_position.len() * mem::size_of::<u16>();
    Ok(DecodedGraphPage {
        bytes,
        nodes,
        slot_to_position: slot_to_position.into_boxed_slice(),
        resident_bytes,
    })
}

#[derive(Clone)]
pub struct GraphEdgeTargets {
    page: Arc<DecodedGraphPage>,
    position: usize,
    remaining: u32,
    width: u8,
    delta: bool,
    previous: u32,
}

impl GraphEdgeTargets {
    fn new(page: Arc<DecodedGraphPage>, source: u32, node: &GraphPageNode) -> Self {
        Self {
            page,
            position: node.edge_start,
            remaining: node.edge_count,
            width: node.edge_width,
            delta: node.delta_edges,
            previous: source,
        }
    }
}

impl Iterator for GraphEdgeTargets {
    type Item = SerializedDepNodeIndex;

    fn next(&mut self) -> Option<Self::Item> {
        if self.remaining == 0 {
            return None;
        }
        let target = if self.delta {
            let encoded = read_page_u32(&self.page.bytes, &mut self.position)
                .expect("validated dependency graph delta edge");
            (i64::from(self.previous) + unzigzag(encoded)) as u32
        } else {
            let width = self.width as usize;
            let mut bytes = [0; 4];
            bytes[..width].copy_from_slice(&self.page.bytes[self.position..self.position + width]);
            self.position += width;
            u32::from_le_bytes(bytes)
        };
        self.remaining -= 1;
        self.previous = target;
        Some(SerializedDepNodeIndex::from_u32(target))
    }

    fn size_hint(&self) -> (usize, Option<usize>) {
        let remaining = self.remaining as usize;
        (remaining, Some(remaining))
    }
}

impl ExactSizeIterator for GraphEdgeTargets {}

fn read_page_u32(bytes: &[u8], position: &mut usize) -> Option<u32> {
    let mut value = 0u32;
    for shift in (0..35).step_by(7) {
        let byte = *bytes.get(*position)?;
        *position += 1;
        if shift == 28 && byte > 0x0f {
            return None;
        }
        value |= u32::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            return (shift == 0 || byte != 0).then_some(value);
        }
    }
    None
}

fn read_graph_array<const N: usize>(d: &mut MemDecoder<'_>) -> Result<[u8; N], ()> {
    if N > d.remaining() {
        return Err(());
    }
    Ok(d.read_array())
}

fn read_graph_u8(d: &mut MemDecoder<'_>) -> Result<u8, ()> {
    if d.remaining() == 0 {
        return Err(());
    }
    Ok(d.read_u8())
}

fn read_graph_u32(d: &mut MemDecoder<'_>) -> Result<u32, ()> {
    let mut value = 0u32;
    for shift in (0..35).step_by(7) {
        let byte = read_graph_u8(d)?;
        if shift == 28 && byte > 0x0f {
            return Err(());
        }
        value |= u32::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            return if shift == 0 || byte != 0 { Ok(value) } else { Err(()) };
        }
    }
    Err(())
}

fn read_graph_u64(d: &mut MemDecoder<'_>) -> Result<u64, ()> {
    let mut value = 0u64;
    for shift in (0..70).step_by(7) {
        let byte = read_graph_u8(d)?;
        if shift == 63 && byte > 1 {
            return Err(());
        }
        value |= u64::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            return if shift == 0 || byte != 0 { Ok(value) } else { Err(()) };
        }
    }
    Err(())
}

fn decode_paged_graph(
    d: &mut MemDecoder<'_>,
    profiler: &SelfProfilerRef,
    page_dir: &Path,
) -> Result<Arc<SerializedDepGraph>, ()> {
    if read_graph_array::<8>(d)? != *PAGED_GRAPH_MAGIC || read_graph_u32(d)? != PAGED_GRAPH_VERSION
    {
        return Err(());
    }
    let node_max = read_graph_u32(d)? as usize;
    let node_count = read_graph_u32(d)? as usize;
    let edge_count = read_graph_u64(d)?;
    let session_count = read_graph_u64(d)?;
    let page_count = read_graph_u32(d)? as usize;
    if node_max > SerializedDepNodeIndex::MAX_AS_U32 as usize + 1
        || node_count > node_max
        || page_count > node_max.div_ceil(PAGE_NODE_CAPACITY as usize)
        // Stable-ID compaction keeps the index space close to the live node count.
        // Besides checking the writer's invariant, this prevents corrupt manifests
        // from triggering unbounded array allocations before a page is inspected.
        || node_max > node_count.saturating_mul(2).saturating_add(PAGE_NODE_CAPACITY as usize)
        // The page descriptor has a 16-byte hash and four variable-width integers. The
        // smallest valid descriptor is 20 bytes; bound allocation by bytes present.
        || page_count > d.remaining() / 20
    {
        return Err(());
    }

    let mut pages = Vec::with_capacity(page_count);
    let mut page_hashes = FxHashMap::default();
    let mut actual_nodes = 0u64;
    let mut actual_edges = 0u64;
    let mut previous_page_id = None;

    for _ in 0..page_count {
        let page_id = read_graph_u32(d)?;
        let page_node_count = read_graph_u32(d)?;
        let page_edge_count = read_graph_u64(d)?;
        let raw_len = usize::try_from(read_graph_u64(d)?).map_err(|_| ())?;
        let expected_hash = Fingerprint::from_le_bytes(read_graph_array::<16>(d)?);
        let first_id = (page_id as usize).checked_mul(PAGE_NODE_CAPACITY as usize).ok_or(())?;
        if first_id >= node_max
            || page_node_count == 0
            || page_node_count > PAGE_NODE_CAPACITY
            || previous_page_id.is_some_and(|previous| page_id <= previous)
            || page_hashes.insert(page_id, expected_hash).is_some()
        {
            return Err(());
        }
        previous_page_id = Some(page_id);
        actual_nodes = actual_nodes.checked_add(u64::from(page_node_count)).ok_or(())?;
        actual_edges = actual_edges.checked_add(page_edge_count).ok_or(())?;
        pages.push((
            page_id,
            GraphPageDescriptor {
                node_count: page_node_count,
                edge_count: page_edge_count,
                raw_len,
                hash: expected_hash,
            },
        ));
    }

    if actual_nodes != node_count as u64 || actual_edges != edge_count {
        return Err(());
    }
    if d.remaining() != 0 {
        return Err(());
    }
    let page_store = Arc::new(GraphPageStore {
        page_dir: page_dir.to_owned(),
        node_max,
        pages,
        cache: Lock::new(GraphPageCache::default()),
        profiler: profiler.clone(),
    });

    Ok(Arc::new(SerializedDepGraph {
        page_store: Some(page_store),
        node_max,
        node_count,
        reverse_index: OnceLock::new(),
        session_count,
        page_hashes,
    }))
}

impl SerializedDepGraph {
    #[instrument(level = "debug", skip(d, profiler))]
    pub fn decode(
        d: &mut MemDecoder<'_>,
        profiler: &SelfProfilerRef,
        page_dir: &Path,
    ) -> Result<Arc<SerializedDepGraph>, ()> {
        decode_paged_graph(d, profiler, page_dir)
    }
}

/// A packed representation of all the fixed-size fields in a `NodeInfo`.
///
/// This stores in one byte array:
/// * The value `Fingerprint` in the `NodeInfo`
/// * The key `Fingerprint` in `DepNode` that is in this `NodeInfo`
/// * The `DepKind`'s discriminant (a u16, but not all bits are used...)
/// * The byte width of the encoded edges for this node
/// * In whatever bits remain, the length of the edge list for this node, if it fits
struct SerializedNodeHeader {
    // 2 bytes for the DepNode
    // 4 bytes for the index
    // 16 for Fingerprint in DepNode
    // 16 for Fingerprint in NodeInfo
    bytes: [u8; 38],
}

// The fields of a `SerializedNodeHeader`, this struct is an implementation detail and exists only
// to make the implementation of `SerializedNodeHeader` simpler.
struct Unpacked {
    len: Option<u32>,
    bytes_per_index: usize,
    kind: DepKind,
    index: SerializedDepNodeIndex,
    key_fingerprint: PackedFingerprint,
    value_fingerprint: Fingerprint,
}

// Bit fields, where
// M: bits used to store the length of a node's edge list
// N: bits used to store the byte width of elements of the edge list
// are
// 0..M    length of the edge
// M..M+N  bytes per index
// M+N..16 kind
impl SerializedNodeHeader {
    const TOTAL_BITS: usize = size_of::<DepKind>() * 8;
    const LEN_BITS: usize = Self::TOTAL_BITS - Self::KIND_BITS - Self::WIDTH_BITS;
    const WIDTH_BITS: usize = DEP_NODE_WIDTH_BITS;
    const KIND_BITS: usize = Self::TOTAL_BITS - DepKind::MAX.leading_zeros() as usize;
    const MAX_INLINE_LEN: usize = (u16::MAX as usize >> (Self::TOTAL_BITS - Self::LEN_BITS)) - 1;

    #[inline]
    fn new(
        node: &DepNode,
        index: DepNodeIndex,
        value_fingerprint: Fingerprint,
        edge_max_index: u32,
        edge_count: usize,
    ) -> Self {
        debug_assert_eq!(Self::TOTAL_BITS, Self::LEN_BITS + Self::WIDTH_BITS + Self::KIND_BITS);

        let mut head = node.kind.as_u16();

        let free_bytes = edge_max_index.leading_zeros() as usize / 8;
        let bytes_per_index = (DEP_NODE_SIZE - free_bytes).saturating_sub(1);
        head |= (bytes_per_index as u16) << Self::KIND_BITS;

        // Encode number of edges + 1 so that we can reserve 0 to indicate that the len doesn't fit
        // in this bitfield.
        if edge_count <= Self::MAX_INLINE_LEN {
            head |= (edge_count as u16 + 1) << (Self::KIND_BITS + Self::WIDTH_BITS);
        }

        let hash: Fingerprint = node.key_fingerprint.into();

        // Using half-open ranges ensures an unconditional panic if we get the magic numbers wrong.
        let mut bytes = [0u8; 38];
        bytes[..2].copy_from_slice(&head.to_le_bytes());
        bytes[2..6].copy_from_slice(&index.as_u32().to_le_bytes());
        bytes[6..22].copy_from_slice(&hash.to_le_bytes());
        bytes[22..].copy_from_slice(&value_fingerprint.to_le_bytes());

        #[cfg(debug_assertions)]
        {
            let res = Self { bytes };
            assert_eq!(value_fingerprint, res.value_fingerprint());
            assert_eq!(*node, res.node());
            if let Some(len) = res.len() {
                assert_eq!(edge_count, len as usize);
            }
        }
        Self { bytes }
    }

    #[inline]
    fn unpack(&self) -> Unpacked {
        let head = u16::from_le_bytes(self.bytes[..2].try_into().unwrap());
        let index = u32::from_le_bytes(self.bytes[2..6].try_into().unwrap());
        let key_fingerprint = self.bytes[6..22].try_into().unwrap();
        let value_fingerprint = self.bytes[22..].try_into().unwrap();

        let kind = head & mask(Self::KIND_BITS) as u16;
        let bytes_per_index = (head >> Self::KIND_BITS) & mask(Self::WIDTH_BITS) as u16;
        let len = (head as u32) >> (Self::WIDTH_BITS + Self::KIND_BITS);

        Unpacked {
            len: len.checked_sub(1),
            bytes_per_index: bytes_per_index as usize + 1,
            kind: DepKind::from_u16(kind),
            index: SerializedDepNodeIndex::from_u32(index),
            key_fingerprint: Fingerprint::from_le_bytes(key_fingerprint).into(),
            value_fingerprint: Fingerprint::from_le_bytes(value_fingerprint),
        }
    }

    #[inline]
    fn len(&self) -> Option<u32> {
        self.unpack().len
    }

    #[inline]
    fn bytes_per_index(&self) -> usize {
        self.unpack().bytes_per_index
    }

    #[inline]
    fn index(&self) -> SerializedDepNodeIndex {
        self.unpack().index
    }

    #[inline]
    fn value_fingerprint(&self) -> Fingerprint {
        self.unpack().value_fingerprint
    }

    #[inline]
    fn node(&self) -> DepNode {
        let Unpacked { kind, key_fingerprint, .. } = self.unpack();
        DepNode { kind, key_fingerprint }
    }
}

#[derive(Debug)]
struct NodeInfo<'a> {
    node: DepNode,
    value_fingerprint: Fingerprint,
    edges: &'a [DepNodeIndex],
}

impl NodeInfo<'_> {
    fn encode(&self, e: &mut MemEncoder, index: DepNodeIndex) {
        let NodeInfo { ref node, value_fingerprint, edges } = *self;
        // The largest index picks the byte width of the edge list.
        let edge_max = edges.iter().map(|e| e.as_u32()).max().unwrap_or(0);
        let header =
            SerializedNodeHeader::new(node, index, value_fingerprint, edge_max, edges.len());
        e.write_array(header.bytes);

        if header.len().is_none() {
            // The edges are all unique and the number of unique indices is less than u32::MAX.
            e.emit_u32(edges.len().try_into().unwrap());
        }

        let bytes_per_index = header.bytes_per_index();
        for node_index in edges.iter() {
            e.write_with(|dest| {
                *dest = node_index.as_u32().to_le_bytes();
                bytes_per_index
            });
        }
    }
}

struct Stat {
    kind: DepKind,
    node_counter: u64,
    edge_counter: u64,
}

struct LocalEncoderState {
    next_node_index: u32,
    remaining_node_index: u32,
    encoder: MemEncoder,
    node_count: usize,
    edge_count: usize,

    /// Stores the number of times we've encoded each dep kind.
    kind_stats: Vec<u32>,
}

struct LocalEncoderResult {
    node_max: u32,
    node_count: usize,
    edge_count: usize,

    /// Stores the number of times we've encoded each dep kind.
    kind_stats: Vec<u32>,
}

struct EncoderState {
    next_node_index: AtomicU64,
    previous: Arc<SerializedDepGraph>,
    file: Lock<Option<FileEncoder<'static>>>,
    current_to_stable: OnceLock<Vec<u32>>,
    graph_start: usize,
    page_compression: Option<CompressionOptions>,
    local: WorkerLocal<RefCell<LocalEncoderState>>,
    stats: Option<Lock<FxHashMap<DepKind, Stat>>>,
}

impl EncoderState {
    fn new(
        encoder: FileEncoder<'static>,
        record_stats: bool,
        previous: Arc<SerializedDepGraph>,
        page_compression: Option<CompressionOptions>,
    ) -> Self {
        let graph_start = encoder.position();
        Self {
            previous,
            next_node_index: AtomicU64::new(0),
            stats: record_stats.then(|| Lock::new(FxHashMap::default())),
            file: Lock::new(Some(encoder)),
            current_to_stable: OnceLock::new(),
            graph_start,
            page_compression,
            local: WorkerLocal::new(|_| {
                RefCell::new(LocalEncoderState {
                    next_node_index: 0,
                    remaining_node_index: 0,
                    edge_count: 0,
                    node_count: 0,
                    encoder: MemEncoder::new(),
                    kind_stats: iter::repeat_n(0, DepKind::MAX as usize + 1).collect(),
                })
            }),
        }
    }

    #[inline]
    fn next_index(&self, local: &mut LocalEncoderState) -> DepNodeIndex {
        if local.remaining_node_index == 0 {
            const COUNT: u32 = 256;

            // We assume that there won't be enough active threads to overflow `u64` from `u32::MAX` here.
            // This can exceed u32::MAX by at most `N` * `COUNT` where `N` is the thread pool count since
            // `try_into().unwrap()` will make threads panic when `self.next_node_index` exceeds u32::MAX.
            local.next_node_index =
                self.next_node_index.fetch_add(COUNT as u64, Ordering::Relaxed).try_into().unwrap();

            // Check that we'll stay within `u32`
            local.next_node_index.checked_add(COUNT).unwrap();

            local.remaining_node_index = COUNT;
        }

        DepNodeIndex::from_u32(local.next_node_index)
    }

    /// Marks the index previously returned by `next_index` as used.
    #[inline]
    fn bump_index(&self, local: &mut LocalEncoderState) {
        local.remaining_node_index -= 1;
        local.next_node_index += 1;
        local.node_count += 1;
    }

    #[inline]
    fn record(
        &self,
        node: &DepNode,
        index: DepNodeIndex,
        edge_count: usize,
        edges: &[DepNodeIndex],
        retained_graph: &Option<Lock<RetainedDepGraph>>,
        local: &mut LocalEncoderState,
    ) {
        local.kind_stats[node.kind.as_usize()] += 1;
        local.edge_count += edge_count;

        if let Some(retained_graph) = &retained_graph {
            // Outline the build of the full dep graph as it's typically disabled and cold.
            outline(move || {
                // Block on the lock rather than using `try_lock`: under the parallel frontend
                // several threads record nodes concurrently, and dropping a node on lock
                // contention would make the retained graph nondeterministic. Readers take a
                // clone of the graph (`retained_dep_graph`) rather than holding the lock, so
                // this never deadlocks against a reentrant `record`.
                retained_graph.lock().push(index, *node, edges);
            });
        }

        if let Some(stats) = &self.stats {
            let kind = node.kind;

            // Outline the stats code as it's typically disabled and cold.
            outline(move || {
                let mut stats = stats.lock();
                let stat =
                    stats.entry(kind).or_insert(Stat { kind, node_counter: 0, edge_counter: 0 });
                stat.node_counter += 1;
                stat.edge_counter += edge_count as u64;
            });
        }
    }

    #[inline]
    fn flush_mem_encoder(&self, local: &mut LocalEncoderState) {
        let data = &mut local.encoder.data;
        if data.len() > 64 * 1024 {
            self.file.lock().as_mut().unwrap().emit_raw_bytes(&data[..]);
            data.clear();
        }
    }

    /// Encodes a node to the current graph.
    fn encode_node(
        &self,
        index: DepNodeIndex,
        node: &NodeInfo<'_>,
        retained_graph: &Option<Lock<RetainedDepGraph>>,
        local: &mut LocalEncoderState,
    ) {
        node.encode(&mut local.encoder, index);
        self.flush_mem_encoder(&mut *local);
        self.record(&node.node, index, node.edges.len(), node.edges, retained_graph, &mut *local);
    }

    /// Encodes a node that was promoted from the previous graph, reading the node and its
    /// fingerprint directly from the previous dep graph. It expects all edges to already
    /// have a new dep node index assigned.
    #[inline]
    fn encode_promoted_node(
        &self,
        index: DepNodeIndex,
        prev_index: SerializedDepNodeIndex,
        retained_graph: &Option<Lock<RetainedDepGraph>>,
        local: &mut LocalEncoderState,
        edges: &[DepNodeIndex],
    ) {
        let node = NodeInfo {
            node: self
                .previous
                .index_to_node(prev_index)
                .expect("a promoted previous dep node must have a readable page"),
            value_fingerprint: self
                .previous
                .value_fingerprint_for_index(prev_index)
                .expect("a promoted previous dep node must have a readable page"),
            edges,
        };
        self.encode_node(index, &node, retained_graph, local);
    }

    fn finish(&self, profiler: &SelfProfilerRef, current: &CurrentDepGraph) -> FileEncodeResult {
        // `TyCtxt::finish` calls this after incremental persistence as a final
        // cleanup step. The graph has already been finalized before the query
        // cache is written so its indices can be remapped to the stable IDs in
        // the graph pages.
        if self.file.lock().is_none() {
            return Ok(0);
        }

        // Prevent more indices from being allocated.
        self.next_node_index.store(u32::MAX as u64 + 1, Ordering::SeqCst);

        let results = broadcast(|_| {
            let mut local = self.local.borrow_mut();

            // Prevent more indices from being allocated on this thread.
            local.remaining_node_index = 0;

            let data = mem::take(&mut local.encoder.data);
            self.file.lock().as_mut().unwrap().emit_raw_bytes(&data);

            LocalEncoderResult {
                kind_stats: local.kind_stats.clone(),
                node_max: local.next_node_index,
                node_count: local.node_count,
                edge_count: local.edge_count,
            }
        });

        let mut encoder = self.file.lock().take().unwrap();

        let mut kind_stats: Vec<u32> = iter::repeat_n(0, DepKind::MAX as usize + 1).collect();

        let mut node_max = 0;
        let mut node_count = 0;
        let mut edge_count = 0;

        for result in results {
            node_max = max(node_max, result.node_max);
            node_count += result.node_count;
            edge_count += result.edge_count;
            for (i, stat) in result.kind_stats.iter().enumerate() {
                kind_stats[i] += stat;
            }
        }

        // Encode the number of each dep kind encountered
        for count in kind_stats.iter() {
            count.encode(&mut encoder);
        }

        self.previous.session_count.checked_add(1).unwrap().encode(&mut encoder);

        debug!(?node_max, ?node_count, ?edge_count);
        debug!("position: {:?}", encoder.position());
        IntEncodedWithFixedSize(node_max.try_into().unwrap()).encode(&mut encoder);
        IntEncodedWithFixedSize(node_count.try_into().unwrap()).encode(&mut encoder);
        IntEncodedWithFixedSize(edge_count.try_into().unwrap()).encode(&mut encoder);
        debug!("position: {:?}", encoder.position());
        // Drop the encoder so that nothing is written after the counts.
        let graph_path = encoder.path().to_path_buf();
        let spool_result = encoder.finish();
        drop(encoder);
        let result = match spool_result {
            Err(err) => Err(err),
            Ok(_) => write_paged_graph(
                &graph_path,
                self.graph_start,
                &self.previous,
                self.page_compression,
            )
            .map_err(|err| (graph_path.clone(), err)),
        };
        let result = match result {
            Ok((position, current_to_stable)) => {
                self.current_to_stable
                    .set(current_to_stable)
                    .expect("dependency graph index mapping was already initialized");
                Ok(position)
            }
            Err(err) => Err(err),
        };
        if let Ok(position) = &result {
            // FIXME(rylev): we hardcode the dep graph file name so we
            // don't need a dependency on rustc_incremental just for that.
            profiler.artifact_size("dep_graph", "dep-graph.bin", *position as u64);
        }

        self.print_incremental_info(current, node_count, edge_count);

        result
    }

    fn serialized_index_for_cache(&self, index: DepNodeIndex) -> SerializedDepNodeIndex {
        let mapping = self.current_to_stable.get().expect(
            "dependency graph must be finalized before serializing the incremental query cache",
        );
        let stable_index = *mapping
            .get(index.as_usize())
            .expect("query cache references an unknown dependency graph index");
        assert_ne!(
            stable_index,
            u32::MAX,
            "query cache references an unused dependency graph index"
        );
        SerializedDepNodeIndex::from_u32(stable_index)
    }

    fn print_incremental_info(
        &self,
        current: &CurrentDepGraph,
        total_node_count: usize,
        total_edge_count: usize,
    ) {
        if let Some(record_stats) = &self.stats {
            let record_stats = record_stats.lock();
            // `stats` is sorted below so we can allow this lint here.
            #[allow(rustc::potential_query_instability)]
            let mut stats: Vec<_> = record_stats.values().collect();
            stats.sort_by_key(|s| -(s.node_counter as i64));

            const SEPARATOR: &str = "[incremental] --------------------------------\
                                     ----------------------------------------------\
                                     ------------";

            eprintln!("[incremental]");
            eprintln!("[incremental] DepGraph Statistics");
            eprintln!("{SEPARATOR}");
            eprintln!("[incremental]");
            eprintln!("[incremental] Total Node Count: {}", total_node_count);
            eprintln!("[incremental] Total Edge Count: {}", total_edge_count);

            if cfg!(debug_assertions) {
                let total_read_count = current.total_read_count.load(Ordering::Relaxed);
                let total_duplicate_read_count =
                    current.total_duplicate_read_count.load(Ordering::Relaxed);
                eprintln!("[incremental] Total Edge Reads: {total_read_count}");
                eprintln!("[incremental] Total Duplicate Edge Reads: {total_duplicate_read_count}");
            }

            eprintln!("[incremental]");
            eprintln!(
                "[incremental]  {:<36}| {:<17}| {:<12}| {:<17}|",
                "Node Kind", "Node Frequency", "Node Count", "Avg. Edge Count"
            );
            eprintln!("{SEPARATOR}");

            for stat in stats {
                let node_kind_ratio =
                    (100.0 * (stat.node_counter as f64)) / (total_node_count as f64);
                let node_kind_avg_edges = (stat.edge_counter as f64) / (stat.node_counter as f64);

                eprintln!(
                    "[incremental]  {:<36}|{:>16.1}% |{:>12} |{:>17.1} |",
                    format!("{:?}", stat.kind),
                    node_kind_ratio,
                    stat.node_counter,
                    node_kind_avg_edges,
                );
            }

            eprintln!("{SEPARATOR}");
            eprintln!("[incremental]");
        }
    }
}

pub(crate) struct GraphEncoder {
    profiler: SelfProfilerRef,
    status: EncoderState,
    /// In-memory copy of the dep graph; only present if `-Zquery-dep-graph` is set.
    retained_graph: Option<Lock<RetainedDepGraph>>,
}

impl GraphEncoder {
    pub(crate) fn new(
        sess: &Session,
        encoder: FileEncoder<'static>,
        prev_node_count: usize,
        previous: Arc<SerializedDepGraph>,
    ) -> Self {
        let retained_graph = sess
            .opts
            .unstable_opts
            .query_dep_graph
            .then(|| Lock::new(RetainedDepGraph::new(prev_node_count)));
        let page_compression = sess
            .opts
            .unstable_opts
            .compress_incremental
            .then(|| sess.opts.unstable_opts.artifact_compression_options());
        let status = EncoderState::new(
            encoder,
            sess.opts.unstable_opts.incremental_info,
            previous,
            page_compression,
        );
        GraphEncoder { status, retained_graph, profiler: sess.prof.clone() }
    }

    pub(crate) fn retained_dep_graph(&self) -> Option<RetainedDepGraph> {
        self.retained_graph.as_ref().map(|retained_graph| retained_graph.lock().clone())
    }

    /// Encodes a node that does not exists in the previous graph.
    pub(crate) fn send_new(
        &self,
        node: DepNode,
        value_fingerprint: Fingerprint,
        edges: &[DepNodeIndex],
    ) -> DepNodeIndex {
        let _prof_timer = self.profiler.generic_activity("incr_comp_encode_dep_graph");
        let node = NodeInfo { node, value_fingerprint, edges };
        let mut local = self.status.local.borrow_mut();
        let index = self.status.next_index(&mut *local);
        self.status.bump_index(&mut *local);
        self.status.encode_node(index, &node, &self.retained_graph, &mut *local);
        index
    }

    /// Encodes a node that exists in the previous graph, but was re-executed.
    ///
    /// This will also ensure the dep node is colored either red or green.
    pub(crate) fn send_and_color(
        &self,
        prev_index: SerializedDepNodeIndex,
        colors: &DepNodeColorMap,
        node: DepNode,
        value_fingerprint: Fingerprint,
        edges: &[DepNodeIndex],
        is_green: bool,
    ) -> DepNodeIndex {
        let _prof_timer = self.profiler.generic_activity("incr_comp_encode_dep_graph");
        let node = NodeInfo { node, value_fingerprint, edges };

        let mut local = self.status.local.borrow_mut();

        let index = self.status.next_index(&mut *local);
        let color = if is_green { DesiredColor::Green { index } } else { DesiredColor::Red };

        // Use `try_set_color` to avoid racing when `send_promoted` is called concurrently
        // on the same index.
        match colors.try_set_color(prev_index, color) {
            TrySetColorResult::Success => {}
            TrySetColorResult::AlreadyRed => panic!("dep node {prev_index:?} is unexpectedly red"),
            TrySetColorResult::AlreadyGreen { index } => return index,
        }

        self.status.bump_index(&mut *local);
        self.status.encode_node(index, &node, &self.retained_graph, &mut *local);
        index
    }

    /// Encodes a node that was promoted from the previous graph. It reads the information directly
    /// from the previous dep graph and expects all edges to already have a new dep node index
    /// assigned.
    ///
    /// Tries to mark the dep node green, and returns Some if it is now green,
    /// or None if had already been concurrently marked red.
    #[inline]
    pub(crate) fn send_promoted(
        &self,
        prev_index: SerializedDepNodeIndex,
        colors: &DepNodeColorMap,
        edges: &[DepNodeIndex],
    ) -> Option<DepNodeIndex> {
        let _prof_timer = self.profiler.generic_activity("incr_comp_encode_dep_graph");

        let mut local = self.status.local.borrow_mut();
        let index = self.status.next_index(&mut *local);

        // Use `try_set_color` to avoid racing when `send_promoted` or `send_and_color`
        // is called concurrently on the same index.
        match colors.try_set_color(prev_index, DesiredColor::Green { index }) {
            TrySetColorResult::Success => {
                self.status.bump_index(&mut *local);
                self.status.encode_promoted_node(
                    index,
                    prev_index,
                    &self.retained_graph,
                    &mut *local,
                    edges,
                );
                Some(index)
            }
            TrySetColorResult::AlreadyRed => None,
            TrySetColorResult::AlreadyGreen { index } => Some(index),
        }
    }

    pub(crate) fn finish(&self, current: &CurrentDepGraph) -> FileEncodeResult {
        let _prof_timer = self.profiler.generic_activity("incr_comp_encode_dep_graph_finish");

        self.status.finish(&self.profiler, current)
    }

    pub(crate) fn serialized_index_for_cache(&self, index: DepNodeIndex) -> SerializedDepNodeIndex {
        self.status.serialized_index_for_cache(index)
    }
}
