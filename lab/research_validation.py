"""Frozen-roster uncertainty and held-out evaluation without rediscovery leakage."""
from collections import defaultdict
from copy import deepcopy
import numpy as np
from .common import digest,MISSING,timestamp
from .contracts import bind_rule
from .rules import unit
from .features import SignalEngine

def block_bootstrap(orders_by_scenario,labels,scale,repetitions=200,block_days=7,seed=1729,should_pause=None):
    def day_of(v):
        t=v.get('kickoff',MISSING)
        if t==MISSING:t=timestamp(v['date']+' 12:00')
        return MISSING if t==MISSING else t//1440
    days=sorted({day_of(v) for v in labels.values() if v['eligible']})
    if MISSING in days:return {'status':'BLOCKED','reason':'有比赛缺少可解释日期，不能构造共同比赛日块','selection_bias_adjusted':False}
    if not days or not any(orders_by_scenario):return {'status':'NOT_APPLICABLE','reason':'没有可重采样的执行订单','selection_bias_adjusted':False}
    index={d:i for i,d in enumerate(days)};pnl=np.zeros((len(orders_by_scenario),len(days)),np.float64);stake=np.zeros_like(pnl)
    for scenario,orders in enumerate(orders_by_scenario):
        for t in orders:
            label=labels[t['sid']];at=index[day_of(label)]
            pnl[scenario,at]+=t['pnl']/(2*scale);stake[scenario,at]+=t.get('stake',1)
    if repetitions<=0:return {'status':'NOT_RUN','reason':'重采样次数为0','selection_bias_adjusted':False}
    rng=np.random.default_rng(seed);n=len(days);width=min(max(1,int(block_days)),n);nets=[];drawdowns=[];rois=[]
    for _ in range(repetitions):
        if _%32==0 and should_pause and should_pause():
            from .mining import Paused
            raise Paused()
        starts=rng.integers(0,n,size=(n+width-1)//width)
        selected=np.concatenate([(np.arange(width)+start)%n for start in starts])[:n]
        sample=pnl[:,selected];totals=sample.sum(axis=1);stakes=stake[:,selected].sum(axis=1);curve=np.c_[np.zeros(len(sample)),np.cumsum(sample,axis=1)]
        nets.append(totals);drawdowns.append(np.max(np.maximum.accumulate(curve,axis=1)-curve,axis=1));rois.append(np.divide(totals,stakes,out=np.zeros_like(totals),where=stakes>0))
    nets=np.asarray(nets);drawdowns=np.asarray(drawdowns);rois=np.asarray(rois)
    return {'status':'PASS','method':'shared_moving_blocks_of_research_match_days','repetitions':repetitions,'block_match_days':width,'seed':seed,'research_match_days':n,
            'scenarios':[{'scenario':i,'net_interval_95':np.quantile(nets[:,i],[.025,.975]).tolist(),'roi_interval_95':np.quantile(rois[:,i],[.025,.975]).tolist(),
                          'drawdown_interval_95':np.quantile(drawdowns[:,i],[.025,.975]).tolist(),'positive_fraction':float(np.mean(nets[:,i]>0)),
                          'degenerate':bool(np.std(pnl[i])==0)} for i in range(len(pnl))],
            'selection_bias_adjusted':False,'scope':'Conditional on this already-selected roster and current corrected historical data. Common blocks preserve joint losses; intervals are not independent future evidence and do not remove the effect of searching many rules/leagues.'}

def adapt_packet_scale(packet,target_scale):
    old=packet['water_scale']
    if target_scale==old:return deepcopy(packet)
    if target_scale<old or target_scale%old:raise ValueError('只允许无损扩大事件水位单位')
    ratio=target_scale//old;result=deepcopy(packet);contract={**result['contract'],'water_scale':target_scale};rules=[]
    def rescale_metrics(value):
        if isinstance(value,dict):
            return {k:v*ratio if k in ('net_i','drawdown_i','drawdown_match_i','drawdown_day_i','denominator') and type(v) is int else rescale_metrics(v) for k,v in value.items()}
        if isinstance(value,list):return [rescale_metrics(v) for v in value]
        return value
    for original in result['rules']:
        r=rescale_metrics(deepcopy(original))
        for a in r['conditions']:
            if unit(a['feature'],old)==old:
                a['value']*=ratio
                if 'upper' in a:a['upper']*=ratio
            if 'min_water_step' in a:a['min_water_step']*=ratio
            from .rules import label
            a['label']=label(a,target_scale)
        for name in ('contract','content_hash','water_scale'):r.pop(name,None)
        rules.append(bind_rule(r,contract))
    result.update(rules=rules,contract=contract,water_scale=target_scale)
    result['unit_adaptation']={'source_packet_sha256':digest(packet),'source_scale':old,'target_scale':target_scale,'ratio':ratio,'research_reselection':False}
    result['roster_hash']=digest({'rules':rules,'contract':contract,'execution_policy':result['execution_policy']});return result

def evaluate_frozen(packet,events,labels,scale,config):
    from .selection import Quotes,dispatch,portfolio_metrics
    from .common import scaled
    policy=packet['execution_policy']
    for key in ('stale_minutes','match_cap'):
        if key not in policy:raise ValueError('冻结包缺少执行政策: '+key)
        if key in config and config[key]!=policy[key]:raise ValueError('评测配置与冻结包执行政策冲突: '+key)
    packet=adapt_packet_scale(packet,scale);engine=SignalEngine(packet['rules'],contract=packet['contract'],execution_policy=policy)
    for sid in {e['sid'] for e in events}:engine.mark_history_complete(sid)
    signals=[];previous=None
    for e in events:
        if previous is not None and e['sid']!=previous:engine.close_match(previous)
        previous=e['sid'];signals.extend(s for s in engine.feed(e) if labels[e['sid']]['eligible'])
    if previous is not None:engine.close_match(previous)
    quotes=Quotes(events,labels,scale,policy['stale_minutes'],minute_close=policy.get('quote_mapping')=='minute_close_latest_v3');by=defaultdict(list)
    for s in signals:by[s['strategy_id']].append(s)
    years=sorted({l['year'] for l in labels.values() if l['eligible']});reports=[]
    from .selection import PREGOAL_REJECTION_POLICY,SCENARIO_NAMES,metrics
    rejection=policy.get('pregoal_rejection')==PREGOAL_REJECTION_POLICY;rule_reports=[]
    for scenario in range(5 if rejection else 4):
        roster=[]
        for r in packet['rules']:
            if scenario==4:trades=[t for s in by[r['id']] if (t:=quotes.trade(s['eid'],s['side'],0,0,reject_pregoal=True)) is not None]
            else:trades=[t for s in by[r['id']] if (t:=quotes.trade(s['eid'],s['side'],max(0,scenario-1),0 if scenario==0 else scaled('0.05',scale))) is not None]
            roster.append({**r,'_trades':[trades]})
            if scenario==0:
                # Per-rule disclosure on the frozen price basis, before the shared match cap; never a reselection input.
                m=metrics(trades,scale,years);rule_reports.append({'strategy_id':r['id'],'direction':r['direction'],**{k:m[k] for k in ('n','net','roi','drawdown','z','p_one_sided')}})
        orders,_=dispatch(roster,0,policy['match_cap'],priority_mode=packet['execution_policy']['priority']);reports.append(portfolio_metrics(orders,scale,years))
    return {'status':'PASS','quote_mapping':policy.get('quote_mapping','legacy_instant_research'),'frozen_packet_sha256':digest(packet),'signals':len(signals),'rules':len(packet['rules']),'scenarios':reports,'scenario_names':list(SCENARIO_NAMES[:len(reports)]),'rules_s0':rule_reports,'research_reselection':False,'interpretation':'Evaluation completed, not a declaration of profitable or live-approved strategies.'}
