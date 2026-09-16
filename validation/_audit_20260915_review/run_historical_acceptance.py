"""Explicit synthetic historical S0-S7 acceptance in an isolated workspace."""
import argparse
import sys
import time
import traceback
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / 'tests'))
from lab.common import DEFAULT, ROOT, VERSION, atomic_json, read_json, sha, source_fingerprint
from lab.data import inspect
from lab.pipeline import run
from lab.release import release_fingerprint
from lab.store import Store
from test_pipeline_standard import nonempty_rows
from test_v031 import write_csv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=('empty', 'nonempty'), required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if ROOT.resolve() not in output.parents:
        raise ValueError('Evidence must remain inside the project.')
    output.mkdir(parents=True, exist_ok=False)
    started = time.time()
    engine = source_fingerprint()
    release = release_fingerprint()
    report = {'status': 'RUNNING', 'case': args.case, 'synthetic': True,
              'version': VERSION, 'engine_hash': engine, 'release_hash': release,
              'research_objective': 'historical', 'real_orders_sent': 0}
    atomic_json(output / 'acceptance.json', report)
    try:
        league, rows = nonempty_rows()
        if args.case == 'empty':
            chosen = {f'synthetic-{year}-0' for year in (2022, 2023, 2024)}
            rows = [row for row in rows if row['sId'] in chosen]
        source = output / 'input' / 'synthetic.csv'
        write_csv(source, rows)
        original = sha(source)
        expected_matches = len({row['sId'] for row in rows})
        config = {**DEFAULT, 'standard_node_budget': 0 if args.case == 'empty' else 10,
                  'acceptance_scope': 'SYNTHETIC_HISTORICAL_' + args.case.upper()}
        assert config['research_objective'] == 'historical'
        assert config['holdout_months'] == 0
        assert config['live_enabled'] is False and config['second_slot_enabled'] is False
        store = Store(output / 'workspace')
        job_id = store.create(league, config, inspect([str(source)], store.root / 'import_cache'))
        job = store.root / job_id
        report['jobdir'] = str(job)
        atomic_json(output / 'acceptance.json', report)
        print('HISTORICAL_' + args.case.upper() + '_JOB=' + str(job), flush=True)
        result = run(store.root, job_id)
        assert result is not None, 'Pipeline did not finish.'
        assert sha(source) == original, 'Synthetic input changed.'
        assert len(read_json(job / 'labels.json')) == expected_matches
        assert read_json(job / 'holdout_plan.json')['status'] != 'SPLIT'
        assert len(result['coverage']['directions']) == 16
        assert result['standard_review']['complete']
        stages = read_json(job / 'workflow_stages.json')['stages']
        if args.case == 'empty':
            assert result['state'] == 'HISTORICAL_RESEARCH_COMPLETE', result['state']
            assert result['coverage']['remaining'] == 0
            assert result['selected'] == 0
            assert result['empty_roster_chain_verified']
            assert all(v for k, v in result['gates'].items() if k != 'nonempty_roster')
            assert all(stage['status'] == 'PASS' for stage in stages.values())
        else:
            assert result['state'] == 'PARTIAL_RESULT', result['state']
            assert result['coverage']['remaining'] > 0
            assert result['selected'] > 0 and result['backup_selected'] > 0
            assert result['gates']['full_standard_search'] is False
            assert all(v for k, v in result['gates'].items() if k != 'full_standard_search')
            assert all(stages[key]['status'] == 'PASS' for key in ('S4', 'S5', 'S6', 'S7'))
            for folder in (job / 'results', job / 'results' / 'lower_risk'):
                trigger = read_json(folder / 'trigger_verification.json')
                assert trigger['status'] == 'PASS'
                assert trigger['execution_economics']['status'] == 'PASS'
                assert trigger['golden_signals'] > 0
        packaging = read_json(job / 'packaging_verification.json')
        assert packaging['fresh_zip_extract']
        assert all(command['exit_code'] == 0 for command in packaging['commands'])
        for key in ('developer', 'audit'):
            assert sha(job / 'packages' / result['packages'][key]) == result['packages'][key + '_sha256']
        assert source_fingerprint() == engine and release_fingerprint() == release, 'Source changed during acceptance.'
        report.update(status='PASS', state=result['state'], matches=expected_matches,
                      selected=result['selected'], backup_selected=result['backup_selected'],
                      min_profit=config['min_profit'], holdout_months=0, input_unchanged=True,
                      source_unchanged=True, directions=16,
                      remaining=result['coverage']['remaining'], gates=result['gates'],
                      stages={key: value['status'] for key, value in stages.items()},
                      fresh_zip_extract=True, package_commands=packaging['commands'],
                      packages=result['packages'],
                      scope='Synthetic historical software acceptance; nonempty case intentionally uses ten nodes per direction. Not real-league research or full release certification.')
    except BaseException as error:
        report.update(status='FAIL', error=str(error), traceback=traceback.format_exc())
        raise
    finally:
        report['seconds'] = round(time.time() - started, 3)
        atomic_json(output / 'acceptance.json', report)
        print({key: report.get(key) for key in ('status', 'case', 'state', 'matches', 'selected', 'remaining', 'seconds')}, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
