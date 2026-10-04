"""Summarize census_benchmark.py results as Markdown tables.

Usage: summarize_census.py RESULTS.json [RESULTS.json ...]
"""

import json
import statistics
import sys
from pathlib import Path

G = 2**30
M = 2**20


def mean(values):
    return statistics.mean(values) if values else float("nan")


def final_links(phase, binary_name):
    links = phase["measurement"]["links"]
    # Long link commands arrive through an @response file, which hides `-o` from the wrapper.
    # An incremental phase's only link is the final one.
    if len(links) == 1 and not links[0]["output"]:
        yield links[0]
        return
    for link in links:
        name = Path(link["output"]).name
        if name == binary_name or (name.startswith(binary_name + "-") and "." not in name):
            yield link


def phase_table(projects):
    print("| Project | Arm | Target GiB | Clean s | Touch s | Example edit s | Library edit s "
          "| Final link s | Peak rustc RSS GiB | Peak linker RSS GiB |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for project, record in projects.items():
        for arm, data in record["arms"].items():
            phases = {p["phase"]: p for p in data["phases"]}
            def wall(prefix):
                return mean([p["measurement"]["wall_seconds"] for n, p in phases.items()
                             if n.startswith(prefix)])
            rss = lambda kind: max(p["measurement"]["sampled_process_tree_rss_peak_by_kind_bytes"]
                                   .get(kind, 0) for p in phases.values()) / G
            incremental = [p for n, p in phases.items() if n != "clean"]
            links = [l["seconds"] for p in incremental
                     for l in final_links(p, record["binary_name"])]
            size = data["phases"][-1]["target_census"]
            print(f"| {project} | {arm} | {size['allocated_unique_inode_bytes'] / G:.3f} "
                  f"| {phases['clean']['measurement']['wall_seconds']:.1f} "
                  f"| {phases['touch']['measurement']['wall_seconds']:.1f} "
                  f"| {wall('edit'):.1f} | {wall('libedit'):.1f} | {mean(links):.2f} "
                  f"| {rss('rustc'):.2f} | {max(rss('wild_linker'), rss('lld_linker')):.2f} |")


def category_table(projects, arm):
    rows = {}
    totals = {}
    for project, record in projects.items():
        if arm not in record["arms"]:
            continue
        census = record["arms"][arm]["phases"][0]["target_census"]
        totals[project] = census["allocated_unique_inode_bytes"]
        for category, size in census["allocated_by_category"].items():
            # `.rlib` files are archives in the ordinary arm and manifests in compact arms.
            label = "rlib" if category == "rlib_manifest" else category
            rows.setdefault(label, {})[project] = size
    names = list(totals)
    print(f"| Category ({arm}, after clean build) | " + " | ".join(f"{n} GiB (share)" for n in names) + " |")
    print("|---|" + "---:|" * len(names))
    for category, sizes in sorted(rows.items(), key=lambda item: -sum(item[1].values())):
        if sum(sizes.values()) < 0.005 * G:
            continue
        cells = [f"{sizes.get(n, 0) / G:.3f} ({sizes.get(n, 0) / totals[n]:.0%})" for n in names]
        print(f"| {category} | " + " | ".join(cells) + " |")
    print("| **total** | " + " | ".join(f"**{totals[n] / G:.3f}**" for n in names) + " |")


def member_table(projects, arm):
    print("| Project | Link | Objects | Objects never read | Metadata never read | "
          "Payload blocks read | Decoded MiB | Link s |")
    print("|---|---|---:|---:|---:|---:|---:|---:|")
    for project, record in projects.items():
        data = record["arms"].get(arm)
        if not data:
            continue
        samples = [("final (edit1)", l) for p in data["phases"] if p["phase"] == "edit1"
                   for l in final_links(p, record["binary_name"])]
        tests = data.get("unit_tests", {}).get("measurement", {}).get("links", [])
        samples += [(f"test: {Path(l['output']).name.rsplit('-', 1)[0]}", l) for l in tests
                    if l["compact_stats"] and l["compact_stats"]["objects"] >= 100]
        for label, link in samples:
            s = link["compact_stats"]
            if not s or not s["objects"]:
                continue
            print(f"| {project} | {label} | {s['objects']} "
                  f"| {s['unread_objects']} ({s['unread_objects'] / s['objects']:.1%}) "
                  f"| {s['unread_metadata_bytes'] / M:.1f} MiB "
                  f"({s['unread_metadata_bytes'] / max(s['metadata_bytes'], 1):.1%}) "
                  f"| {s['read_blocks']}/{s['blocks']} | {s['decoded_bytes'] / M:.0f} "
                  f"| {link['seconds']:.2f} |")


def link_phase_table(projects, arm, top=6):
    """Wild --time top-level phases for the edit1 final link (milliseconds)."""
    for project, record in projects.items():
        data = record["arms"].get(arm)
        if not data:
            continue
        for phase in data["phases"]:
            if phase["phase"] != "edit1":
                continue
            for link in final_links(phase, record["binary_name"]):
                rows = []
                for line in link["time_report"]:
                    text = line.lstrip("│├└┌─┴ ")
                    parts = text.split(None, 1)
                    try:
                        rows.append((float(parts[0]), parts[1]))
                    except (ValueError, IndexError):
                        continue
                rows.sort(reverse=True)
                print(f"- {project}/{arm}: " + ", ".join(f"{name} {ms:.0f} ms" for ms, name in rows[:top]))


def gdb_table(projects):
    print("| Project | Arm | Line info | Type info | Read errors |")
    print("|---|---|---|---|---:|")
    for project, record in projects.items():
        for arm, data in record["arms"].items():
            if "gdb" in data:
                g = data["gdb"]
                print(f"| {project} | {arm} | {'yes' if g['line_found'] else 'no'} "
                      f"| {'yes' if g['type_found'] else 'no'} | {g['read_errors']} |")


def main():
    projects = {}
    for path in sys.argv[1:]:
        for project, record in json.loads(Path(path).read_text())["projects"].items():
            if record.get("arms") and all(a.get("phases") for a in record["arms"].values()):
                # Runs made before the on-demand reader was reverted named this arm `lazy`.
                if "lazy" in record["arms"]:
                    record["arms"]["compressed"] = record["arms"].pop("lazy")
                record["binary_name"] = {"bevy": "compression_probe", "polars": "compression_probe",
                                         "nushell": "nu"}[project]
                projects[project] = record
    print("## Phases\n")
    phase_table(projects)
    for arm in ("ordinary", "compressed"):
        print(f"\n## Target census: {arm}\n")
        category_table(projects, arm)
    print("\n## Wild member use (compressed arm)\n")
    member_table(projects, "compressed")
    print("\n## Debugger check (gdb, after the last edit)\n")
    gdb_table(projects)
    print("\n## Largest Wild --time entries, edit1 final link\n")
    for arm in ("ordinary", "compressed"):
        link_phase_table(projects, arm)


if __name__ == "__main__":
    main()
