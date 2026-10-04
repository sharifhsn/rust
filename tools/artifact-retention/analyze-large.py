#!/usr/bin/env python3
"""Summarize raw paired large-workspace receipts without adopting old caches."""
import argparse
import gzip
import json
from pathlib import Path
import statistics

GIB = 1024 ** 3


def census_allocation(receipt):
    inodes = {}
    with gzip.open(receipt, "rt") as stream:
        for line in stream:
            row = json.loads(line)
            inodes[(row["device"], row["inode"])] = row["allocated"]
    return sum(inodes.values())


def pin_allocation(receipt, state):
    sessions = {p for s in state["sessions"].values() for g in s["slots"].values() for p in g}
    outputs = {p for g in state["outputs"].values() for p in g}
    execution = {p for g in state["executions"].values() for p in g}
    known = set(state["units"])
    inodes = {}
    with gzip.open(receipt, "rt") as stream:
        for line in stream:
            row = json.loads(line)
            inode = (row["device"], row["inode"])
            entry = inodes.setdefault(inode, {"bytes": row["allocated"], "sessions": False,
                                             "outputs": False, "execution": False, "known": False})
            parts = Path(row["path"]).parts
            # A retained unit is profile/build/package/hash, with optional target.
            unit = next((str(Path(*parts[:n])) for n in (4, 5)
                         if len(parts) > n and parts[n-3] == "build" and len(parts[n-1]) == 16), None)
            entry["sessions"] |= unit in sessions
            entry["outputs"] |= unit in outputs
            entry["execution"] |= unit in execution
            entry["known"] |= unit in known
    result = {k: 0 for k in ["session_live", "output_only", "execution_only", "known_inactive", "outside_known_units"]}
    for entry in inodes.values():
        category = next((category for flag, category in [
            ("sessions", "session_live"), ("outputs", "output_only"),
            ("execution", "execution_only"), ("known", "known_inactive")]
                         if entry[flag]), "outside_known_units")
        result[category] += entry["bytes"]
    result["total"] = sum(result.values())
    return result


def summarize(root):
    rows = json.loads((root / "results.json").read_text())
    summary_path = root / "summary.json"
    completed = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    summary = dict(completed)
    # A terminated host can leave a complete paired history and incomplete QA.
    # Derive only histories whose entire saved trace succeeded in both arms.
    for path in root.glob("*-trace.json"):
        project = path.name.removesuffix("-trace.json")
        trace = json.loads(path.read_text())
        if project in summary or not trace:
            continue
        if not all(any(r["project"] == project and r["arm"] == arm
                       and r["configuration"] == cfg and r["label"] == mode and r["returncode"] == 0
                       for r in rows)
                   for arm in ["control", "active"] for cfg in trace for mode in ["check", "build", "test"]):
            continue
        sizes = {arm: next(r["inventory"]["allocated"] for r in reversed(rows)
                           if r["project"] == project and r["arm"] == arm and r["label"] == "test"
                           and r["configuration"] == trace[-1]) for arm in ["control", "active"]}
        summary[project] = dict(configurations=len(trace), control_bytes=sizes["control"],
                                active_bytes=sizes["active"],
                                saved_fraction=1-sizes["active"]/sizes["control"],
                                reached_100_gib=sizes["control"] >= 100*GIB)
    for project, value in summary.items():
        value["qa_complete"] = project in completed
        trace = json.loads((root / f"{project}-trace.json").read_text())
        project_rows = [r for r in rows if r["project"] == project]
        value["trace"] = trace
        value["history"] = {}
        for arm in ["control", "active"]:
            history = [r for r in project_rows if r["arm"] == arm and r["label"] in ["check", "build", "test"]]
            value["history"][arm] = dict(seconds=sum(r["seconds"] for r in history),
                                         final=history[-1]["inventory"],
                                         sampled_tree_rss_peak=max(r["sampled_tree_rss_peak"] for r in history),
                                         sampled_tree_fds_peak=max(r["sampled_tree_fds_peak"] for r in history),
                                         fresh_by_configuration=[dict(name=c["name"], commands=[
                                             {k: r[k] for k in ["label", "seconds", "fresh", "artifacts"]}
                                             for r in history if r["configuration"]["name"] == c["name"]]) for c in trace])
            receipt = root / f"{project}-{arm}-history-files.jsonl.gz"
            if receipt.exists():
                census = census_allocation(receipt)
                measured = value[arm + "_bytes"]
                value["history"][arm]["allocation_census"] = dict(
                    bytes=census, original_command_bytes=measured,
                    later_census_delta_bytes=census - measured)
            if arm == "active":
                state = history[-1]["state"]
                sessions = {p for s in state["sessions"].values() for g in s["slots"].values() for p in g}
                outputs = {p for g in state["outputs"].values() for p in g}
                execution = {p for g in state["executions"].values() for p in g}
                value["history"][arm]["root_unit_counts"] = dict(
                    session_live=len(sessions), output_only=len(outputs-sessions),
                    execution_only=len(execution-sessions-outputs),
                    known_inactive=len(set(state["units"])-sessions-outputs-execution))
                receipt = root / f"{project}-active-history-files.jsonl.gz"
                value["history"][arm]["allocation_by_root"] = (
                    pin_allocation(receipt, history[-1]["state"]) if receipt.exists() else None)
                if receipt.exists():
                    assert value["history"][arm]["allocation_by_root"]["total"] == census
                else:
                    value["history"][arm]["allocation_by_root_unavailable"] = "Per-file census absent from saved checkpoint"
        value["ordinary_feature_history"] = {
            arm: [{"configuration": r["configuration"]["name"], "allocated": r["inventory"]["allocated"]}
                  for r in project_rows if r["arm"] == arm and r["label"] == "test"
                  and r["configuration"]["name"] in {"default", "development-features"}]
            for arm in ["control", "active"]
        }
        value["warm"] = {}
        for mode in ["check", "build", "test", "clippy"]:
            samples = {arm: [r["seconds"] for r in project_rows if r["arm"] == arm
                            and r["label"].startswith("warm-") and r["label"].endswith("-" + mode)
                            and not r.get("warmup_excluded")]
                       for arm in ["control", "active"]}
            medians = {arm: statistics.median(times) if times else None for arm, times in samples.items()}
            value["warm"][mode] = dict(samples=samples, median_seconds=medians,
                                       delta_ms=(medians["active"] - medians["control"]) * 1000
                                       if all(v is not None for v in medians.values()) else None)
        value["switch_back"] = {arm: {k: r[k] for k in ["seconds", "fresh", "artifacts", "inventory"]}
                                for arm in ["control", "active"] for r in project_rows
                                if r["arm"] == arm and r["label"] == "switch-back"}
        value["sampled_transition_peaks"] = [dict(arm=r["arm"], configuration=r["configuration"]["name"],
                                                   allocated=r["sampled_disk_peak"])
                                             for r in project_rows if "sampled_disk_peak" in r]
        value["commands"] = len(project_rows)
        value["successful_commands"] = sum(r["returncode"] == 0 for r in project_rows)
        value["expected_failures"] = sum(r["returncode"] == 101 and r["label"] == "expected-compile-failure" for r in project_rows)
        value["runtime_oracles"] = sum(r["label"] == "runtime-oracle" for r in project_rows)
    return dict(projects=summary, commands=len(rows),
                successful_commands=sum(r["returncode"] == 0 for r in rows),
                warm_all_fresh=all(r["fresh"] == r["artifacts"] and r["artifacts"] > 0
                                   for r in rows if r["label"].startswith("warm-") and not r.get("warmup_excluded"))
                if any(r["label"].startswith("warm-") and not r.get("warmup_excluded") for r in rows) else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.write_text(json.dumps(summarize(args.results), indent=2) + "\n")


if __name__ == "__main__":
    main()
