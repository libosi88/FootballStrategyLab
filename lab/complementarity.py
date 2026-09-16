"""Optional resumable pair research, isolated from the primary execution roster."""
from pathlib import Path
import json
import sqlite3
from .common import atomic_json,canonical,digest,source_fingerprint
from .history_policy import portfolio_availability,portfolio_qualifies,segment_evidence
from .mining import Paused


def study_pairs(root,population,events,labels,config,scale,update=None,should_pause=None):
    from .selection import dispatch,portfolio_metrics
    from .search_plan import unrank_combination
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    update=update or (lambda **kw:None);should_pause=should_pause or (lambda:False)
    pool=[];excluded=0
    for rule in population:
        if should_pause():raise Paused()
        if set(rule.get('tags',[]))-{'HIGH_RISK_ALTERNATIVE'}:
            excluded+=1;continue
        pool.append(rule)
    pool.sort(key=lambda r:r['id']);n=len(pool);total=n*(n-1)//2
    binding=digest({'method':'FSL_COMPLEMENTARY_PAIRS_V1','code':source_fingerprint(),
                    'config':config,'pool':[(r['id'],r['_trade_ref'] if '_trade_ref' in r else r['_trades']) for r in pool]})
    conn=sqlite3.connect(root/'pairs.sqlite3')
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS meta(id INTEGER PRIMARY KEY,body TEXT,hash TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS pairs(id INTEGER PRIMARY KEY,body TEXT,hash TEXT)')
        old=conn.execute('SELECT body,hash FROM meta WHERE id=1').fetchone()
        state={'binding':binding,'next':0,'qualified_pairs':0,'best':None,'status':'RUNNING'}
        if old:
            state=json.loads(old[0])
            if digest(state)!=old[1] or state.get('binding')!=binding:
                raise ValueError('互补研究断点身份或内容改变')
        count=0
        for index,body,h in conn.execute('SELECT id,body,hash FROM pairs ORDER BY id'):
            if should_pause():raise Paused()
            if index!=count or digest(json.loads(body))!=h:
                raise ValueError('互补研究逐对审计内容或顺序改变')
            count+=1
        if count!=state['next']:raise ValueError('互补研究审计账与游标不一致')
        availability=portfolio_availability(events,labels,config)
        years=sorted({l['year'] for l in labels.values() if l.get('eligible')})
        limit=int(config.get('complementarity_pair_budget',10000))
        ratio=config['portfolio_min_return_drawdown_ratio']
        def save():
            conn.execute('INSERT INTO meta VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,hash=excluded.hash',(canonical(state),digest(state)))
            conn.commit()
        while state['next']<total:
            if should_pause():state['status']='PAUSED';save();raise Paused()
            if limit and state['next']>=limit:state['status']='PARTIAL_PAIR_BUDGET';break
            i,j=unrank_combination(n,2,state['next']);pair=[pool[i],pool[j]]
            orders,_=dispatch(pair,4,config['match_cap'],priority_mode='frozen_rule_priority')
            m=portfolio_metrics(orders,scale,years);m['segment_availability']=availability
            feasible=portfolio_qualifies(m,config,ratio)
            row={'ids':[r['id'] for r in pair],'metrics':m,'portfolio_qualified':feasible}
            conn.execute('INSERT INTO pairs VALUES(?,?,?)',(state['next'],canonical(row),digest(row)))
            if feasible:
                state['qualified_pairs']+=1
                best=state['best']
                rank=lambda x:(-x['metrics']['net_i'],x['metrics']['drawdown_match_i'],tuple(x['ids']))
                if best is None or rank(row)<rank(best):state['best']=row
            state['next']+=1
            if state['next']%64==0:
                save();update(message='独立互补研究：不改变主名单',complementary_pairs=state['next'],complementary_pairs_total=total)
        if state['next']==total:state['status']='PAIR_STUDY_COMPLETE'
        save()
        result={**state,'eligible_representatives':n,'excluded_for_nonrisk_reasons':excluded,
                'planned_pairs':total,'remaining_pairs':total-state['next'],
                'scope':'All unordered pairs passing non-risk single-rule gates; not arbitrary-size portfolio optimization',
                'primary_roster_changed':False,'execution_handoff_verified':False,'live_enabled':False,'second_slot_enabled':False}
        if state['best']:
            ids=set(state['best']['ids'])
            result['best_pair_rules']=[{k:v for k,v in r.items() if not k.startswith('_')} for r in pool if r['id'] in ids]
            result['best_pair_segments']=segment_evidence(state['best']['metrics'],config,portfolio=True)
        atomic_json(root/'result.json',result)
        return result
    finally:conn.close()
