"""Summarize repeated compression profile builds and inspect retained artifacts."""
import argparse, collections, hashlib, json, statistics, struct
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory
    data = json.loads((root / "results.json").read_text())
    groups = collections.defaultdict(list)
    inspected = []
    for run in data["runs"]:
        assert run["input_unchanged"] and run["dependency_repositories_unchanged"]
        is_large_run = "workspace_sources_unchanged_except_controlled_edits" in run
        if is_large_run:
            assert run["workspace_sources_unchanged_except_controlled_edits"]
        for phase in run["phases"]:
            assert phase["runtime_oracle"]["exit_code"] == 0
            if is_large_run:
                assert phase["runtime_oracle"]["stdout_matches"]
            for c in phase["cargo_commands"]:
                assert c["exit_code"] == 0 and not c["rustc_audit"]["audit_errors"]
            if phase["name"] == "noop":
                assert sum(c["rustc_audit"]["count"] for c in phase["cargo_commands"]) == 0
                if is_large_run or phase["disk_allocated_growth_bytes"] != 0:
                    validation = phase["noop_validation"]
                    assert validation["output_paths_unchanged"] and validation["rewritten_outputs_content_identical"]
                    assert validation["other_file_metadata_unchanged"]
                assert phase["disk_logical_growth_bytes"] == 0
            groups[(run["workload"], run["arm"], phase["name"])].append(phase)
        directory = Path(run["target_dir"]).parent
        totals = collections.defaultdict(lambda: dict(files=0, logical=0, allocated=0, packed=0, decoded=0))
        seen = set()
        inc_inodes, other_inodes = {}, set()
        entries = [json.loads(line) for line in (directory / "final-file-census.jsonl").read_text().splitlines()]
        entries = [entry for entry in entries if entry.get("kind") == "file"]
        target = Path(run["target_dir"])
        profile_directory = "debug" if run["profile"] == "dev" else "release"
        def is_incremental(path):
            relative = path.relative_to(target) if path.is_relative_to(target) else None
            return relative is not None and relative.parts[:2] == (profile_directory, "incremental")
        for entry in entries:
            identity = (entry["device"], entry["inode"])
            if is_incremental(Path(entry["path"])):
                inc_inodes[identity] = entry
            else:
                other_inodes.add(identity)
        shared = inc_inodes.keys() & other_inodes
        for entry in entries:
            identity = (entry["device"], entry["inode"])
            if identity in seen: continue
            seen.add(identity)
            path = Path(entry["path"])
            scope = "shared/" if identity in shared else "incremental/" if identity in inc_inodes else "artifacts/"
            kind = scope + (path.name if path.suffix == ".bin" else path.suffix or "executable-or-other")
            row = totals[kind]
            row["files"] += 1; row["logical"] += entry["logical_bytes"]; row["allocated"] += entry["allocated_bytes"]
            if "is_compressed" in entry:
                if entry["is_compressed"]:
                    row["packed"] += 1; row["decoded"] += entry["packed_decoded_bytes"]
            elif path.is_file():
                with path.open("rb") as stream: header = stream.read(64)
                if header[:8] == b"RUSTZRL1":
                    assert len(header) == 64
                    row["packed"] += 1; row["decoded"] += struct.unpack_from("<Q", header, 24)[0]
        incremental_scope = dict(
            files=len(inc_inodes), logical_bytes=sum(e["logical_bytes"] for e in inc_inodes.values()),
            allocated_bytes=sum(e["allocated_bytes"] for e in inc_inodes.values()),
            shared_with_other_scope_allocated_bytes=sum(e["allocated_bytes"] for k,e in inc_inodes.items() if k in other_inodes),
        )
        assert sum(row["allocated"] for row in totals.values()) == run["phases"][-1]["disk_after"]["allocated_unique_inode_bytes"]
        assert sum(row["logical"] for row in totals.values()) == run["phases"][-1]["disk_after"]["logical_unique_inode_bytes"]
        inspected.append(dict(workload=run["workload"], arm=run["arm"], repetition=run["repetition"], classes=dict(totals), incremental_scope=incremental_scope))
    summary = []
    for (workload, arm, phase), rows in groups.items():
        result = dict(workload=workload, profile=data["runs"][0]["profile"], arm=arm, phase=phase, n=len(rows))
        fields = {
            "wall_seconds": lambda r:r["cargo_wall_seconds_sum"],
            "cpu_seconds": lambda r:r["child_user_cpu_seconds_sum"]+r["child_system_cpu_seconds_sum"],
            "allocated_bytes": lambda r:r["disk_after"]["allocated_unique_inode_bytes"],
            "logical_bytes": lambda r:r["disk_after"]["logical_unique_inode_bytes"],
            "rss_peak_bytes": lambda r:r["sampled_process_tree_rss_peak_bytes"],
            "disk_peak_bytes": lambda r:max(r["sampled_target_tmp_allocated_peak_bytes"],r["disk_before"]["allocated_unique_inode_bytes"],r["disk_after"]["allocated_unique_inode_bytes"]),
        }
        for name, getter in fields.items():
            values = [getter(r) for r in rows]
            result[name] = dict(median=statistics.median(values), min=min(values), max=max(values), samples=values)
        summary.append(result)
    paired = []
    comparisons = []
    by_run = {(r["workload"], r["arm"], r["repetition"]):r for r in data["runs"]}
    for run in data["runs"]:
        for baseline_arm in ["dedup_off", "incremental_only", "legacy"]:
            baseline = by_run.get((run["workload"], baseline_arm, run["repetition"]))
            if baseline is None or run["arm"] == baseline_arm: continue
            if baseline_arm == "incremental_only" and not run["arm"].startswith("both_"): continue
            if baseline_arm == "legacy" and run["arm"] not in {"fast", "balanced", "small"}: continue
            for phase, base in zip(run["phases"], baseline["phases"]):
                assert phase["name"] == base["name"]
                row = dict(workload=run["workload"], baseline=baseline_arm, arm=run["arm"], repetition=run["repetition"], phase=phase["name"],
                    wall_ratio=phase["cargo_wall_seconds_sum"]/base["cargo_wall_seconds_sum"],
                    allocated_saving=1-phase["disk_after"]["allocated_unique_inode_bytes"]/base["disk_after"]["allocated_unique_inode_bytes"])
                comparisons.append(row)
                if baseline_arm == "dedup_off": paired.append(row)
    comparison_groups = collections.defaultdict(list)
    for row in comparisons:
        comparison_groups[(row["workload"], row["baseline"], row["arm"], row["phase"])].append(row)
    comparison_summary = []
    for (workload, baseline, arm, phase), rows in comparison_groups.items():
        row = dict(workload=workload, baseline=baseline, arm=arm, phase=phase, n=len(rows))
        for metric in ["wall_ratio", "allocated_saving"]:
            values = [r[metric] for r in rows]
            row[metric] = dict(median=statistics.median(values), min=min(values), max=max(values), samples=values)
        comparison_summary.append(row)
    out = dict(analyzer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), paired_to_dedup_off=paired, paired_comparisons=comparisons, paired_summary=comparison_summary, completed_runs=len(data["runs"]), all_runtime_and_noop_checks_passed=True, summary=summary, artifact_inspection=inspected)
    (root / "profile-summary.json").write_text(json.dumps(out,indent=2)+"\n")
    print("workload arm phase n allocated_MiB wall_s CPU_s RSS_MiB")
    for row in summary:
        if row["phase"] == "noop": continue
        print(row["workload"],row["arm"],row["phase"],row["n"],
              *[round(row[k]["median"] / (1048576 if "bytes" in k else 1),4) for k in ["allocated_bytes","wall_seconds","cpu_seconds","rss_peak_bytes"]])

if __name__ == "__main__": main()
