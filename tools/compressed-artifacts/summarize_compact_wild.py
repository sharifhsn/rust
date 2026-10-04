"""Summarize the recorded Bevy experiment without running a build."""

import json
import os
from pathlib import Path
import re
from statistics import median

OUT = Path(os.environ.get("COMPACT_LAB_OUTPUT", Path(__file__).resolve().parents[2] /
    "docs/target-directory-size/measurements/2026-09-27-compact-wild"))


def counters(path, marker):
    records = [json.loads(m.group(1)) for m in re.finditer(
        re.escape(marker) + r" (\{[^\n]*\})", path.read_text())]
    return {"records": len(records), "totals": {
        key: sum(record[key] for record in records) for key in records[0]
    } if records else {}}


def categories(files):
    inodes = {}
    for file in files:
        if file["kind"] != "file":
            continue
        key = (file["device"], file["inode"])
        entry = inodes.setdefault(key, {"paths": [], "logical": 0, "allocated": 0})
        entry["paths"].append(file["path"])
        entry["logical"] = max(entry["logical"], file["logical_bytes"])
        entry["allocated"] = max(entry["allocated"], file["allocated_bytes"])
    result = {}
    for entry in inodes.values():
        paths = entry["paths"]
        if any(p.endswith(".rco") for p in paths):
            category = "compact object store (including incremental aliases)"
        elif any(p.endswith(".rlib") for p in paths):
            category = "libraries or library manifests"
        elif any(p.endswith(".rmeta") for p in paths):
            category = "Rust metadata"
        elif any("/incremental/" in p and p.endswith(".o") for p in paths):
            category = "separate incremental objects"
        elif any("/incremental/" in p for p in paths):
            category = "other incremental state"
        else:
            category = "executables, build scripts, native outputs and other files"
        row = result.setdefault(category, {"inodes": 0, "logical_bytes": 0, "allocated_bytes": 0})
        row["inodes"] += 1
        row["logical_bytes"] += entry["logical"]
        row["allocated_bytes"] += entry["allocated"]
    return result


def incremental_categories(files):
    result, seen = {}, set()
    for file in files:
        key = (file["device"], file["inode"])
        path = Path(file["path"])
        if file["kind"] != "file" or "incremental" not in path.parts or key in seen:
            continue
        seen.add(key)
        name = "objects" if path.suffix == ".o" else path.name
        row = result.setdefault(name, {"logical_bytes": 0, "allocated_bytes": 0, "inodes": 0})
        row["logical_bytes"] += file["logical_bytes"]
        row["allocated_bytes"] += file["allocated_bytes"]
        row["inodes"] += 1
    return result


def main():
    summary = json.loads((OUT / "bevy-summary.json").read_text())
    confirmation = json.loads((OUT / "bevy-confirmation.json").read_text())
    assert {a["arm"] for a in summary["arms"]} == {"ordinary", "wrapper", "compact"}
    trials = {r["trial"] for r in confirmation}
    assert len(trials) >= 4 and len(confirmation) == len(trials) * 3
    result = {"arms": {}, "comparisons": {}}
    for arm in summary["arms"]:
        name = arm["arm"]
        phases = {p["phase"]: p for p in arm["phases"]}
        assert set(phases) == {"clean", "noop", "edit1", "edit2", "edit3"}
        assert phases["noop"]["measurement"]["rustc_invocation_count"] == 0
        assert all(phases[e]["measurement"]["rustc_invocation_count"] == 1
                   for e in ("edit1", "edit2", "edit3"))
        assert all(p["measurement"]["exit_code"] == 0 and p["runtime"]["exit"] == 0
                   for p in phases.values())
        runs = [r for r in confirmation if r["arm"] == name]
        assert len(runs) == len(trials) and {r["trial"] for r in runs} == trials
        census = phases["edit3"]["census"]
        files = json.loads((OUT / "bevy" / name / "files.json").read_text())
        breakdown = categories(files["files"])
        assert sum(r["allocated_bytes"] for r in breakdown.values()) == census["allocated_unique_inode_bytes"]
        stats = OUT / "bevy" / name / "stats.log"
        result["arms"][name] = {
            "retained_allocated_bytes": census["allocated_unique_inode_bytes"],
            "retained_logical_bytes": census["logical_unique_inode_bytes"],
            "hardlink_alias_bytes": census["hardlink_alias_bytes"],
            "clean_wall_seconds": phases["clean"]["measurement"]["wall_seconds"],
            "noop_wall_seconds": phases["noop"]["measurement"]["wall_seconds"],
            "clean_rustc_invocations": phases["clean"]["measurement"]["rustc_invocation_count"],
            "sampled_edit_wall_seconds": [phases[e]["measurement"]["wall_seconds"] for e in ("edit1", "edit2", "edit3")],
            "confirmation_wall_seconds": [r["wall_seconds"] for r in runs],
            "confirmation_median_wall_seconds": median(r["wall_seconds"] for r in runs),
            "confirmation_cpu_seconds": [r["user_seconds"] + r["system_seconds"] for r in runs],
            "peak_rss_by_phase": {p: r["measurement"]["monitor"]["sampled_process_tree_rss_sum_peak_bytes"] for p, r in phases.items()},
            "peak_disk_by_phase": {p: r["measurement"]["monitor"]["sampled_target_tmp_allocated_unique_inode_peak_bytes"] for p, r in phases.items()},
            "metadata_counters": counters(stats, "RUSTC_COMPACT_METADATA"),
            "linker_counters": counters(stats, "WILD_COMPACT_STATS"),
            "allocated_categories": breakdown,
            "incremental_categories": incremental_categories(files["files"]),
        }
    for left, right in (("compact", "ordinary"), ("compact", "wrapper"), ("wrapper", "ordinary")):
        a, b = result["arms"][left], result["arms"][right]
        result["comparisons"][left + "_vs_" + right] = {
            "retained_allocated_reduction_percent": 100 * (1 - a["retained_allocated_bytes"] / b["retained_allocated_bytes"]),
            "retained_logical_reduction_percent": 100 * (1 - a["retained_logical_bytes"] / b["retained_logical_bytes"]),
            "edit_time_change_percent": 100 * (a["confirmation_median_wall_seconds"] / b["confirmation_median_wall_seconds"] - 1),
            "paired_edit_time_ratios": [
                next(r["wall_seconds"] for r in confirmation if r["arm"] == left and r["trial"] == trial) /
                next(r["wall_seconds"] for r in confirmation if r["arm"] == right and r["trial"] == trial)
                for trial in sorted(trials)
            ],
        }
    (OUT / "bevy-analysis.json").write_text(json.dumps(result, indent=2) + "\n")
    print("| Mode | Retained allocated GiB | Retained logical GiB | Clean seconds | No-op seconds | Edit median seconds | Edit range seconds |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for name, row in result["arms"].items():
        edits = row["confirmation_wall_seconds"]
        print(f'| {name} | {row["retained_allocated_bytes"] / 2**30:.3f} | '
              f'{row["retained_logical_bytes"] / 2**30:.3f} | {row["clean_wall_seconds"]:.2f} | '
              f'{row["noop_wall_seconds"]:.3f} | {row["confirmation_median_wall_seconds"]:.3f} | '
              f'{min(edits):.3f}–{max(edits):.3f} |')
    print(json.dumps(result["comparisons"], indent=2))


if __name__ == "__main__":
    main()
