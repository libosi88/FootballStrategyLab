"""Bounded real AtomCatalog.add profile; only disposable databases are written."""
from pathlib import Path
from itertools import islice
from collections import defaultdict
import argparse
import cProfile
import hashlib
import io
import json
import pstats
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from lab.common import atomic_json, canonical, sha, source_fingerprint
from lab.standard_atoms import AtomCatalog, T
from lab.standard_spec import path_atoms

COUNT = 4096
COMMIT_EVERY = 512


def add_stream(catalog):
    for index, atom in enumerate(islice(path_atoms(100, timed=True), COUNT)):
        if index % COMMIT_EVERY == 0:
            catalog.commit()
        catalog.add(atom, T, 100)
    catalog.commit()


def logical_identity(catalog):
    counts = {}
    checksum = hashlib.sha256()
    for table, query in (
        ('atoms', 'SELECT id,key,body,groups,feature FROM atoms ORDER BY id'),
        ('aliases', 'SELECT key,atom_id,body,groups FROM aliases ORDER BY key'),
    ):
        rows = 0
        for row in catalog.conn.execute(query):
            checksum.update((canonical((table, row)) + '\n').encode('utf-8'))
            rows += 1
        counts[table] = rows
    return {'sha256': checksum.hexdigest(), **counts}


def category(filename, name):
    if 'sqlite3.Connection' in name and 'commit' in name:
        return 'sqlite_commit'
    if 'sqlite3.Connection' in name or 'sqlite3.Cursor' in name:
        return 'sqlite_execute_fetch'
    if '/json/' in filename.replace('\\', '/') or name == 'canonical':
        return 'json_serialization'
    if name == 'digest' or 'hashlib' in name or 'HASH' in name:
        return 'hash'
    if filename.endswith('rules.py'):
        return 'label_and_units'
    if filename.endswith('standard_spec.py'):
        return 'T_generator'
    if filename.endswith('standard_atoms.py'):
        return 'catalog_python_and_normalization'
    return 'other_python_and_builtins'


def profile_details(profile):
    statistics_object = pstats.Stats(profile)
    exclusive = defaultdict(float)
    functions = []
    for (filename, line, name), (primitive, total, self_time, cumulative, callers) in statistics_object.stats.items():
        exclusive[category(filename, name)] += self_time
        functions.append({'file': filename, 'line': line, 'function': name,
                          'primitive_calls': primitive, 'calls': total,
                          'self_seconds': self_time, 'cumulative_seconds': cumulative})
    stream = io.StringIO()
    pstats.Stats(profile, stream=stream).sort_stats('cumulative').print_stats(30)
    return {'exclusive_seconds_by_category': dict(exclusive),
            'profile_total_seconds': statistics_object.total_tt,
            'top_cumulative_functions': sorted(functions, key=lambda row: -row['cumulative_seconds'])[:30]}, stream.getvalue()


def one_run(directory, label, synchronous=None, journal=None, profile=False):
    database = directory / (label + '.sqlite3')
    with AtomCatalog(database, True) as catalog:
        if journal:
            catalog.conn.execute('PRAGMA journal_mode=' + journal).fetchone()
        if synchronous:
            catalog.conn.execute('PRAGMA synchronous=' + synchronous)
        observed = {name: catalog.conn.execute('PRAGMA ' + name).fetchone()[0]
                    for name in ('journal_mode', 'synchronous', 'page_size', 'cache_size')}
        profiler = cProfile.Profile() if profile else None
        begin, cpu = time.perf_counter(), time.process_time()
        if profiler:
            profiler.enable()
        add_stream(catalog)
        if profiler:
            profiler.disable()
        timing = {'wall_seconds': time.perf_counter() - begin, 'cpu_seconds': time.process_time() - cpu}
        identity = logical_identity(catalog)
        result = {'label': label, 'pragmas': observed, **timing, 'logical_identity': identity,
                  'main_database_bytes_before_close': database.stat().st_size}
        if profiler:
            details, text = profile_details(profiler)
            result['profile'] = details
            return result, profiler, text
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine-hash', required=True)
    args = parser.parse_args()
    frozen = source_fingerprint()
    if args.engine_hash != frozen:
        raise ValueError('Engine mismatch; no experiment started')
    temporary_parent = Path('D:/FootballStrategyLab_workspace/v041_india_profile').resolve()
    temporary_parent.mkdir(parents=True, exist_ok=True)
    destination = ROOT / 'validation/v041_india'
    with tempfile.TemporaryDirectory(prefix='dictionary_add_', dir=temporary_parent) as temporary:
        directory = Path(temporary).resolve()
        assert temporary_parent in directory.parents
        profiled, profiler, text = one_run(directory, 'profile_default', profile=True)
        runs = []
        # Balanced order limits a simple warm-cache/order bias. None means use
        # SQLite defaults exactly as the production AtomCatalog currently does.
        policies = {
            'DEFAULT': (None, None),
            'DELETE_NORMAL': ('NORMAL', 'DELETE'),
            'WAL_FULL': ('FULL', 'WAL'),
        }
        for round_index, order in enumerate((('DEFAULT', 'DELETE_NORMAL', 'WAL_FULL'),
                                             ('WAL_FULL', 'DELETE_NORMAL', 'DEFAULT'),
                                             ('DELETE_NORMAL', 'DEFAULT', 'WAL_FULL'))):
            for policy in order:
                sync, journal = policies[policy]
                row = one_run(directory, f'{round_index}_{policy}', sync, journal)
                row['policy'] = policy
                runs.append(row)
                print(canonical({'policy': policy, 'round': round_index,
                                 'wall_seconds': row['wall_seconds'], 'logical_identity': row['logical_identity']}), flush=True)
        identities = {row['logical_identity']['sha256'] for row in [profiled, *runs]}
        assert len(identities) == 1
        report = {'scope': '4096 predeclared first T atoms through the unmodified production generator and AtomCatalog.add; fresh disposable databases; no mining/search, CSV access or production-configuration changes.',
                  'engine_hash': frozen, 'raw_T_atoms': COUNT, 'commit_every': COMMIT_EVERY,
                  'commit_calls': 9, 'scale': 100, 'profiled_default': profiled, 'unprofiled_runs': runs,
                  'unprofiled_policy_medians': {
                      policy: {'wall_seconds': statistics.median(row['wall_seconds'] for row in runs if row['policy'] == policy),
                               'cpu_seconds': statistics.median(row['cpu_seconds'] for row in runs if row['policy'] == policy)}
                      for policy in policies},
                  'all_logical_tables_identical': len(identities) == 1,
                  'same_original_atom_ids_aliases_bodies_groups_and_features': True,
                  'temporary_parent': str(temporary_parent), 'temporary_databases_disposable': True,
                  'limitations': ['cProfile adds call overhead; unprofiled repetitions are separately reported.',
                      '4096 insertions into fresh tables do not measure million-row B-tree growth, existing-dictionary resume or complete per-direction domain generation.',
                      'Temporary synchronization policies are component experiments only; current production settings are unchanged.',
                      'The active India task may share CPU and disk; finite timing differences are not full-league runtime estimates.']}
        assert source_fingerprint() == frozen
        report['engine_unchanged'] = True
        profiler.dump_stats(str(destination / 'dictionary_add_profile_9e.prof'))
        (destination / 'dictionary_add_profile_9e.txt').write_text(text, encoding='utf-8')
        atomic_json(destination / 'dictionary_add_profile_9e.json', report)
        print(canonical({'status': 'PASS', 'profile_categories': profiled['profile']['exclusive_seconds_by_category'],
                         'policy_medians': report['unprofiled_policy_medians'], 'engine_unchanged': True}), flush=True)


if __name__ == '__main__':
    main()
