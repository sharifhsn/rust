//! Attribute the debug info in a Cargo target directory to the crate whose files hold it, and
//! estimate how much of it `debug = "line-tables-only"` would remove.
//!
//! Symbol-based tools like cargo-bloat credit generic code to the crate that defines it. But
//! debug info is emitted by the crate that *compiles* the code, so a monomorphized
//! `Vec<MyType>` lives in your crate's objects with your crate's debug settings. This walks
//! every object file on disk (loose, inside `.rlib` archives, in the incremental cache, `.dwo`
//! files) and sums the bytes of its debug sections per owning crate. For ELF executables,
//! which carry a copy of everyone's DWARF, the debug sections are split by compile unit.
//!
//! `line-tables-only` still keeps every function and inlined call (backtraces need them), so
//! it removes much less from code-heavy crates than from type-heavy ones. The estimate walks
//! each unit's DIEs: functions, inlined calls and their namespaces count as kept; types,
//! variables, parameters and everything under them count as removed.
//!
//! Usage: debuginfo-census [TARGET_DIR]   (run from the workspace, defaults to ./target)

use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use object::read::archive::ArchiveFile;
use object::{Object, ObjectSection};

#[derive(Default, Clone, Copy)]
struct Bytes {
    disk: u64,
    debug: u64,
    /// Estimated bytes that `line-tables-only` would remove.
    removable: u64,
}

impl std::ops::AddAssign for Bytes {
    fn add_assign(&mut self, o: Self) {
        self.disk += o.disk;
        self.debug += o.debug;
        self.removable += o.removable;
    }
}

/// Section name without its object-format decoration: `.zdebug_info`, `__debug_info` and
/// `.debug_info.dwo` all become `debug_info`.
fn base_name(name: &str) -> Option<&str> {
    let name = name.trim_start_matches('.').trim_start_matches("__");
    let name = name.strip_suffix(".dwo").unwrap_or(name);
    let name = name.strip_prefix('z').filter(|n| n.starts_with("debug_")).unwrap_or(name);
    (name.starts_with("debug_") || name.starts_with("apple_")).then_some(name)
}

/// How a debug section responds to `line-tables-only`.
enum Fate {
    Kept,
    Removed,
    /// Names referenced from DIEs: split by which DIEs reference them.
    Strings,
    /// Shared by kept and removed entries: scales with `.debug_info`.
    Proportional,
}

fn fate(base: &str) -> Fate {
    match base {
        "debug_loc" | "debug_loclists" | "debug_pubtypes" | "apple_types" | "apple_objc" => Fate::Removed,
        "debug_line" | "debug_line_str" | "debug_ranges" | "debug_rnglists" | "debug_aranges"
        | "debug_abbrev" | "debug_frame" | "debug_addr" | "debug_pubnames" | "apple_names"
        | "apple_namespac" => Fate::Kept,
        "debug_str" | "debug_str_offsets" => Fate::Strings,
        _ => Fate::Proportional,
    }
}

/// Per compile unit: the crate it belongs to (from rustc's unit name), its `.debug_info`
/// bytes, and how many of those `line-tables-only` would remove.
struct UnitStats {
    crate_name: Option<String>,
    size: u64,
    removed: u64,
    /// String-table bytes referenced only by removed DIEs, first seen in this unit.
    strings_removed: u64,
}

/// Unit stats, plus the total bytes of all strings the DIEs reference.
fn unit_stats(file: &object::File) -> (Vec<UnitStats>, u64) {
    let dwo = file.section_by_name(".debug_info.dwo").is_some();
    let load = |id: gimli::SectionId| -> Result<Cow<[u8]>, gimli::Error> {
        let name = if dwo { id.dwo_name().unwrap_or(id.name()) } else { id.name() };
        Ok(file
            .section_by_name(name)
            .and_then(|s| s.uncompressed_data().ok())
            .unwrap_or(Cow::Borrowed(&[])))
    };
    let Ok(sections) = gimli::DwarfSections::load(load) else { return (Vec::new(), 0) };
    let endian = if file.is_little_endian() { gimli::RunTimeEndian::Little } else { gimli::RunTimeEndian::Big };
    let dwarf = sections.borrow(|s| gimli::EndianSlice::new(s, endian));
    let mut stats = Vec::new();
    // String offset -> (length, referenced by a kept DIE, first unit to reference it).
    let mut strings: HashMap<u64, (u64, bool, usize)> = HashMap::new();
    let mut headers = dwarf.units();
    while let Ok(Some(header)) = headers.next() {
        let size = header.length_including_self() as u64;
        let Ok(unit) = dwarf.unit(header) else { continue };
        // rustc names units "<crate root path>/@/<crate>.<hash>-cgu.<n>".
        let crate_name = unit
            .name
            .map(|n| n.to_string_lossy().into_owned())
            .and_then(|n| n.split_once("/@/").map(|(_, cgu)| cgu.split('.').next().unwrap_or("").to_string()));
        // Each DIE's bytes run to the next DIE's offset; a DIE is kept only if it and all its
        // ancestors are kinds that `line-tables-only` still emits.
        let mut removed = 0;
        let mut ancestors: Vec<bool> = Vec::new();
        let mut previous: Option<(u64, bool)> = None;
        let mut entries = unit.entries();
        while let Ok(Some(entry)) = entries.next_dfs() {
            let depth = entry.depth();
            let offset = entry.offset().0 as u64;
            if let Some((start, false)) = previous {
                removed += offset.saturating_sub(start);
            }
            ancestors.truncate(depth.max(0) as usize);
            let parent_kept = ancestors.last().copied().unwrap_or(true);
            let kept = parent_kept
                && matches!(
                    entry.tag(),
                    gimli::DW_TAG_compile_unit
                        | gimli::DW_TAG_subprogram
                        | gimli::DW_TAG_inlined_subroutine
                        | gimli::DW_TAG_namespace
                );
            ancestors.push(kept);
            previous = Some((offset, kept));
            for attr in entry.attrs() {
                let offset = match attr.value() {
                    gimli::AttributeValue::DebugStrRef(o) => Some(o),
                    gimli::AttributeValue::DebugStrOffsetsIndex(i) => dwarf
                        .debug_str_offsets
                        .get_str_offset(unit.encoding().format, unit.str_offsets_base, i)
                        .ok(),
                    _ => None,
                };
                let Some(offset) = offset else { continue };
                let unit_index = stats.len();
                let e = strings.entry(offset.0 as u64).or_insert_with(|| {
                    let len = dwarf.debug_str.get_str(offset).map_or(0, |s| s.len() as u64 + 1);
                    (len, false, unit_index)
                });
                e.1 |= kept;
            }
        }
        if let Some((start, false)) = previous {
            removed += size.saturating_sub(start);
        }
        stats.push(UnitStats { crate_name, size, removed, strings_removed: 0 });
    }
    let mut total_strings = 0;
    for (len, kept, unit) in strings.into_values() {
        total_strings += len;
        if !kept {
            stats[unit].strings_removed += len;
        }
    }
    (stats, total_strings)
}

/// On-disk bytes of each fate of debug section (compressed size if compressed).
fn debug_sections(file: &object::File) -> (u64, u64, u64, u64) {
    let (mut kept, mut removed, mut strings, mut proportional) = (0, 0, 0, 0);
    for section in file.sections() {
        let (Ok(name), Some((_, size))) = (section.name(), section.file_range()) else { continue };
        let Some(base) = base_name(name) else { continue };
        match fate(base) {
            Fate::Kept => kept += size,
            Fate::Removed => removed += size,
            Fate::Strings => strings += size,
            Fate::Proportional => proportional += size,
        }
    }
    (kept, removed, strings, proportional)
}

/// Debug bytes of one object file, split across the crates of its compile units (an ELF
/// executable holds everyone's), or all credited to `default` (a crate's own objects).
fn analyze(file: &object::File, default: &str) -> Vec<(String, Bytes)> {
    let (kept, removed, strings, proportional) = debug_sections(file);
    let debug = kept + removed + strings + proportional;
    if debug == 0 {
        return Vec::new();
    }
    let (units, total_strings) = unit_stats(file);
    let total: u64 = units.iter().map(|u| u.size).sum::<u64>().max(1);
    let string_share = |x: u64| (strings as u128 * x as u128 / total_strings.max(1) as u128) as u64;
    let linked = matches!(file.kind(), object::ObjectKind::Executable | object::ObjectKind::Dynamic);
    let scale = |x: u64, num: u64| (x as u128 * num as u128 / total as u128) as u64;
    let mut out: HashMap<String, Bytes> = HashMap::new();
    if units.is_empty() {
        out.insert(default.to_string(), Bytes { debug, removable: removed, ..Default::default() });
    }
    for unit in &units {
        let name = match (&unit.crate_name, linked) {
            (Some(n), true) => n.clone(),
            (None, true) => "(C and other)".to_string(),
            (_, false) => default.to_string(),
        };
        let b = out.entry(name).or_default();
        b.debug += scale(debug, unit.size);
        b.removable += scale(removed, unit.size) + scale(proportional, unit.removed) + string_share(unit.strings_removed);
    }
    out.into_iter().collect()
}

/// The crate a file belongs to, from Cargo's and rustc's naming conventions.
fn owner(rel: &Path) -> String {
    let parts: Vec<&str> = rel.iter().filter_map(|p| p.to_str()).collect();
    if let Some(i) = parts.iter().position(|p| *p == "incremental") {
        if let Some(dir) = parts.get(i + 1) {
            return dir.rsplit_once('-').map_or(*dir, |(n, _)| n).to_string();
        }
    }
    if parts.contains(&".fingerprint") {
        return "(cargo bookkeeping)".into();
    }
    let name = parts.last().copied().unwrap_or("");
    // Build scripts and their outputs (legacy layout) are not crates you link.
    if let Some(i) = parts.iter().position(|p| *p == "build") {
        if parts.len() > i + 2 && !name.ends_with(".rlib") && !name.ends_with(".rmeta") && !name.ends_with(".o") {
            return "(build scripts)".into();
        }
    }
    let stem = name
        .strip_prefix("lib")
        .filter(|_| [".rlib", ".rmeta", ".so", ".dylib", ".a"].iter().any(|e| name.ends_with(e)))
        .unwrap_or(name);
    let crate_name = stem.split(['-', '.']).next().unwrap_or(stem);
    if crate_name.is_empty() { "(other)".into() } else { crate_name.to_string() }
}

/// Crate names of the workspace members, and of the crates that only run at build time:
/// proc macros, build dependencies, and everything only they depend on. Cargo builds those
/// with the `build-override` profile, so `[profile.dev.package."*"]` doesn't shrink them.
fn classify_crates() -> (HashSet<String>, HashSet<String>) {
    let Ok(out) = Command::new("cargo").args(["metadata", "--format-version", "1"]).output() else {
        return Default::default();
    };
    let Ok(meta) = serde_json::from_slice::<serde_json::Value>(&out.stdout) else { return Default::default() };
    let mut names: HashMap<&str, Vec<String>> = HashMap::new();
    let mut proc_macro = HashSet::new();
    for package in meta["packages"].as_array().into_iter().flatten() {
        let id = package["id"].as_str().unwrap_or("");
        for target in package["targets"].as_array().into_iter().flatten() {
            let kinds: Vec<&str> = target["kind"].as_array().into_iter().flatten().filter_map(|k| k.as_str()).collect();
            if kinds.contains(&"custom-build") {
                continue;
            }
            if kinds.contains(&"proc-macro") {
                proc_macro.insert(id);
            }
            if let Some(n) = target["name"].as_str() {
                names.entry(id).or_default().push(n.replace('-', "_"));
            }
        }
    }
    let members: Vec<&str> = meta["workspace_members"].as_array().into_iter().flatten().filter_map(|m| m.as_str()).collect();
    let mut edges: HashMap<&str, Vec<&str>> = HashMap::new();
    for node in meta["resolve"]["nodes"].as_array().into_iter().flatten() {
        let id = node["id"].as_str().unwrap_or("");
        for dep in node["deps"].as_array().into_iter().flatten() {
            let normal = dep["dep_kinds"].as_array().into_iter().flatten().any(|k| k["kind"].is_null());
            if normal {
                edges.entry(id).or_default().push(dep["pkg"].as_str().unwrap_or(""));
            }
        }
    }
    // Crates linked into your binaries: reachable through normal dependencies, not via a proc macro.
    let mut linked: HashSet<&str> = HashSet::new();
    let mut stack = members.clone();
    while let Some(id) = stack.pop() {
        if proc_macro.contains(id) || !linked.insert(id) {
            continue;
        }
        stack.extend(edges.get(id).into_iter().flatten().copied());
    }
    let workspace: HashSet<String> = members.iter().flat_map(|m| names.get(m).cloned().unwrap_or_default()).collect();
    let linked_names: HashSet<String> = linked.iter().flat_map(|m| names.get(m).cloned().unwrap_or_default()).collect();
    let build_time = names.values().flatten().filter(|n| !linked_names.contains(*n)).cloned().collect();
    (workspace, build_time)
}

fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else { return };
    for entry in entries.flatten() {
        let Ok(kind) = entry.file_type() else { continue };
        if kind.is_dir() {
            walk(&entry.path(), out);
        } else if kind.is_file() {
            out.push(entry.path());
        }
    }
}

fn gib(x: u64) -> String {
    format!("{:.2} GiB", x as f64 / (1u64 << 30) as f64)
}

fn main() {
    let root = PathBuf::from(std::env::args().nth(1).unwrap_or_else(|| "target".into()));
    let (workspace, build_time) = classify_crates();
    let mut files = Vec::new();
    walk(&root, &mut files);

    let mut seen = HashSet::new();
    let mut by_crate: HashMap<String, Bytes> = HashMap::new();
    for path in files {
        let Ok(meta) = fs::metadata(&path) else { continue };
        if !seen.insert((meta.dev(), meta.ino())) {
            continue; // hard link to a file we already counted
        }
        let disk = meta.blocks() * 512;
        let rel = path.strip_prefix(&root).unwrap_or(&path);
        let who = owner(rel);
        let Ok(data) = fs::read(&path) else { continue };
        let mut parts = Vec::new();
        if let Ok(archive) = ArchiveFile::parse(&*data) {
            for member in archive.members().flatten() {
                let Ok(bytes) = member.data(&*data) else { continue };
                if let Ok(obj) = object::File::parse(bytes) {
                    parts.extend(analyze(&obj, &who));
                }
            }
        } else if let Ok(obj) = object::File::parse(&*data) {
            parts.extend(analyze(&obj, &who));
        }
        let debug: u64 = parts.iter().map(|(_, b)| b.debug).sum();
        *by_crate.entry(who).or_default() += Bytes { disk: disk.saturating_sub(debug), ..Default::default() };
        for (name, b) in parts {
            *by_crate.entry(name).or_default() += Bytes { disk: b.debug, ..b };
        }
    }

    let group = |name: &str| -> &'static str {
        if name.starts_with('(') {
            "other"
        } else if workspace.contains(name) {
            "workspace crates"
        } else if build_time.contains(name) {
            "build-time crates"
        } else if ["std", "core", "alloc", "compiler_builtins", "panic_unwind", "hashbrown", "gimli", "addr2line"]
            .contains(&name)
        {
            "standard library"
        } else {
            "dependencies"
        }
    };
    let mut groups: HashMap<&str, Bytes> = HashMap::new();
    let mut total = Bytes::default();
    for (name, b) in &by_crate {
        *groups.entry(group(name)).or_default() += *b;
        total += *b;
    }
    let pct = |x: u64| if total.disk == 0 { 0.0 } else { 100.0 * x as f64 / total.disk as f64 };
    println!("{:<18} {:>10} {:>18} {:>32}", "", "on disk", "debug info", "line-tables-only would remove");
    for name in ["workspace crates", "dependencies", "build-time crates", "standard library", "other"] {
        let b = groups.get(name).copied().unwrap_or_default();
        println!(
            "{:<18} {:>10} {:>10} ({:>3.0}%) {:>24} ({:>3.0}%)",
            name, gib(b.disk), gib(b.debug), pct(b.debug), gib(b.removable), pct(b.removable)
        );
    }
    println!(
        "{:<18} {:>10} {:>10} ({:>3.0}%) {:>24} ({:>3.0}%)",
        "total", gib(total.disk), gib(total.debug), pct(total.debug), gib(total.removable), pct(total.removable)
    );

    let mut crates: Vec<_> = by_crate.into_iter().filter(|(_, b)| b.debug > 0).collect();
    crates.sort_by_key(|(_, b)| std::cmp::Reverse(b.removable));
    let top: usize = std::env::var("TOP").ok().and_then(|v| v.parse().ok()).unwrap_or(10);
    println!("\nCrates where line-tables-only would save the most:");
    for (name, b) in crates.iter().take(top) {
        let tag = if group(name) == "workspace crates" { " (workspace)" } else { "" };
        if std::env::var_os("RAW").is_some() {
            println!("{}\t{}\t{}\t{}", name, group(name), b.debug, b.removable);
        } else {
            println!("  {:>10} of {:>10}  {}{}", gib(b.removable), gib(b.debug), name, tag);
        }
    }
}
