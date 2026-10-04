#!/usr/bin/env python3
"""Combine completed large-project matrices into compact, reproducible tables."""

import argparse
import hashlib
import json
import statistics
from pathlib import Path


ARMS = ["dedup_off", "incremental_only", "both_fast", "both_balanced"]
LABELS = ["Raw", "Caches balanced", "Both fast", "Both balanced"]


def stats(values):
    return dict(median=statistics.median(values), min=min(values), max=max(values), samples=values)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    matrices = []
    oracle_count = 0
    noop_count = 0
    compiler_hash = None
    for directory in args.directories:
        results = json.loads((directory / "results.json").read_text())
        manifest = json.loads((directory / "manifest.json").read_text())
        summary = json.loads((directory / "profile-summary.json").read_text())
        assert manifest["arms"] == ARMS
        expected = len(manifest["workloads"]) * len(ARMS) * manifest["repetitions"]
        assert len(results["runs"]) == summary["completed_runs"] == expected
        assert summary["all_runtime_and_noop_checks_passed"]
        current_hash = manifest["rustc_driver_libraries"]
        if compiler_hash is None:
            compiler_hash = current_hash
        assert current_hash == compiler_hash
        matrices.append(dict(path=str(directory.resolve()), completed_runs=expected,
                             manifest_sha256=hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest(),
                             results_sha256=hashlib.sha256((directory / "results.json").read_bytes()).hexdigest(),
                             summary_sha256=hashlib.sha256((directory / "profile-summary.json").read_bytes()).hexdigest()))
        for run in results["runs"]:
            assert run["workspace_sources_unchanged_except_controlled_edits"]
            for phase in run["phases"]:
                assert phase["runtime_oracle"]["stdout_matches"]
                oracle_count += 1
                if phase["name"] == "noop":
                    validation = phase["noop_validation"]
                    assert all(validation[key] for key in ["output_paths_unchanged", "rewritten_outputs_content_identical", "other_file_metadata_unchanged"])
                    noop_count += 1
            census = Path(run["target_dir"]).parent / "final-file-census.jsonl"
            # Retained totals can be called target sizes only after proving TMP is empty.
            for line in census.read_text().splitlines():
                entry = json.loads(line)
                assert entry["kind"] != "file" or not Path(entry["path"]).is_relative_to(run["temp_dir"])
        for workload in manifest["workloads"]:
            lookup = {(s["arm"], s["phase"]): s for s in summary["summary"] if s["workload"] == workload}
            paired = {(s["arm"], s["phase"]): s for s in summary["paired_summary"] if s["workload"] == workload and s["baseline"] == "dedup_off"}
            for arm, label in zip(ARMS, LABELS):
                matching = [r for r in results["runs"] if r["workload"] == workload and r["arm"] == arm]
                assert len(matching) == manifest["repetitions"]
                clean_audits = [r["phases"][0]["cargo_commands"][0]["rustc_audit"] for r in matching]
                row = dict(workload=workload, arm=arm, label=label, n=len(matching),
                           rustc_invocations=stats([a["count"] for a in clean_audits]),
                           incremental_codegen_invocations=stats([sum(i["incremental_codegen"] for i in a["invocations"]) for a in clean_audits]),
                           phases={phase: lookup[(arm, phase)] for phase in ["clean_build", "noop", "semantic_edit_1", "semantic_edit_2"]},
                           paired_to_raw={phase: paired[(arm, phase)] for phase in ["clean_build", "semantic_edit_1", "semantic_edit_2"]} if arm != "dedup_off" else {})
                rows.append(row)
    output = dict(matrices=matrices, completed_runs=sum(m["completed_runs"] for m in matrices),
                  runtime_oracles=oracle_count, noops=noop_count, final_tmp_files=0,
                  summarizer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), rows=rows)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "large-summary.json").write_text(json.dumps(output, indent=2) + "\n")
    lines = ["# Generated large-project results", "", f"{output['completed_runs']} fresh-target runs; {oracle_count} checked runtime executions; {noop_count} no-ops with zero logged recompilations.", "", "Sizes are retained allocated GiB after the indicated phase. Times and resources are medians of repeated runs; paired percentages use per-repetition ratios. Ranges are sample extrema, not confidence intervals.", ""]
    for phase, title in [("clean_build", "Clean builds"), ("semantic_edit_1", "First semantic edit"), ("semantic_edit_2", "Second semantic edit")]:
        lines += [f"## {title}", "", "| Project | Mode | n | Retained GiB | Wall s | Paired wall change, median [range] | CPU s | RSS peak GiB | Disk peak GiB |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
        for row in rows:
            values = row["phases"][phase]
            pct = "—"
            if row["paired_to_raw"]:
                r = row["paired_to_raw"][phase]["wall_ratio"]
                pct = f"{100*(r['median']-1):+.1f}% [{100*(r['min']-1):+.1f}, {100*(r['max']-1):+.1f}]"
            lines.append(f"| {row['workload']} | {row['label']} | {row['n']} | {values['allocated_bytes']['median']/2**30:.3f} | {values['wall_seconds']['median']:.3f} | {pct} | {values['cpu_seconds']['median']:.2f} | {values['rss_peak_bytes']['median']/2**30:.3f} | {values['disk_peak_bytes']['median']/2**30:.3f} |")
        lines.append("")
    lines += ["## Retained size reductions", "", "| Project | Mode | Raw GiB | Compressed GiB | Paired saving, median [range] |", "|---|---|---:|---:|---:|"]
    for row in rows:
        if not row["paired_to_raw"]:
            continue
        baseline = next(r for r in rows if r["workload"] == row["workload"] and r["arm"] == "dedup_off")
        p = row["paired_to_raw"]["semantic_edit_2"]["allocated_saving"]
        raw = baseline["phases"]["semantic_edit_2"]["allocated_bytes"]["median"] / 2**30
        compressed = row["phases"]["semantic_edit_2"]["allocated_bytes"]["median"] / 2**30
        lines.append(f"| {row['workload']} | {row['label']} | {raw:.3f} | {compressed:.3f} | {100*p['median']:.2f}% [{100*p['min']:.2f}, {100*p['max']:.2f}] |")
    (args.out / "tables.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({k: output[k] for k in ["completed_runs", "runtime_oracles", "noops", "final_tmp_files"]}))


if __name__ == "__main__":
    main()
