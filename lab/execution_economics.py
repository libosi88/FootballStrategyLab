"""Independent economic qualification gate; mechanical replay PASS is insufficient."""
from .common import money_threshold,scaled,historical_objective

STANDARD_SCOPES=('FSL_STANDARD_V3_1','FSL_STANDARD_V3_2')

def verify_economics(packet,signals_by_rule,quotes,labels,config,stream_report):
    if not packet['rules']:return {'status':'PASS','scope':'EMPTY_ROSTER','rules':[],'qualification_claimed':False}
    if packet['execution_policy'].get('quote_mapping')!='minute_close_latest_v3':
        return {'status':'NOT_VERIFIED','reason':'旧名单没有以实际分钟末取价参与筛选，不能授予新标准经济资格'}
    from .selection import metrics,dispatch,portfolio_metrics,PREGOAL_REJECTION_POLICY,priced_scenario_count
    policy=packet['execution_policy']
    rejection_applies=policy.get('pregoal_rejection')==PREGOAL_REJECTION_POLICY
    if packet.get('scope') in STANDARD_SCOPES and not rejection_applies:
        return {'status':'FAIL','reason':'标准冻结包缺少进球前拒单政策，筛选与交接口径不能对齐','rules':[],'qualification_claimed':False}
    from .standard_review import pregoal_disclosure
    from .research_standard import direction_availability,research_gate_reasons,portfolio_feasible
    scale=packet['water_scale'];den=2*scale;rows=[];rejection_roster=[]
    all_years=sorted({l['year'] for l in labels.values() if l['eligible']})
    # The same availability, research segments and trigger window as the review decide sample gates.
    availability=direction_availability(quotes.events,labels,sorted({r['direction'] for r in packet['rules'] if r.get('direction')}),policy)
    needed=priced_scenario_count(policy)
    for rule in packet['rules']:
        signals=signals_by_rule[rule['id']];major=[]
        for scenario in range(4):
            major.append([t for s in signals if (t:=quotes.trade(s['eid'],s['side'],max(0,scenario-1),0 if scenario==0 else scaled('0.05',scale))) is not None])
        kept=[t for s in signals if (t:=quotes.trade(s['eid'],s['side'],0,0,reject_pregoal=True)) is not None]
        available=availability.get(rule.get('direction'))
        if available is None:
            # A rule without a declared direction is judged on its own executed matches per research segment.
            matches={}
            for t in major[0]:matches.setdefault(t.get('segment',''),set()).add(t['sid'])
            available={segment:len(sids) for segment,sids in sorted(matches.items())}
        segments=list(available)
        mm=[metrics(t,scale,all_years,segments) for t in major]
        pregoal=pregoal_disclosure(major[0],kept,scale,all_years,segments)
        historical=historical_objective(config) and rejection_applies
        # Historical facts exclude fills the pre-goal rule rejects; validation keeps s0 plus every priced scenario.
        base,head=(kept,metrics(kept,scale,all_years,segments)) if historical else (major[0],mm[0])
        reasons,_=research_gate_reasons(base,head,mm[1:],config,scale,available,pregoal['metrics'] if rejection_applies else None)
        if head['net_i']<=money_threshold(config['min_profit'],den):reasons.append('剔除进球前拒单后的历史净胜不过门槛' if historical else '实际执行原价净胜不过门槛')
        # The review blocks validation-mode rules whose stress or rejection fills carry a known score-quality flag.
        # This extra gate follows the research objective itself, not whether the packet carries the rejection policy.
        if not historical_objective(config) and any(t['quality'] for trades in (*major[1:],kept) for t in trades):reasons.append('压力或拒单情景报价存在已知比分质量旗标')
        rows.append({'strategy_id':rule['id'],'status':'FAIL' if reasons else 'PASS','reasons':reasons,'execution_metrics':mm,'pregoal_rejection':pregoal,'direction_available_matches':available})
        rejection_roster.append({**rule,'_trades':[kept]})
    ratio=packet.get('portfolio_research_return_drawdown_ratio',config['portfolio_min_return_drawdown_ratio'])
    scenarios=stream_report.get('scenarios',{})
    priced=[scenarios.get(str(i),{}).get('metrics') for i in range(needed)]
    rejection=None
    if rejection_applies:
        orders,_=dispatch(rejection_roster,0,policy.get('match_cap',config['match_cap']),priority_mode=policy.get('priority','strategy_id_lexical'))
        rejection=portfolio_metrics(orders,scale,all_years)
        from .history_policy import portfolio_availability
        rejection['segment_availability']=portfolio_availability(quotes.events,labels,config)
        locked=scenarios.get('4',{}).get('metrics')
        if locked and (locked.get('n')!=rejection['n'] or locked.get('net_i')!=rejection['net_i']):
            return {'status':'FAIL','reason':'交接s4组合与经济门禁复算不一致','rules':rows,'portfolio_within_frozen_risk':False,
                    'pregoal_rejection_portfolio':rejection,'locked_pregoal_rejection_portfolio':locked,
                    'risk_return_drawdown_ratio':str(ratio),'scope':'all actual minute-close scenarios plus the pre-goal rejection scenario; uploaded labels only, not future/live approval'}
        if not priced[-1]:priced[-1]=rejection
    stream_complete=all(str(i) in scenarios for i in range(4))
    priced_complete=all(priced) and (not rejection_applies or '4' in scenarios or rejection is not None)
    # Same criterion as the portfolio search: worst priced net against the worst match-ordered drawdown.
    decision=([rejection] if rejection is not None else priced[:1]) if historical_objective(config) else priced
    portfolio_ok=stream_complete and priced_complete and len(priced)==needed and portfolio_feasible(min(m['net_i'] for m in decision),max(m['drawdown_match_i'] for m in decision),ratio)
    from .history_policy import portfolio_history_reasons,segment_evidence
    segment_reasons=portfolio_history_reasons(decision[0],config)
    portfolio_ok=portfolio_ok and not segment_reasons
    if rejection_applies and '4' not in scenarios:
        portfolio_ok=False
    return {'status':'PASS' if portfolio_ok and all(r['status']=='PASS' for r in rows) else 'FAIL',
            'quote_mapping':'minute_close_latest_v3','rules':rows,'portfolio_within_frozen_risk':portfolio_ok,
            'pregoal_rejection_portfolio':rejection,'priced_scenario_count':needed,
            'portfolio_segment_verification':{'status':'FAIL' if segment_reasons else 'PASS','reasons':segment_reasons,
                'evidence':segment_evidence(decision[0],config,portfolio=True) if historical_objective(config) else None},
            'risk_return_drawdown_ratio':str(ratio),'scope':'all actual minute-close scenarios plus the pre-goal rejection scenario; uploaded labels only, not future/live approval'}
