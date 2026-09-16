"""Explicit coverage accounting and per-league research completion gates."""
from pathlib import Path
from contextlib import closing
import sqlite3
from .common import *


def module_coverage(jobdir,coverage,engine=None,trigger=None):
    root=Path(jobdir);modules={f'M0{i}':{'planned':0,'representatives':0,'equivalent_occurrences':0,'proven_zero':0,'proven_nonprofitable':0,'candidates':0} for i in range(8)}
    gaps={}
    threshold=money_threshold(read_json(root/'config.json')['min_profit'],2*coverage['units']['water'])
    for direction,scope in coverage['directions'].items():
        folder=root/'mining'/direction
        if scope['status']=='NO_DATA':
            modules['M00']['planned']+=1
            from .search_integrity import verify_search_evidence
            try:verify_search_evidence(folder,'standard_empty_scope')
            except (ValueError,KeyError):gaps[direction]='MISSING_OR_CHANGED_EMPTY_SCOPE_EVIDENCE';continue
            modules['M00']['proven_zero']+=1;continue
        spec=read_json(folder/'search_spec.json');plan=read_json(folder/'standard_plan.json')
        if not spec:gaps[direction]='MISSING_SEARCH_SPEC';continue
        for name,total in spec['modules'].items():modules[name]['planned']+=total
        backend=scope.get('search_backend','standard_class_dfs')
        if scope.get('status')=='COMPLETE':
            from .search_integrity import verify_search_evidence
            try:verify_search_evidence(folder,backend,spec.get('binding'))
            except (ValueError,KeyError):gaps[direction]='MISSING_OR_CHANGED_COMPLETED_SEARCH_EVIDENCE';continue
        elif scope.get('status') in ('BUDGET_STOP','PAUSED','RESOURCE_BLOCKED','INTERRUPTED') and backend=='standard_class_dfs':
            from .search_integrity import verify_search_evidence
            try:verify_search_evidence(folder,'standard_class_dfs_partial',spec.get('binding'))
            except (ValueError,KeyError):gaps[direction]='MISSING_OR_CHANGED_PARTIAL_SEARCH_EVIDENCE';continue
        else:gaps[direction]='UNSEALED_SEARCH_STATE';continue
        if backend=='standard_global_bound':
            for name,total in spec['modules'].items():modules[name]['proven_nonprofitable']+=total-(1 if name=='M00' else 0)
            modules['M00']['representatives']+=1
            continue
        if not plan:gaps[direction]='MISSING_SEARCH_PLAN';continue
        db=folder/'search.sqlite3'
        if not db.is_file():gaps[direction]='MISSING_SEARCH_LEDGER';continue
        with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger'").fetchone():gaps[direction]='MISSING_LEDGER_TABLE';continue
            for block,kind,weight,net in conn.execute('SELECT block,kind,weight,net FROM ledger'):
                row=modules[plan['blocks'][block]['module']];weight=int(weight)
                if kind=='EVALUATED':
                    row['representatives']+=1;row['equivalent_occurrences']+=weight-1
                    if net>threshold:row['candidates']+=weight
                elif kind=='PROVEN_ZERO':row['proven_zero']+=weight
                elif kind=='PROVEN_NONPROFITABLE_BOUND':row['proven_nonprofitable']+=weight
                else:raise ValueError('未知覆盖账本类型')
    for row in modules.values():
        row['remaining']=row['planned']-sum(row[k] for k in ('representatives','equivalent_occurrences','proven_zero','proven_nonprofitable'))
        if row['remaining']<0:raise ValueError('模块覆盖发生数为负')
        row['status']='PASS' if row['remaining']==0 else 'PARTIAL'
    grammar=read_json(root/'config.json').get('search_grammar','compact_v1')
    excluded=('M03','M04','M05') if grammar=='compact_v1' else ('M04',) if grammar=='balanced_v1' else ()
    for name in excluded:
        if modules[name]['planned']:raise ValueError('规格声明不包含的模块出现搜索计数')
        modules[name].update(status='NOT_APPLICABLE',reason='本次冻结语法不包含，不能作为已经搜索的证明')
    modules['M08']={'status':'PASS' if engine and engine['status']=='PASS' and trigger and trigger['status'] in ('PASS','EMPTY_ROSTER') else 'PENDING',
        'engine_validation':engine,'trigger_validation':trigger,'count_meaning':'dictionary-normalized atom module expression occurrences; no claim of complete logical equivalence proof',
        'extension_scope':'E01-E04 not included'}
    blocked={d:s.get('blocked') for d,s in coverage['directions'].items() if s.get('blocked')}
    result={'modules':modules,'blocked_directions':blocked,'search_grammar':grammar,'excluded_modules':list(excluded),
        'evidence_gaps':gaps,'evidence_complete':not gaps,
        'declared_standard_search_complete':bool(coverage.get('standard_search_complete',False)) and not gaps and not blocked and all(row['remaining']==0 for name,row in modules.items() if name!='M08')}
    atomic_json(root/'coverage_modules.json',result);return result


HOLDOUT_FAILURE_TEXT={'FAILED':'留出的最近12个月样本外净胜不为正（含进球前拒单情景）','INSUFFICIENT_SAMPLE':'样本外订单数不足，无法确认',
    'UNAVAILABLE_SHORT_HISTORY':'数据太短，留不出不超过一半比赛的最近12个月样本外','UNAVAILABLE_NO_RECENT_MATCHES':'最近12个月没有合格比赛可作样本外',
    'EMPTY_ROSTER':'主名单为空或未通过非空验收','DISABLED':'样本外留出已关闭','NOT_RECORDED':'本任务没有样本外留出记录','NOT_RUN':'未进行样本外评测'}

def coverage_ratio(coverage):
    planned=int(coverage.get('planned_expressions') or 0);remaining=int(coverage.get('remaining') or 0)
    return (planned-remaining)/planned if planned else None

def completion_state(coverage,summary,trigger,engine,packaging=False,config=None,holdout=None,packaging_skipped=False):
    gates={'full_standard_search':bool(coverage.get('standard_search_complete')) and coverage.get('remaining')==0,
        'engine_tests':bool(engine and engine.get('status')=='PASS'),
        'all_pool_review':bool(summary.get('standard_review',{}).get('complete')),
        'selection':summary.get('selection_status')=='FINITE_LOCAL_SEARCH_COMPLETE',
        'trigger':trigger.get('status') in ('PASS','EMPTY_ROSTER'),
        'nonempty_roster':trigger.get('status')=='PASS' and trigger.get('rules',0)>0,
        'rule_cases':trigger.get('rule_cases',{}).get('status')=='PASS',
        'persistent_paper':all(trigger.get('persistent_paper_execution',{}).get(str(i),{}).get('status')=='PASS' for i in range(4)) and trigger.get('persistent_paper_execution',{}).get('4',{'status':'PASS'}).get('status')=='PASS',
        'streaming_paper':trigger.get('streaming_paper_execution',{}).get('status')=='PASS' and (trigger.get('persistent_paper_execution',{}).get('4',{}).get('status')!='PASS' or trigger.get('streaming_paper_execution',{}).get('scenarios',{}).get('4',{}).get('status')=='PASS'),
        'execution_economics':trigger.get('execution_economics',{}).get('status')=='PASS',
        'packaging':bool(packaging)}
    if config is not None:
        gates['standard_research_thresholds']=config.get('profile')!='standard' or standard_thresholds_match(config)
        if int(config.get('holdout_months',0)) and not config.get('research_partition'):
            # The evaluation must have been decided (confirmed, failed or unavailable); its verdict drives the recommendation.
            gates['holdout_evaluation']=bool(holdout) and holdout.get('status') not in ('NOT_RUN','NOT_RECORDED')
    if packaging_skipped:gates.pop('packaging')
    if config is not None and historical_objective(config):
        complete=all(value for name,value in gates.items() if name!='nonempty_roster')
        state=('HISTORICAL_RESEARCH_COMPLETE_NO_HANDOFF' if packaging_skipped else 'HISTORICAL_RESEARCH_COMPLETE') if complete else 'PARTIAL_RESULT'
        found=gates['nonempty_roster']
        result='HISTORICAL_SELECTION_READY' if complete and found else 'HISTORICAL_SEARCH_EMPTY' if complete else 'PARTIAL_HISTORICAL_RESULT'
        missing=[name for name,value in gates.items() if not value and name!='nonempty_roster']
        reasons=['尚未完成：'+','.join(missing)] if missing else []
        if complete and not found:reasons.append('规定范围的历史搜索与筛选已完成，没有满足本次历史稳定性条件的策略')
        return {'version':VERSION,'state':state,'v3_status':state,'research_objective':'historical','gates':gates,
                'empty_roster_chain_verified':complete and not found,'recommendation':{'status':result,'reasons':reasons,'live_approval':False},
                'research_only':True,'historical_holdout':'NOT_REQUIRED','holdout':holdout,'independent_future_validation':'NOT_REQUIRED',
                'historical_rolling_validation':'OPTIONAL','account_execution':'NOT_DEPLOYED','live_enabled':False,'second_slot_enabled':False,
                'selection_status':summary['selection_status'],'verification_status':trigger['status'],'packaging_skipped':bool(packaging_skipped),
                'coverage_ratio':coverage_ratio(coverage),'coverage':{k:coverage.get(k) for k in ('planned_expressions','evaluated','equivalent_occurrences','proven_zero','proven_nonprofitable','remaining','local_search_complete','standard_search_complete')}}
    standard=all(gates.values()) and not packaging_skipped
    finite=coverage.get('local_search_complete') and gates['selection'] and gates['trigger']
    state=('STANDARD_HANDOFF_COMPLETE' if standard else 'RESEARCH_COMPLETE_NO_HANDOFF' if packaging_skipped and all(gates.values())
           else 'FINITE_WORKFLOW_DONE' if finite and coverage.get('local_profile')!='standard' and (packaging or packaging_skipped) else 'PARTIAL_RESULT')
    ratio=coverage_ratio(coverage);verdict=((holdout or {}).get('main') or {}).get('verdict');reasons=[]
    # Only a completed search, a non-empty verified roster and a confirmed holdout earn a paper-trading recommendation.
    if not gates['full_standard_search']:reasons.append('搜索未完成（冻结语法覆盖 '+(f'{ratio:.4%}' if ratio is not None else '未知')+'），结果只能观察')
    if not gates['nonempty_roster']:reasons.append('主名单为空或未通过非空验收')
    if verdict!='CONFIRMED':reasons.append(HOLDOUT_FAILURE_TEXT.get(verdict or (holdout or {}).get('status') or 'NOT_RUN','样本外评测未确认'))
    other=[name for name,value in gates.items() if not value and name not in ('full_standard_search','nonempty_roster')]
    if other:
        from .reporting import GATE_NAMES
        reasons.append('未通过门禁：'+'、'.join(GATE_NAMES.get(name,name) for name in other))
    return {'version':VERSION,'state':state,'v3_status':'STANDARD_HANDOFF_COMPLETE' if standard else 'PARTIAL_CORE','gates':gates,
        'empty_roster_chain_verified':trigger.get('status')=='EMPTY_ROSTER' and all(value for name,value in gates.items() if name!='nonempty_roster'),
        'recommendation':{'status':'RECOMMENDED_FOR_PAPER_TRADING' if not reasons else 'OBSERVATION_ONLY','reasons':list(dict.fromkeys(reasons)),'live_approval':False},
        'research_only':True,'historical_holdout':verdict or (holdout or {}).get('status','NOT_RUN'),'holdout':holdout,'independent_future_validation':'NOT_RUN','historical_rolling_validation':'NOT_RUN','account_execution':'NOT_DEPLOYED',
        'live_enabled':False,'second_slot_enabled':False,'selection_status':summary['selection_status'],'verification_status':trigger['status'],'packaging_skipped':bool(packaging_skipped),
        'coverage_ratio':ratio,'coverage':{k:coverage.get(k) for k in ('planned_expressions','evaluated','equivalent_occurrences','proven_zero','proven_nonprofitable','remaining','local_search_complete','standard_search_complete')}}


def update_league_registry(store):
    from .store import resumability
    entries=[];current_engine=source_fingerprint()
    for job in store.jobs(include_manifest=True):
        root=store.root/job['id'];summary=read_json(root/'summary.json',{});audit=read_json(root/'data_audit.json',{})
        inventory=next((r for r in job['manifest'].get('leagues',[]) if (r['league'],r['company'])==(job['league'],job['config']['company'])),{})
        labels=read_json(root/'labels.json',{});dates=sorted(l['date'] for l in labels.values() if l.get('date'))
        period={k:inventory.get(k) for k in ('date_min','date_max','years','matches')}
        if dates:period.update(date_min=dates[0],date_max=dates[-1],years=sorted({l['year'] for l in labels.values() if l.get('year')}))
        period.update(audit.get('counts',{}));period['water_scale']=audit.get('water_scale')
        period['complete_calendar_years_claimed']=False
        counts={k:summary.get('coverage',{}).get(k) for k in ('planned_expressions','evaluated','equivalent_occurrences','proven_zero','proven_nonprofitable','remaining','candidates','standard_search_complete')}
        counts.update({k:summary.get('standard_review',{}).get(k) for k in ('direct_normalized_rules','qualified_signature_groups')})
        entries.append({**resumability(job,current_engine),'job':job['id'],'league':job['league'],'company':job['config']['company'],'profile':job['config']['profile'],
            'created':job['created'],'process_status':job['status'],'engine_hash':job['config'].get('engine_hash'),'input_hashes':[r['sha256'] for r in job['manifest']['files']],
            'data_scope':period,'coverage':counts,'selected':summary.get('selected'),'backup_selected':summary.get('backup_selected'),
            'workflow':read_json(root/'workflow_status.json',{'state':'NOT_COMPLETED'}),'packages':summary.get('packages',{}),
            'roster_hash':read_json(root/'results/rules.json',{}).get('roster_hash'),'path':job['id'],'live_enabled':False})
    result={'schema':'FSL_LEAGUE_REGISTRY_V1','version':VERSION,'current_engine_hash':current_engine,'updated_at':time.time(),'experiments':entries,'cross_league_profits_combined':False,'account_risk_layer':'NOT_DEPLOYED'}
    atomic_json(store.root/'league_registry.json',result);return result
