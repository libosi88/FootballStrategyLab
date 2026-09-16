"""Read-only production audit: synthetic policy counterexamples, not real-league research."""
from pathlib import Path
import copy
from datetime import date, timedelta
import json
import sys
import tempfile
import traceback

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from lab.common import DEFAULT, check_config, source_fingerprint, sha, timestamp, settlement, digest
from lab.release import release_files, release_fingerprint
from lab.contracts import bind_rule
from lab.features import SignalEngine
from lab.selection import Quotes, metrics, dispatch, portfolio_metrics
from lab.portfolio_search import PortfolioSearch
from lab.research_standard import assign_segments, direction_availability, research_gate_reasons, portfolio_feasible
from lab.standard_spec import compact_atoms, max_conditions_for
from test_core import event, TEST_CONTRACT


def manifest():
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in release_files(ROOT)}


def compact_metric(m):
    return {k: m.get(k) for k in ('n', 'net', 'drawdown', 'drawdown_match', 'year_net',
                                  'segment_net', 'segment_counts', 'history_days')}


def build_quotes(schedules, directions, minute_values):
    events, labels = [], {}
    for year, schedule in sorted(schedules.items()):
        for day_offset, outcomes in enumerate(schedule):
            day = (date(year, 1, 1) + timedelta(days=day_offset)).isoformat()
            sid = f'synthetic-{year}-{day_offset:03d}'
            mid = len(labels)
            kickoff = timestamp(day + ' 12:00')
            labels[sid] = {'eligible': True, 'final': [2, 1], 'year': year,
                           'date': day, 'kickoff': kickoff, 'league': TEST_CONTRACT['league']}
            for index, outcome in sorted(outcomes.items()):
                is_total = directions[index].endswith(('OVER', 'UNDER'))
                line = (10 if outcome else 14) if is_total else (2 if outcome else 6)
                minute = minute_values[index]
                e = event(len(events), line=line, w0=95, w1=85, ts=kickoff + minute)
                e.update(sid=sid, mid=mid, market=0 if is_total else 1,
                         minute=minute, minute_raw=str(minute))
                events.append(e)
    assign_segments(labels)
    return events, labels


def derive_rules(events, labels, directions, minute_values):
    rules = [bind_rule({'id': f'r{i}', 'direction': d, 'priority': i,
                        'conditions': [{'feature': 'minute', 'op': 'eq', 'value': minute_values[i]}]},
                       TEST_CONTRACT) for i, d in enumerate(directions)]
    policy = {'feature_version': 'v3', 'cross_stale_minutes': 5,
              'cross_stale_minutes_prematch': 1440, 'prematch_trigger_status': 'any'}
    engine = SignalEngine(rules, contract=TEST_CONTRACT, execution_policy=policy)
    for sid in labels:
        engine.mark_history_complete(sid)
    signals = []
    for e in events:
        signals.extend(engine.feed(e))
    quotes = Quotes(events, labels, 100, 5, minute_close=True)
    for i, r in enumerate(rules):
        ts = [quotes.trade(s['eid'], s['side'], reject_pregoal=True)
              for s in signals if s['strategy_id'] == r['id']]
        ts = [t for t in ts if t is not None]
        r['_trades'] = [ts] * 5
        r['_execution_priority'] = i
    return rules, signals


def gate_rule(r, config, labels, availability):
    ts = r['_trades'][4]
    m = metrics(ts, 100, sorted({l['year'] for l in labels.values()}), list(availability))
    reasons, tags = research_gate_reasons(ts, m, [], config, 100, availability)
    if m['net_i'] <= int(float(config['min_profit']) * 200):
        reasons.append('below frozen profit threshold')
    return {'accepted': not reasons, 'reasons': reasons, 'tags': tags,
            'metrics': compact_metric(m)}


def solve(rules, config, labels, directory):
    years = sorted({l['year'] for l in labels.values()})
    def scorer(rs):
        orders, _ = dispatch(rs, 4, config['match_cap'], priority_mode='frozen_rule_priority')
        m = portfolio_metrics(orders, 100, years)
        return {'raw': m, 'stress': [], 'historical': m}
    result = PortfolioSearch(rules, scorer, config, 100, directory, 'SYNTHETIC_AUDIT',
                             config['portfolio_min_return_drawdown_ratio']).run()
    chosen = [r for r in rules if r['id'] in result['chosen']]
    orders, rejected = dispatch(chosen, 4, config['match_cap'], True, 'frozen_rule_priority')
    return {'chosen': result['chosen'], 'search_status': result['status'],
            'feasible': result['score']['feasible'],
            'metrics': compact_metric(scorer(chosen)['historical']),
            'rejected_signal_count': len(rejected), 'orders': orders}


def portfolio_segment_case():
    directions = ['LIVE_GIVE', 'LIVE_GIVE']
    first = [{0: False, 1: True}] * 10 + [{0: True}] * 12 + [{1: False}] * 6
    later = [item for _ in range(10) for item in ({0: True, 1: False}, {1: True})]
    later += [{1: True}] * 8 + [{0: True}] * 12
    events, labels = build_quotes({2022: first, 2023: later, 2024: later}, directions, [5, 20])
    rules, signals = derive_rules(events, labels, directions, [5, 20])
    config = check_config({})
    avail = direction_availability(events, labels, directions, config)
    gates = {r['id']: gate_rule(r, config, labels, avail[r['direction']]) for r in rules}
    eligible = [r for r in rules if gates[r['id']]['accepted']]
    with tempfile.TemporaryDirectory(prefix='portfolio_', dir=OUT) as td:
        result = solve(eligible, config, labels, Path(td))
    orders = result.pop('orders')
    result.update(single_rule_gates=gates, eligible_rule_count=len(eligible),
                  signal_count=len(signals), available_matches=avail,
                  selected_has_losing_segment=any(v < 0 for v in result['metrics']['segment_net'].values()))
    result['defect_reproduced'] = len(result['chosen']) == 2 and result['feasible'] and result['selected_has_losing_segment']
    with (OUT / 'portfolio_segment_orders.jsonl').open('x', encoding='utf-8') as stream:
        for t in orders:
            stream.write(json.dumps(t, ensure_ascii=False) + '\n')
    return result


def complementary_filter_case():
    directions = ['LIVE_GIVE', 'LIVE_OVER']
    schedule = [{0: True, 1: False}] * 20 + [{0: False, 1: True}] * 20 + [{0: True, 1: True}] * 10
    events, labels = build_quotes({2022: schedule, 2023: schedule, 2024: schedule}, directions, [5, 5])
    rules, _ = derive_rules(events, labels, directions, [5, 5])
    config = check_config({})
    avail = direction_availability(events, labels, directions, config)
    gates = {r['id']: gate_rule(r, config, labels, avail[r['direction']]) for r in rules}
    with tempfile.TemporaryDirectory(prefix='complement_', dir=OUT) as td:
        base = Path(td)
        current = solve([r for r in rules if gates[r['id']]['accepted']], config, labels, base / 'filtered')
        joint = portfolio_metrics(dispatch(rules, 4, config['match_cap'], priority_mode='frozen_rule_priority')[0],
                                  100, [2022, 2023, 2024])
    current.pop('orders')
    feasible = portfolio_feasible(joint['net_i'], joint['drawdown_match_i'], config['portfolio_min_return_drawdown_ratio'])
    return {'scope': 'DESIGN_TRADEOFF: standalone quality requirement excludes complementary high-risk rules',
            'single_rule_gates': gates, 'filtered_search': current,
            'both_together': compact_metric(joint), 'joint_feasible': feasible,
            'all_individuals_fail_only_risk': all(g['tags'] == ['HIGH_RISK_ALTERNATIVE'] for g in gates.values())}


def recent_segment_cutoff_case():
    schedule = [{0: True}] * 60 + [{0: False}] * 20
    events, labels = build_quotes({2022: schedule, 2023: schedule, 2024: [{0: False}] * 20},
                                  ['LIVE_GIVE'], [5])
    # Keep these synthetic research segments fixed to isolate the 15% eligibility policy,
    # independently of the end-anchor date construction.
    for lab in labels.values():
        lab['segment'] = f"Y{lab['year']}"
    rules, _ = derive_rules(events, labels, ['LIVE_GIVE'], [5])
    r = rules[0]
    # Interleave winners/losses in early years so each period's drawdown is not the issue.
    for year in (2022, 2023):
        ts = [t for t in r['_trades'][4] if t['year'] == year]
        ts.sort(key=lambda t: t['sort_time'])
        for i, t in enumerate(ts):
            positive = i % 4 != 3
            t.update(line=2 if positive else 6, pnl=settlement(2 if positive else 6, 95, 1, 0, 100))
    config = check_config({})
    low = {'Y2022': 100, 'Y2023': 100, 'Y2024': 35}
    high = {'Y2022': 100, 'Y2023': 100, 'Y2024': 36}
    before = gate_rule(r, config, labels, low)
    after = gate_rule(r, config, labels, high)
    return {'scope': 'Synthetic per-rule policy boundary, not a full research pipeline',
            'recent_available_share_below': 35 / 235, 'recent_available_share_above': 36 / 236,
            'below_15_percent': before, 'above_15_percent': after,
            'same_rule_trades_in_both_checks': True,
            'accepted_with_losing_recent_segment': before['accepted'] and before['metrics']['segment_net']['Y2024'] < 0,
            'one_nontrigger_match_changes_gate': before['accepted'] and not after['accepted']}


def compact_scope_case():
    def domain(feature):
        return (-200, 200) if feature != 'water' else (50, 150)
    def values(feature):
        return set(range(-3, 100))
    atoms = list(compact_atoms('LIVE_GIVE', 100, domain, values))
    required = {'feature': 'samewater', 'op': 'le', 'value': -15}
    return {'max_conditions': max_conditions_for(DEFAULT), 'atom_count_fixture': len(atoms),
            'samewater_atoms': [a for a in atoms if a['feature'] == 'samewater'],
            'explicit_samewater_drop_015_present': required in atoms,
            'has_pulse': any(a.get('pulse') for a in atoms),
            'has_timed_span': any('span' in a for a in atoms),
            'has_three_step_path': any(a['feature'].startswith('path3') for a in atoms),
            'scope': 'Declared compact grammar limitation, not an undisclosed v3_full enumeration defect'}


def main():
    target = OUT / 'rationality_probe_results.json'
    if target.exists():
        raise FileExistsError('Preserve existing audit evidence')
    before = manifest()
    report = {'scope': 'RATIONALITY_AUDIT_TARGETED_SYNTHETIC_NOT_FULL_PROJECT_ACCEPTANCE',
              'engine_hash': source_fingerprint(), 'release_hash': release_fingerprint(),
              'real_jobs_started': False, 'production_source_modified': False, 'cases': {}}
    for name, fn in [('portfolio_segment_gate', portfolio_segment_case),
                     ('complementary_singleton_filter', complementary_filter_case),
                     ('recent_segment_15_percent', recent_segment_cutoff_case),
                     ('compact_scope', compact_scope_case)]:
        try:
            report['cases'][name] = fn()
        except Exception as error:
            report['cases'][name] = {'probe_error': str(error), 'traceback': traceback.format_exc()}
    report['source_unchanged'] = before == manifest()
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report['source_unchanged'] and all('probe_error' not in c for c in report['cases'].values()) else 2


if __name__ == '__main__':
    raise SystemExit(main())
