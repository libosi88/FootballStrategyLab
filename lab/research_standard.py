"""Scale-aware research standard shared by review, legacy selection, economics and the holdout.

Absolute streak and drawdown limits favour small lucky samples, so risk is judged relative to
profit (net >= R x drawdown), the sample requirement follows the direction's available matches,
and time coverage uses research segments (end-anchored 12-month windows) instead of archive
calendar years, which split seasons.
"""
from datetime import date,timedelta
from fractions import Fraction
from math import ceil,erfc,sqrt
from .common import MISSING,money_threshold,side_for,historical_objective

SEGMENT_DAYS=365
# Segments holding less than this share of a direction's available matches are disclosed, not gated:
# a requirement of one or two bets there is noise.
SMALL_SEGMENT_SHARE=Fraction(15,100)
HOLDOUT_POLICY='latest_months_end_anchored_v1'

from functools import lru_cache
@lru_cache(maxsize=4096)
def _fraction_text(text):return Fraction(text)
# Fractions are immutable, so repeated ratio strings (once per portfolio evaluation) are parsed once.
def fraction(value):return _fraction_text(str(value))

def decision_metrics(config,score):
    return [score.get('historical',score['raw'])] if historical_objective(config) else [score['raw'],*score['stress']]

# ---- research segments ---------------------------------------------------------------------------
def segment_anchor(labels):
    dates=[l['date'] for l in labels.values() if l.get('eligible') and l.get('date')]
    return max(dates) if dates else None

def segment_key(day,anchor):
    """(anchor-365*(k+1), anchor-365*k] is segment k, named by its first and last day."""
    k=(date.fromisoformat(anchor)-date.fromisoformat(day)).days//SEGMENT_DAYS
    last=date.fromisoformat(anchor)-timedelta(days=SEGMENT_DAYS*k);first=last-timedelta(days=SEGMENT_DAYS-1)
    return f'{first.isoformat()}~{last.isoformat()}'

def assign_segments(labels,anchor=None,config=None):
    config=config or {};basis=config.get('segment_basis','end_anchored_365')
    anchor=config.get('segment_anchor_date') or anchor or segment_anchor(labels)
    for label in labels.values():
        day=label.get('date')
        label['segment']=(day[:4]+'-01-01~'+day[:4]+'-12-31' if basis=='calendar_year' else segment_key(day,anchor)) if anchor and day else ''
        label['segment_basis']=basis
    return anchor

# ---- trigger window and direction availability ----------------------------------------------------
def trigger_window_allows(event,policy):
    """With prematch_trigger_status='即', prematch rules trigger only on the source's same-day 即 market
    (measured 0-11 hours before kickoff); early-market 早 quotes still build feature history."""
    return event['phase']==1 or (policy or {}).get('prematch_trigger_status','any')!='即' or event.get('status')=='即'

def scope_of(direction):
    return (0 if direction.startswith('PRE') else 1,0 if direction.endswith(('OVER','UNDER')) else 1)

def direction_matches(events,labels,directions,policy):
    """Per direction: eligible matches with at least one evaluable trigger quote."""
    lookup={}
    for d in directions:lookup.setdefault(scope_of(d),[]).append(d)
    seen={d:set() for d in directions}
    for e in events:
        wanted=lookup.get((e['phase'],e['market']))
        if not wanted or not e['valid']:continue
        label=labels.get(e['sid']) or {}
        if not label.get('eligible') or e['phase'] and e['market'] and e['score'][0]==MISSING or not trigger_window_allows(e,policy):continue
        for d in wanted:
            if side_for(d,e['line']) is not None:seen[d].add(e['sid'])
    return seen

def direction_availability(events,labels,directions,policy):
    """Per direction: eligible matches per research segment with at least one evaluable trigger quote."""
    out={}
    for d,matches in direction_matches(events,labels,directions,policy).items():
        counts={}
        for sid in matches:
            segment=(labels.get(sid) or {}).get('segment','');counts[segment]=counts.get(segment,0)+1
        out[d]=dict(sorted(counts.items()))
    return out

# ---- single-rule gates -----------------------------------------------------------------------------
def required_matches(config,available):
    return max(int(config['min_matches']),min(int(config['min_matches_ceiling']),ceil(fraction(config['min_matches_share'])*available)))

def segment_shortfalls(m,config,availability):
    total=sum(availability.values());counts=m.get('segment_counts') or {};share=fraction(config['min_segment_share']);short=[]
    for segment,available in availability.items():
        if not total or Fraction(available,total)<fraction(config.get('major_segment_share','0.15')):continue
        if counts.get(segment,0)<ceil(share*m['n']*Fraction(available,total)):short.append(segment)
    return short

def return_drawdown_ok(m,ratio):
    return m['n']>0 and m['net_i']>0 and Fraction(m['net_i'])>=fraction(ratio)*m['drawdown_i']

def research_gate_reasons(base,m,stress,config,scale,availability,rejection=None):
    """Reasons and tags for one rule on one price basis. stress holds the three stress scenarios;
    rejection is the pre-goal rejection scenario when the standard policy applies it."""
    reasons=[];tags=[];den=2*scale;historical=historical_objective(config)
    priced=[m] if historical else [m,*stress,*([rejection] if rejection is not None else [])]
    if any(x.get('outcome_counts',{}).get('unclassified',0) for x in priced):reasons.append('存在无法归入合法五类的结算，需核查逐笔');tags.append('SETTLEMENT_CLASSIFICATION_FAILED')
    available=sum(availability.values());need=required_matches(config,available);short=segment_shortfalls(m,config,availability)
    if m['n']<need or short:
        reasons.append(f'场次或研究段覆盖不足（需≥{need}场，方向可用{available}场'+('；覆盖不足段：'+'、'.join(short) if short else '')+'）');tags.append('LOW_SAMPLE_OBSERVATION')
    if historical:
        days=int(m.get('history_days',0));required_days=365*config.get('min_history_years',2)
        if days<required_days:reasons.append(f'历史覆盖不足（{days}天，需至少{required_days}天）');tags.append('SHORT_HISTORY')
        from .history_policy import segment_evidence,segment_policy_reasons
        evidence=segment_evidence(m,config,availability);m['segment_evidence']=evidence
        segment_reasons=segment_policy_reasons(evidence,config)
        if segment_reasons:reasons.extend(segment_reasons);tags.append('UNSTABLE_HISTORICAL_SEGMENTS')
    if not all(return_drawdown_ok(x,config['min_return_drawdown_ratio']) for x in priced):
        reasons.append(('历史收益回撤比' if historical else '收益回撤比（含压力与拒单情景）')+f"低于{config['min_return_drawdown_ratio']}");tags.append('HIGH_RISK_ALTERNATIVE')
    pnl=[t['pnl'] for t in base];wins=sorted(p for p in pnl if p>0);by_segment={}
    for t in base:by_segment[t.get('segment','')]=by_segment.get(t.get('segment',''),0)+t['pnl']
    # Removing the best segment is only meaningful when the direction has data in at least two segments.
    if sum(pnl)-sum(wins[-5:])<=0 or len(availability)>=2 and sum(pnl)-max(by_segment.values(),default=0)<=0:
        reasons.append('盈利集中度未通过（去掉最盈利5笔或最好研究段后不为正）');tags.append('CONCENTRATION_FAILED')
    if not historical and any(x['net_i']<=money_threshold(config['stress_min_profit'],den) for x in stress):reasons.append('压力情景净胜未过门槛');tags.append('STRESS_FAILED')
    if not historical and rejection is not None and rejection['net_i']<=money_threshold(config['min_profit'],den):reasons.append('进球前报价拒单情景净胜未过门槛');tags.append('PREGOAL_REJECTION_FAILED')
    if any(t['quality'] for t in base):reasons.append('存在已知比分质量旗标');tags.append('QUALITY_BLOCKED')
    return reasons,tags

# ---- portfolio gate and disclosure statistics ----------------------------------------------------------
def portfolio_feasible(min_net_i,max_drawdown_i,ratio,empty=False):
    """The empty roster is always feasible; otherwise the worst priced net must be positive and at least
    ratio x the worst match-ordered drawdown, so the budget grows with the roster instead of capping it."""
    return empty or min_net_i>0 and Fraction(min_net_i)>=fraction(ratio)*max_drawdown_i

def evidence_statistics(values):
    """One-sided normal approximation of mean per-order P&L against zero. Disclosure only: it does not
    correct for the number of rules searched unless combined with an explicit test count."""
    n=len(values)
    if n<2:return {'pnl_sd':None,'z':None,'p_one_sided':None}
    mean=sum(values)/n;sd=sqrt(sum((v-mean)**2 for v in values)/(n-1))
    if not sd>0:return {'pnl_sd':0.0,'z':None,'p_one_sided':None}
    z=mean/(sd/sqrt(n))
    return {'pnl_sd':sd,'z':z,'p_one_sided':0.5*erfc(z/sqrt(2))}

def bonferroni(p,tests):
    return None if p is None else min(1.0,p*max(1,int(tests or 1)))

# ---- holdout -----------------------------------------------------------------------------------------
def holdout_plan(labels,config):
    if historical_objective(config):return {'status':'FULL_HISTORY','policy':'all_selected_history_v1','discovery_matches':sum(bool(l.get('eligible')) for l in labels.values()),'holdout_matches':0}
    months=int(config.get('holdout_months',0))
    if config.get('research_partition'):return {'status':'DISABLED_FOR_PARTITIONED_TRAINING','policy':HOLDOUT_POLICY}
    if not months:return {'status':'DISABLED','policy':HOLDOUT_POLICY}
    eligible=sorted(l['date'] for l in labels.values() if l.get('eligible') and l.get('date'))
    if not eligible:return {'status':'UNAVAILABLE_NO_ELIGIBLE_MATCHES','policy':HOLDOUT_POLICY,'months':months}
    anchor=eligible[-1];cutoff=(date.fromisoformat(anchor)-timedelta(days=round(months*SEGMENT_DAYS/12))).isoformat()
    held=sum(d>cutoff for d in eligible);total=len(eligible)
    plan={'policy':HOLDOUT_POLICY,'months':months,'anchor':anchor,'cutoff':cutoff,'discovery_matches':total-held,'holdout_matches':held,'max_share':str(config['holdout_max_share'])}
    if not held:plan['status']='UNAVAILABLE_NO_RECENT_MATCHES'
    elif Fraction(held,total)>fraction(config['holdout_max_share']):plan['status']='UNAVAILABLE_SHORT_HISTORY'
    else:plan['status']='SPLIT'
    return plan

def holdout_verdict(packet,report,config):
    """Pre-registered roster-level test: enough orders, positive minute-close net, and positive net
    after pre-goal rejection when that scenario exists. Stress scenarios are disclosed."""
    if not packet.get('rules'):return 'EMPTY_ROSTER'
    scenarios=report['scenarios']
    if scenarios[0]['n']<int(config['holdout_min_orders']):return 'INSUFFICIENT_SAMPLE'
    return 'CONFIRMED' if all(scenarios[i]['net_i']>0 for i in ([0,4] if len(scenarios)>4 else [0])) else 'FAILED'
