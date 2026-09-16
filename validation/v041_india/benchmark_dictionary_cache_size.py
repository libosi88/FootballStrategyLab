"""Replay a frozen complete alias stream into bounded disposable SQLite trials."""
from pathlib import Path
import argparse
import ctypes
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from lab.common import atomic_json, canonical, read_json, sha, source_fingerprint
from lab.standard_atoms import AtomCatalog


def memory_bytes():
    from ctypes import wintypes
    class Memory(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
             'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
             'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD)
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    value = Memory()
    value.cb = ctypes.sizeof(value)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {'rss_bytes': value.WorkingSetSize, 'peak_rss_bytes': value.PeakWorkingSetSize}


def logical_identity(connection):
    checksum = hashlib.sha256()
    counts = {}
    for table, query in (
        ('atoms', 'SELECT id,key,body,groups,feature FROM atoms ORDER BY id'),
        ('aliases', 'SELECT rowid,key,atom_id,body,groups FROM aliases ORDER BY rowid'),
    ):
        count = 0
        for row in connection.execute(query):
            checksum.update((canonical((table, row)) + '\n').encode('utf-8'))
            count += 1
        counts[table] = count
    return {'sha256': checksum.hexdigest(), **counts}


def child(args):
    if source_fingerprint() != args.engine_hash:
        raise ValueError('Engine changed; child not started')
    source = sqlite3.connect(args.source.resolve().as_uri() + '?mode=ro', uri=True)
    total = source.execute('SELECT COUNT(*) FROM aliases').fetchone()[0]
    report_path = args.output / (args.child_label + '.json')
    report = {'status': 'RUNNING', 'policy': args.child_label, 'completed_aliases': 0,
              'total_aliases': total, 'pid': os.getpid(), 'commit_every': 512,
              'source': str(args.source), 'engine_hash': args.engine_hash}
    try:
        with AtomCatalog(args.database, True) as catalog:
            catalog.conn.execute('PRAGMA journal_mode=DELETE').fetchone()
            catalog.conn.execute('PRAGMA synchronous=FULL')
            if args.child_label == 'cache_64mib':
                catalog.conn.execute('PRAGMA cache_size=-65536')
            report['pragmas'] = {name: catalog.conn.execute('PRAGMA ' + name).fetchone()[0]
                                 for name in ('journal_mode', 'synchronous', 'cache_size', 'page_size')}
            start, cpu = time.perf_counter(), time.process_time()
            count = 0
            for rowid, body, groups in source.execute('SELECT rowid,body,groups FROM aliases ORDER BY rowid'):
                if count % 512 == 0:
                    catalog.commit()
                    report.update(completed_aliases=count, wall_seconds=time.perf_counter()-start,
                                  cpu_seconds=time.process_time()-cpu, **memory_bytes())
                    atomic_json(report_path, report)
                    if time.time() >= args.deadline:
                        break
                atom = json.loads(body)
                catalog.add(atom, groups, 100)
                count += 1
            catalog.commit()
            report.update(status='COMPLETE' if count == total else 'TIME_LIMIT_PARTIAL',
                          completed_aliases=count, wall_seconds=time.perf_counter()-start,
                          cpu_seconds=time.process_time()-cpu, **memory_bytes())
            report['logical_identity'] = logical_identity(catalog.conn)
            report['database_bytes'] = args.database.stat().st_size
        report['peak_rss_bytes'] = max(report['peak_rss_bytes'], memory_bytes()['peak_rss_bytes'])
        report['engine_unchanged'] = source_fingerprint() == args.engine_hash
        atomic_json(report_path, report)
        print(canonical(report), flush=True)
    finally:
        source.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine-hash', required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--child-label', choices=('cache_default', 'cache_64mib'))
    parser.add_argument('--database', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT/'validation/v041_india/dictionary_cache_size_9e')
    parser.add_argument('--deadline', type=float)
    args = parser.parse_args()
    if args.child_label:
        return child(args)
    start = time.time()
    if source_fingerprint() != args.engine_hash:
        raise ValueError('Engine mismatch; no trial started')
    args.output.mkdir(parents=True, exist_ok=True)
    run = read_json(ROOT/'validation/v041_india/standard_run.json')
    source = args.source or Path(run['physical_jobdir'])/'mining/PRE_GIVE/atoms.sqlite3'
    marker = read_json(source.parent/'dictionary_complete.json')
    source_hash = sha(source)
    if source_hash != marker['database_sha256']:
        raise ValueError('Source PRE_GIVE dictionary is not frozen at the marker hash')
    connection = sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)
    try:
        source_identity = logical_identity(connection)
    finally:
        connection.close()
    temporary_parent = Path('D:/FootballStrategyLab_workspace/v041_india_profile').resolve()
    temporary_parent.mkdir(parents=True, exist_ok=True)
    deadline = start + 300
    report = {'scope': 'One paired complete frozen PRE_GIVE alias-rowid replay using production AtomCatalog.add. No search, no live source/configuration mutation.',
              'engine_hash': args.engine_hash, 'source': str(source), 'source_sha256': source_hash,
              'source_identity': source_identity, 'time_limit_seconds': 300,
              'comparison': 'Both DELETE/FULL; only temporary connection cache_size differs.',
              'trials': [], 'interrupted_trials': []}
    atomic_json(args.output/'report.json', report)
    with tempfile.TemporaryDirectory(prefix='cache_size_pair_', dir=temporary_parent) as temporary:
        directory = Path(temporary).resolve()
        assert temporary_parent in directory.parents
        for label in ('cache_default', 'cache_64mib'):
            if time.time() >= deadline:
                report['interrupted_trials'].append({'label': label, 'reason': 'overall_time_limit_before_start'})
                break
            command = [sys.executable, '-B', str(Path(__file__).resolve()), '--engine-hash', args.engine_hash,
                       '--source', str(source), '--child-label', label,
                       '--database', str(directory/(label+'.sqlite3')), '--output', str(args.output), '--deadline', str(deadline)]
            print('CACHE_TRIAL_BEGIN '+label, flush=True)
            with (args.output/(label+'.log')).open('w', encoding='utf-8') as log:
                process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                    timeout=max(1, deadline-time.time())+15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            trial = read_json(args.output/(label+'.json'))
            if process.returncode or not trial:
                raise RuntimeError('Trial failed: '+label)
            if trial['status'] == 'COMPLETE':
                if trial['logical_identity'] != source_identity:
                    raise AssertionError('Complete replay did not preserve atoms/aliases/rowid/IDs/body/groups: '+label)
                trial['full_logical_identity_matches_source'] = True
            report['trials'].append(trial)
            atomic_json(args.output/'report.json', report)
            print('CACHE_TRIAL_END '+canonical(trial), flush=True)
    report['wall_seconds_including_preflight'] = time.time()-start
    report['source_unchanged'] = sha(source) == source_hash
    report['engine_unchanged'] = source_fingerprint() == args.engine_hash
    assert report['source_unchanged'] and report['engine_unchanged']
    complete = len(report['trials']) == 2 and all(row['status'] == 'COMPLETE' for row in report['trials'])
    report['status'] = 'PAIRED_COMPLETE' if complete else 'PARTIAL_TIME_LIMIT'
    if complete:
        baseline, larger = report['trials']
        report['wall_speedup_64mib'] = baseline['wall_seconds']/larger['wall_seconds']
        report['cpu_speedup_64mib'] = baseline['cpu_seconds']/larger['cpu_seconds']
    report['limitations'] = ['Single ordered pair; active India worker shares disk/CPU.',
        'Replays final unique aliases once each, not every historical repeated add from W/C dictionary construction.',
        'No synchronization/journal change; larger cache applies only to two disposable experimental databases.',
        'Results do not request or imply changing/restarting the current production task.']
    atomic_json(args.output/'report.json', report)
    print(canonical({'status': report['status'], 'report': str(args.output/'report.json'),
                     'wall_speedup_64mib': report.get('wall_speedup_64mib'), 'source_unchanged': True, 'engine_unchanged': True}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
