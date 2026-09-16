"""Explicit historical evidence policies; no future or account authorization."""
from collections import Counter
from decimal import Decimal
from fractions import Fraction
from math import ceil

DEFAULTS = {
    'historical_policy_version': 'FSL_HISTORY_SEGMENT_SAFE_V2',
    'segment_basis': 'calendar_year', 'segment_anchor_date': '',
    'major_segment_share': '0.15', 'minor_segment_loss_policy': 'block',
    'segment_evidence_min_matches': 5, 'max_best_segment_profit_share': '1',
    'portfolio_segment_checks': True, 'missing_result_status': 'exclude',
    'archive_result_contract': '', 'complementarity_research': False,
    'complementarity_pair_budget': 10000,
}


def segment_evidence(m, config, availability=None, *, portfolio=False):
    """Keep observed losses distinct from insufficient segment samples."""
    counts = m.get('segment_match_counts' if portfolio else 'segment_counts', {})
    den = int(m.get('denominator', 200))
    nets = m.get('segment_net_i')
    if nets is None:
        nets = {k: int(Decimal(str(v))*den) for k,v in m.get('segment_net', {}).items()}
    available = availability if availability is not None else m.get('segment_availability', counts)
    total = sum(available.values())
    n = int(m.get('matches', 0) if portfolio else m.get('n', 0))
    minimum = int(config.get('segment_evidence_min_matches', 5))
    rows = []
    for key in sorted(set(available)|set(counts)|set(nets)):
        a,c,net = available.get(key,0), int(counts.get(key,0)), int(nets.get(key,0))
        major = bool(total and Fraction(a,total)>=Fraction(str(config.get('major_segment_share','0.15'))))
        required = ceil(Fraction(str(config.get('min_segment_share','0.5')))*n*Fraction(a,total)) if total and major else 0
        status = ('NO_ORDERS' if not c else 'LOSING_SEGMENT' if net<0 else
                  'INSUFFICIENT_SEGMENT_SAMPLE' if c<minimum else
                  'POSITIVE_SEGMENT' if net>0 else 'BREAK_EVEN_SEGMENT')
        rows.append({'segment':key,'available_matches':a,'matches_or_orders':c,
                     'net_i':net,'net':net/den,'major':major,'required_coverage':required,
                     'coverage_ok':c>=required,'evidence_status':status,'sample_is_small':c<minimum})
    positive = sum(max(0,int(v)) for v in nets.values())
    best = max((int(v) for v in nets.values()), default=0)
    net_total = sum(int(v) for v in nets.values())
    return {'policy':config.get('historical_policy_version','legacy_v1'),'segments':rows,
            'count_basis':'distinct_matches_after_dispatch' if portfolio else 'executed_first_orders',
            'best_segment_positive_profit_share':best/positive if positive else None,
            'remove_best_segment_net':(net_total-best)/den if len(nets)>1 else None,
            'remove_best_segment_retention':(net_total-best)/net_total if net_total>0 and len(nets)>1 else None,
            'worst_segment_net':min(nets.values())/den if nets else None,
            'has_small_sample_segments':any(r['sample_is_small'] for r in rows),
            'all_segments_sufficient_and_positive':bool(rows) and all(r['net_i']>0 and not r['sample_is_small'] for r in rows),
            'guarantees_future_profit':False}


def segment_policy_reasons(evidence, config):
    rows = evidence['segments']; reasons = []
    bad = [r['segment'] for r in rows if r['major'] and r['net_i'] <= 0 or
           config.get('minor_segment_loss_policy', 'disclose') == 'block' and r['net_i'] < 0]
    if bad: reasons.append('研究段出现亏损或主要段净胜未为正：' + '、'.join(bad))
    short = [r['segment'] for r in rows if not r['coverage_ok']]
    if short: reasons.append('研究段实际执行覆盖不足：' + '、'.join(short))
    cap = Fraction(str(config.get('max_best_segment_profit_share', '1')))
    nets = [r['net_i'] for r in rows]; positive = sum(max(0, v) for v in nets)
    if cap < 1 and positive and Fraction(max(nets), positive) > cap:
        reasons.append('最好研究段盈利贡献超过冻结比例上限')
    return reasons


def portfolio_history_reasons(m, config, *, empty=False):
    from .common import historical_objective
    if empty or not historical_objective(config) or not config.get('portfolio_segment_checks', False): return []
    if not all(k in m for k in ('segment_net_i', 'segment_match_counts', 'history_days')):
        return ['缺少组合实际订单的分段或跨度证据']
    reasons = segment_policy_reasons(segment_evidence(m, config, portfolio=True), config)
    if int(m.get('matches', 0)) < int(config.get('min_matches', 0)):
        reasons.append('组合实际执行比赛数低于冻结最低场次')
    if int(m['history_days']) < 365 * int(config.get('min_history_years', 0)):
        reasons.append('组合实际执行覆盖跨度不足')
    return reasons


def portfolio_qualifies(m, config, ratio, *, empty=False):
    from .research_standard import portfolio_feasible
    return portfolio_feasible(m['net_i'], m['drawdown_match_i'], ratio, empty=empty) and not portfolio_history_reasons(m, config, empty=empty)


def portfolio_availability(events, labels, config):
    from .research_standard import direction_matches
    available = direction_matches(events, labels, config['directions'], config)
    seen = set().union(*available.values()) if available else set()
    return dict(sorted(Counter(labels[sid].get('segment', '') for sid in seen).items()))


def qualify_results(labels, config):
    """Classify uploaded evidence without claiming an external verification."""
    policy = config.get('missing_result_status', 'trial')
    contract = config.get('archive_result_contract', '').strip()
    if policy == 'archive_contract' and not contract:
        raise ValueError('文件级完场契约必须有明确来源说明，不能自动推断')
    counts = Counter()
    for label in labels.values():
        if label.get('conflict') or label.get('quality_exclusions'):
            status = 'CONFLICT_OR_QUALITY_QUARANTINE'
        elif label.get('result_status') in ('完', '完场', '已结束'):
            status = 'UPLOADED_COMPLETION_CONFIRMED'
        elif label.get('result_status'):
            status = 'NOT_CONFIRMED_COMPLETE'
        elif policy == 'archive_contract':
            status = 'DECLARED_ARCHIVE_CONTRACT'; label['archive_result_contract'] = contract
        else:
            status = 'LABEL_ONLY_TRIAL'
            if policy == 'exclude':
                label['eligible'] = False
                label['result_evidence_exclusion'] = 'missing_completion_status'
        label['result_evidence_status'] = status
        counts[status] += 1
    return {'policy': policy, 'counts': dict(counts), 'archive_result_contract': contract or None,
            'eligible_matches': sum(bool(l.get('eligible')) for l in labels.values()),
            'label_only_trial_included': sum(l.get('result_evidence_status') == 'LABEL_ONLY_TRIAL' and bool(l.get('eligible')) for l in labels.values()),
            'independent_results_verified': False, 'real_execution_verified': False}


def compare_roster_risk(main, backup):
    keys = ('net', 'drawdown_match', 'drawdown_day', 'streak', 'max_match_stake', 'worst_match_net')
    values = {k: {'main': main.get(k), 'backup': backup.get(k)} for k in keys}
    risks = {'drawdown_match':1, 'drawdown_day':1, 'streak':1, 'max_match_stake':1,'worst_match_net':-1}
    comparable = bool(main.get('n') and backup.get('n')) and all(main.get(k) is not None and backup.get(k) is not None for k in risks)
    no_worse = comparable and all(sign*backup[k] <= sign*main[k] for k,sign in risks.items())
    lower = no_worse and any(sign*backup[k] < sign*main[k] for k,sign in risks.items())
    return {'status': 'LOWER_ON_REPORTED_RISK_DIMENSIONS' if lower else
            'SAME_ON_REPORTED_RISK_DIMENSIONS' if no_worse else 'RISK_TRADEOFF_OR_INSUFFICIENT_EVIDENCE',
            'dimensions': values, 'backup_has_orders': bool(backup.get('n')),
            'stronger_ratio_threshold_alone_does_not_prove_lower_risk': True,
            'rosters_may_not_be_combined': True}
