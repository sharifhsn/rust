"""Summarize completed native artifact benchmarks and inspect retained artifacts."""

import argparse
from collections import defaultdict
import json
from pathlib import Path
from statistics import median
import struct


def artifact_census(run):
    target = Path(run['target_dir'])
    temp = Path(run['temp_dir'])
    seen = set()
    kinds = defaultdict(lambda: {'files': 0, 'logical_bytes': 0, 'allocated_bytes': 0,
                                 'compressed_files': 0, 'decoded_bytes': 0})
    packed = []
    leftovers = []
    for root in (target, temp):
        if not root.is_dir():
            raise RuntimeError(f'Artifact inspection requires retained target: {root}')
        for path in root.rglob('*'):
            if path.name.startswith('rustc-artifacts') or '.artifact-compression-' in path.name:
                leftovers.append(str(path))
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            key = (stat.st_dev, stat.st_ino)
            if key in seen:
                continue
            seen.add(key)
            kind = path.suffix if path.suffix in {'.rmeta', '.rlib', '.dylib', '.a', '.o'} else 'other'
            row = kinds[kind]
            row['files'] += 1
            row['logical_bytes'] += stat.st_size
            row['allocated_bytes'] += stat.st_blocks * 512
            if kind in {'.rmeta', '.rlib'}:
                with path.open('rb') as stream:
                    header = stream.read(64)
                if header[:8] == b'RUSTZRL1':
                    raw_len = struct.unpack_from('<Q', header, 24)[0]
                    row['compressed_files'] += 1
                    row['decoded_bytes'] += raw_len
                    packed.append({'path': str(path), 'stored_bytes': stat.st_size,
                                   'decoded_bytes': raw_len, 'header_hex': header.hex()})
                else:
                    row['decoded_bytes'] += stat.st_size
    assert not leftovers, leftovers
    return {'workload': run['workload'], 'arm': run['arm'], 'repetition': run['repetition'],
            'kinds': dict(kinds), 'packed': packed, 'temporary_artifact_leftovers': leftovers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path)
    parser.add_argument('--inspect-artifacts', action='store_true')
    args = parser.parse_args()
    data = json.loads(args.results.read_text())
    runs = data['runs']
    grouped = defaultdict(list)
    counts = {'runs': len(runs), 'runtime_oracles': 0, 'audited_rustc_invocations': 0,
              'noop_rustc_invocations': 0, 'noop_disk_growth_bytes': 0}
    for run in runs:
        assert run['dependency_repositories_unchanged'] and run['input_unchanged']
        counts['audited_rustc_invocations'] += run['all_rustc_invocations_audited']
        for phase in run['phases']:
            assert phase['runtime_oracle']['stdout_matches']
            counts['runtime_oracles'] += 1
            processes = []
            gaps = []
            for command in phase['cargo_commands']:
                assert command['exit_code'] == 0 and not command['rustc_audit']['audit_errors']
                if phase['name'] == 'noop':
                    counts['noop_rustc_invocations'] += command['rustc_invocation_count']
                samples = [json.loads(line) for line in Path(command['sample_file']).read_text().splitlines()]
                processes.extend(p['rss_bytes'] for s in samples for p in s['processes'])
                gaps.extend(b['relative_seconds'] - a['relative_seconds'] for a, b in zip(samples, samples[1:]))
            if phase['name'] == 'noop':
                counts['noop_disk_growth_bytes'] += abs(phase['disk_allocated_growth_bytes'])
            grouped[run['workload'], run['arm'], phase['name']].append({
                'wall_seconds': phase['cargo_wall_seconds_sum'],
                'cpu_seconds': phase['child_user_cpu_seconds_sum'] + phase['child_system_cpu_seconds_sum'],
                'logical_bytes': phase['disk_after']['logical_unique_inode_bytes'],
                'allocated_bytes': phase['disk_after']['allocated_unique_inode_bytes'],
                # The end snapshots are observed lower bounds too. Include them so
                # a fast final link cannot make the reported peak smaller than rest.
                'peak_allocated_lower_bound_bytes': max(
                    phase['sampled_target_tmp_allocated_peak_bytes'],
                    phase['disk_before']['allocated_unique_inode_bytes'],
                    phase['disk_after']['allocated_unique_inode_bytes']),
                'peak_tree_rss_lower_bound_bytes': phase['sampled_process_tree_rss_peak_bytes'],
                'peak_process_rss_lower_bound_bytes': max(processes, default=0),
                'median_sample_gap_seconds': median(gaps) if gaps else None,
            })
    assert counts['noop_rustc_invocations'] == 0
    assert counts['noop_disk_growth_bytes'] == 0
    rows = []
    for (workload, arm, phase), samples in sorted(grouped.items()):
        metrics = {}
        for key in samples[0]:
            values = [s[key] for s in samples if s[key] is not None]
            metrics[key] = {'median': median(values), 'min': min(values), 'max': max(values)} if values else None
        rows.append({'workload': workload, 'arm': arm, 'phase': phase,
                     'repetitions': len(samples), 'metrics': metrics})
    out = {'source': str(args.results.resolve()), 'validation': counts, 'rows': rows,
           'note': 'RSS and disk peaks are sampled lower bounds; allocation does not resolve APFS shared extents. CPU uses reaped-child resource accounting. Timings include concurrent sampling.'}
    args.results.with_name('summary.json').write_text(json.dumps(out, indent=2) + '\n')
    if args.inspect_artifacts:
        inspected = [artifact_census(run) for run in runs]
        args.results.with_name('artifact-census.json').write_text(json.dumps(inspected, indent=2) + '\n')
    print(json.dumps(counts, indent=2))
    lookup = {(r['workload'], r['arm'], r['phase']): r['metrics'] for r in rows}
    print('| Workload | Baseline MiB | Compressed MiB | Allocated saving | Clean time delta | Edit time delta |')
    print('|---|---:|---:|---:|---:|---:|')
    for workload in sorted({r['workload'] for r in rows}):
        b, c = [lookup[workload, arm, 'clean_build'] for arm in ('dedup_off', 'compress_on')]
        eb, ec = [lookup[workload, arm, 'edited_rebuild'] for arm in ('dedup_off', 'compress_on')]
        size_b, size_c = b['allocated_bytes']['median'], c['allocated_bytes']['median']
        clean = c['wall_seconds']['median'] / b['wall_seconds']['median'] - 1
        edit = ec['wall_seconds']['median'] / eb['wall_seconds']['median'] - 1
        print(f'| {workload} | {size_b / 2**20:.2f} | {size_c / 2**20:.2f} | {100 * (1-size_c/size_b):.1f}% | {100*clean:+.1f}% | {100*edit:+.1f}% |')


if __name__ == '__main__':
    main()
