#!/usr/bin/env python3
"""precise-sweep: delete everything in a Cargo target dir that the *current* build
configurations do not use, without forcing a rebuild.

Prototype / research code.  Dry-run by default.  See research-precise-sweep.md.

How it works
------------
For every build configuration you list with --cfg (e.g. "build", "build --release",
"test --no-run --all-targets", "check --all-targets") it runs

    CARGO_LOG=cargo::core::compiler::fingerprint=debug \\
        cargo <cfg> --message-format=json

and records the *set of units* the build needs:

  * `fingerprint at: <profile>/.fingerprint/<pkg>-<hash>/<kind>-<name>`  (cargo's own
    debug log; one line per unit, printed for fresh units too; INTERNAL, unstable
    format but the only place that names *every* unit's fingerprint dir, including
    bins whose uplifted filename has no hash).   New layout: `.../build/<pkg>/<hash>/
    fingerprint/<kind>-<name>`.
  * `compiler-artifact` `filenames`/`executable` and `build-script-executed` `out_dir`
    from --message-format=json  (public, stable) -- used as a second source of hashes
    (union => can only make the keep-set larger, i.e. safer).
  * incremental dirs: local (path) lib units are resolved *exactly*: the incremental
    dir name is `<crate>-<base36(StableCrateId, 13 digits)>` and `rustc -Zls=root
    <rmeta>` prints the StableCrateId.  Local bin/test/example/build-script units
    cannot be resolved exactly (their `-C metadata` is not recoverable for fresh
    units), so we keep the N most recently modified remaining dirs per crate name,
    where N is the number of such current units.  Incremental dirs never affect
    freshness, only the speed of the next *edit* build of that crate.

Everything else under `<profile>/{.fingerprint,deps,build,examples,incremental}`
whose name carries a 16-hex unit hash that is not in the keep-set is deleted.

Locking: takes an exclusive non-blocking flock on every `.cargo*lock` file in each
profile dir it modifies (the same files cargo 1.98 uses), so it refuses to run while
a build is active and blocks builds while it deletes.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HASH_RE = re.compile(r"-([0-9a-f]{16})(?:\.|$)")
B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def b36(n: int, width: int = 13) -> str:
    s = ""
    while n:
        n, r = divmod(n, 36)
        s = B36[r] + s
    return s.rjust(width, "0")


def unit_hash(name: str) -> str | None:
    m = HASH_RE.search(name)
    return m.group(1) if m else None


def tree_size(path: Path, seen: set) -> int:
    """Allocated bytes under path, counting each inode once."""
    total = 0
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if os.path.isdir(p) and not os.path.islink(p):
            try:
                stack.extend(Path(p) / e for e in os.listdir(p))
            except OSError:
                pass
            continue
        key = (st.st_dev, st.st_ino)
        if key in seen:
            continue
        seen.add(key)
        total += st.st_blocks * 512
    return total


def fmt(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return str(n)


class Run:
    """Result of running one configuration."""

    def __init__(self, cfg: str):
        self.cfg = cfg
        self.fp_paths: list[Path] = []  # every "fingerprint at:" path
        self.json_paths: list[Path] = []  # filenames / executable / out_dir
        self.units: list[dict] = []  # compiler-artifact messages
        self.fresh = 0
        self.total = 0
        self.compiling_lines = 0
        self.returncode = 0


def run_cfg(cfg: str, manifest: str | None, target_dir: str | None, env_extra: dict) -> Run:
    r = Run(cfg)
    argv = ["cargo", *shlex.split(cfg), "--message-format=json"]
    if manifest:
        argv += ["--manifest-path", manifest]
    env = dict(os.environ)
    env.update(env_extra)
    env["CARGO_LOG"] = "cargo::core::compiler::fingerprint=debug"
    env["CARGO_TERM_COLOR"] = "never"
    env["NO_COLOR"] = "1"
    if target_dir:
        env["CARGO_TARGET_DIR"] = target_dir
    p = subprocess.run(argv, env=env, capture_output=True, text=True)
    r.returncode = p.returncode
    if p.returncode != 0:
        sys.stderr.write(p.stderr[-2000:])
        raise SystemExit(f"cargo {cfg} failed (exit {p.returncode}); refusing to sweep")
    for line in p.stderr.splitlines():
        m = re.search(r"fingerprint at: (\S.*)$", line)
        if m:
            r.fp_paths.append(Path(m.group(1).strip()))
        if re.match(r"\s*(Compiling|Checking|Building) ", line):
            r.compiling_lines += 1
    for line in p.stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reason = msg.get("reason")
        if reason == "compiler-artifact":
            r.units.append(msg)
            r.total += 1
            r.fresh += bool(msg.get("fresh"))
            for f in msg.get("filenames", []):
                r.json_paths.append(Path(f))
            if msg.get("executable"):
                r.json_paths.append(Path(msg["executable"]))
        elif reason == "build-script-executed" and msg.get("out_dir"):
            r.json_paths.append(Path(msg["out_dir"]))
    if not r.fp_paths:
        raise SystemExit(
            "no `fingerprint at:` lines seen; CARGO_LOG format changed or nothing built. "
            "Refusing to sweep (would delete everything)."
        )
    return r


def profile_of(path: Path) -> tuple[Path, str] | None:
    """Map a path inside a build-dir to (profile_dir, layout).

    legacy: <profile>/.fingerprint/<pkg>-<h>/<file>        -> profile = parent of .fingerprint
    v2:     <profile>/build/<pkg>/<h>/fingerprint/<file>   -> profile = 4 above the file
    """
    parts = path.parts
    for i, part in enumerate(parts):
        if part == ".fingerprint":
            return Path(*parts[:i]), "legacy"
    for i in range(len(parts) - 2, 2, -1):
        if parts[i] == "fingerprint" and parts[i - 3] == "build" and HASH_RE.search("-" + parts[i - 1]):
            return Path(*parts[: i - 3]), "v2"
    return None


def stable_crate_id_name(crate: str, files: list[Path], env: dict) -> str | None:
    e = dict(os.environ)
    e.update(env)
    e["RUSTC_BOOTSTRAP"] = "1"
    for f in files:
        if f.suffix not in (".rmeta", ".rlib") or not f.exists():
            continue
        out = subprocess.run(["rustc", "-Zls=root", str(f)], env=e, capture_output=True, text=True).stdout
        m = re.search(r"StableCrateId\((\d+)\)", out)
        if m:
            return f"{crate}-{b36(int(m.group(1)))}"
    return None


def newest_mtime(path: Path) -> float:
    best = os.lstat(path).st_mtime
    try:
        for e in os.scandir(path):
            best = max(best, e.stat(follow_symlinks=False).st_mtime)
    except OSError:
        pass
    return best


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest-path")
    ap.add_argument("--target-dir", help="default: `cargo metadata` target_directory")
    ap.add_argument("--cfg", action="append", required=True,
                    help='cargo args to keep warm, e.g. "build", "test --no-run --all-targets"; repeatable')
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--incremental", choices=["exact+heuristic", "exact-only", "keep", "delete-all"],
                    default="exact+heuristic")
    ap.add_argument("--prune-unlisted-profiles", action="store_true",
                    help="also delete whole profile dirs no --cfg touched (DANGEROUS: they may be configs you "
                         "forgot to list)")
    ap.add_argument("--verify", action="store_true", help="after applying, re-run every --cfg and require all fresh")
    ap.add_argument("--rustup-toolchain", help="export RUSTUP_TOOLCHAIN for cargo/rustc")
    ap.add_argument("--pause-before-delete", type=float, default=0, help=argparse.SUPPRESS)  # test hook
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    env_extra = {"RUSTUP_TOOLCHAIN": a.rustup_toolchain} if a.rustup_toolchain else {}
    target_dir = a.target_dir
    if not target_dir:
        md = subprocess.run(["cargo", "metadata", "--no-deps", "--format-version=1"]
                            + (["--manifest-path", a.manifest_path] if a.manifest_path else []),
                            capture_output=True, text=True, env={**os.environ, **env_extra})
        target_dir = json.loads(md.stdout)["target_directory"]
    target = Path(target_dir).resolve()

    # ---- 1. collect current units -------------------------------------------------
    runs = [run_cfg(c, a.manifest_path, str(target), env_extra) for c in a.cfg]
    for r in runs:
        print(f"[cfg] {r.cfg!r}: {r.total} artifacts, {r.fresh} fresh, "
              f"{len(set(r.fp_paths))} unit fingerprints, {r.compiling_lines} recompiled during this run")

    keep_units: dict[Path, set[str]] = defaultdict(set)   # profile -> set(unit hashes) (legacy)
    keep_v2: dict[Path, set[Path]] = defaultdict(set)     # profile -> set(build/<pkg>/<hash> dirs)
    layouts: dict[Path, str] = {}
    for r in runs:
        for p in r.fp_paths:
            pr = profile_of(p)
            if not pr:
                print(f"  warning: cannot place {p}", file=sys.stderr)
                continue
            prof, layout = pr
            layouts[prof] = layout
            if layout == "legacy":
                # p = <profile>/.fingerprint/<pkg>-<h>/<file>
                unit = p.parent.name
                keep_units[prof].add(unit_hash(unit) or unit)
            else:
                keep_v2[prof].add(p.parent.parent)  # build/<pkg>/<h>
        # second source: hashes in artifact paths (union = safer)
        for p in r.json_paths:
            pr = profile_of(p)
            prof = None
            s = str(p)
            for cand in list(layouts):
                if s.startswith(str(cand) + os.sep):
                    prof = cand
                    break
            if prof is None:
                continue
            if layouts[prof] == "legacy":
                h = unit_hash(p.name)
                if h is None and p.name == "out":  # build-script-executed out_dir
                    h = unit_hash(p.parent.name)
                if h is None and p.parent.parent.name == "build":  # build/<pkg>-<h>/build-script-build
                    h = unit_hash(p.parent.name)
                if h:
                    keep_units[prof].add(h)
            else:
                rel = p.relative_to(prof).parts
                if len(rel) >= 3 and rel[0] == "build":
                    keep_v2[prof].add(prof / "build" / rel[1] / rel[2])

    # incremental keep set (per profile dir), from local units
    inc_exact: dict[Path, set[str]] = defaultdict(set)
    inc_count: dict[Path, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for r in runs:
        for m in r.units:
            if not m["package_id"].startswith(("path+file://", "file://")):
                continue
            if m["target"]["kind"] == ["custom-build"] and not m["filenames"]:
                continue
            files = [Path(f) for f in m["filenames"]]
            if not files:
                continue
            prof = None
            for cand in layouts:
                if str(files[0]).startswith(str(cand) + os.sep):
                    prof = cand
            if prof is None:
                continue
            crate = m["target"]["name"].replace("-", "_")
            is_lib = any(k in ("lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro") for k in m["target"]["kind"]) \
                and not m["profile"].get("test")
            if is_lib:
                nm = stable_crate_id_name(crate, files, env_extra)
                if nm:
                    inc_exact[prof].add(nm)
                    continue
            inc_count[prof][crate].add(tuple(sorted(map(str, files))))

    # ---- 2. plan --------------------------------------------------------------------
    plan: list[tuple[Path, str]] = []  # (path, category)
    unmanaged: list[Path] = []
    all_profiles = set(layouts)
    if a.prune_unlisted_profiles:
        # a "profile dir" = has .fingerprint (legacy) or .cargo-lock (both layouts), at depth 1 or 2
        cands = set()
        for d in [target, *[c for c in target.iterdir() if c.is_dir()]]:
            for c in [d, *[x for x in d.iterdir() if x.is_dir()]]:
                if c != target and ((c / ".fingerprint").is_dir() or (c / ".cargo-lock").exists()):
                    cands.add(c.resolve())
        listed = {p.resolve() for p in all_profiles}
        # keep parents of listed profiles (e.g. target/<triple>/) implicitly: only whole profile dirs are removed
        plan += [(c, "unlisted-profile") for c in sorted(cands - listed)]

    for prof in sorted(all_profiles):
        if layouts[prof] == "legacy":
            keep = keep_units[prof]
            for sub in (".fingerprint", "deps", "build", "examples"):
                d = prof / sub
                if not d.is_dir():
                    continue
                for e in sorted(d.iterdir()):
                    h = unit_hash(e.name)
                    if h is None:
                        # unhashed: build/<pkg>-<h> handled above; `.fingerprint/<pkg>-<h>` too.
                        # deps/foo.d etc. do not exist; unhashed files are left alone.
                        unmanaged.append(e)
                        continue
                    if h not in keep:
                        plan.append((e, sub))
        else:  # v2: build/<pkg>/<hash>
            b = prof / "build"
            for pkgdir in sorted(b.iterdir()) if b.is_dir() else []:
                if not pkgdir.is_dir():
                    continue
                for unit in sorted(pkgdir.iterdir()):
                    if unit not in keep_v2[prof]:
                        plan.append((unit, "build(v2)"))
        # incremental
        inc = prof / "incremental"
        if inc.is_dir() and a.incremental != "keep":
            by_crate: dict[str, list[Path]] = defaultdict(list)
            for e in inc.iterdir():
                if e.is_dir():
                    by_crate[e.name.rsplit("-", 1)[0]].append(e)
                else:
                    unmanaged.append(e)
            for crate, dirs in by_crate.items():
                if a.incremental == "delete-all":
                    plan += [(d, "incremental") for d in dirs]
                    continue
                rest = [d for d in dirs if d.name not in inc_exact[prof]]
                n_heur = len(inc_count[prof].get(crate, ()))
                if a.incremental == "exact-only":
                    n_heur = len(rest)  # keep all unresolved
                rest.sort(key=newest_mtime, reverse=True)
                plan += [(d, "incremental") for d in rest[n_heur:]]

    # ---- 3. report ------------------------------------------------------------------
    seen: set = set()
    sizes: dict[str, int] = defaultdict(int)
    for path, cat in plan:
        sizes[cat] += tree_size(path, seen)
    total_before = tree_size(target, set())
    print(f"\ntarget dir: {target}  ({fmt(total_before)} allocated)")
    print("profile dirs (kept): " + ", ".join(f"{p.relative_to(target)} [{layouts[p]}]" for p in sorted(all_profiles)))
    for cat, sz in sorted(sizes.items()):
        n = sum(1 for _, c in plan if c == cat)
        print(f"  {'delete' if a.apply else 'would delete':12s} {cat:16s} {n:4d} entries  {fmt(sz)}")
    print(f"  {'TOTAL':12s} {'':16s} {len(plan):4d} entries  {fmt(sum(sizes.values()))}")
    if a.verbose:
        for path, cat in plan:
            print(f"    - [{cat}] {path.relative_to(target)}")
        for u in unmanaged:
            print(f"    ? unmanaged (kept): {u.relative_to(target)}")

    if not a.apply:
        print("\n(dry run; pass --apply to delete)")
        return 0

    # ---- 4. delete under lock ----------------------------------------------------------
    if a.pause_before_delete:
        import time
        time.sleep(a.pause_before_delete)
    locks = []
    try:
        for prof in sorted(all_profiles):
            for lf in sorted(prof.glob(".cargo*lock")):
                fd = open(lf, "a")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise SystemExit(f"cargo is running (could not lock {lf}); aborting without deleting")
                locks.append(fd)
        for path, cat in plan:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        # v2: prune now-empty build/<pkg> dirs
        for prof in all_profiles:
            b = prof / "build"
            if layouts[prof] == "v2" and b.is_dir():
                for pkgdir in b.iterdir():
                    if pkgdir.is_dir() and not any(pkgdir.iterdir()):
                        pkgdir.rmdir()
    finally:
        for fd in locks:
            fd.close()
    total_after = tree_size(target, set())
    print(f"\nreclaimed {fmt(total_before - total_after)}  ({fmt(total_before)} -> {fmt(total_after)})")

    if a.verify:
        ok = True
        for c in a.cfg:
            r = run_cfg(c, a.manifest_path, str(target), env_extra)
            good = r.fresh == r.total and r.compiling_lines == 0
            ok &= good
            print(f"[verify] {c!r}: {r.fresh}/{r.total} fresh, {r.compiling_lines} recompiled -> {'OK' if good else 'NOT FRESH'}")
        return 0 if ok else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
