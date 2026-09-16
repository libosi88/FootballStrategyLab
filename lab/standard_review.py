"""Persistent full-candidate review with reversible family/logic reuse.

Basic failures may be shared only by identical full historical signal masks.
Every normalized candidate that survives them receives its own perturbations.
"""
from pathlib import Path
from collections import OrderedDict
from itertools import product
import sqlite3,json,zlib,hashlib
import numpy as np
from .common import *
from .mining import Paused,first_indices
from .rules import rule_id,label
from .normalization import normalize_conditions
from .standard_atoms import AtomCatalog
from .standard_columns import StandardColumns
from .standard_search import candidate_families,family_conditions_from

class RowStream:
    def __init__(self,path,query,params=()):self.path=Path(path);self.query=query;self.params=params
    def __iter__(self):
        conn=sqlite3.connect(self.path)
        try:
            for (body,) in conn.execute(self.query,self.params):yield json.loads(body)
        finally:conn.close()

class TradeArchive:
    def __init__(self,path,conn=None,max_cache_bytes=32*1024*1024):
        self.path=Path(path);self.conn=conn or sqlite3.connect(self.path);self.owns=conn is None;self.cache=OrderedDict()
        self.cache_bytes=0;self.max_cache_bytes=max(0,int(max_cache_bytes))
        self.conn.execute('CREATE TABLE IF NOT EXISTS trades(signature TEXT PRIMARY KEY,sha TEXT,payload BLOB)')
    def put(self,signature,trades):
        raw=canonical(trades).encode();h=hashlib.sha256(raw).hexdigest()
        old=self.conn.execute('SELECT sha FROM trades WHERE signature=?',(signature,)).fetchone()
        if old and old[0]!=h:raise ValueError('同历史签名对应不同压力订单，禁止复用')
        self.conn.execute('INSERT OR IGNORE INTO trades VALUES(?,?,?)',(signature,h,zlib.compress(raw,3)))
        return {'signature':signature,'sha256':h,'database':self.path.name}
    def get(self,ref):
        key=ref['signature']
        if key in self.cache:
            result,h,size=self.cache[key]
            if h!=ref['sha256']:raise ValueError('候选逐笔档案引用校验失败')
            self.cache.move_to_end(key);return result
        row=self.conn.execute('SELECT sha,payload FROM trades WHERE signature=?',(key,)).fetchone()
        if row is None:raise ValueError('候选逐笔档案缺失')
        raw=zlib.decompress(row[1])
        if row[0]!=ref['sha256'] or hashlib.sha256(raw).hexdigest()!=row[0]:raise ValueError('候选逐笔档案校验失败')
        result=json.loads(raw)
        # Budget measures decoded JSON bytes, not a process RSS guarantee.
        if len(raw)<=self.max_cache_bytes:
            self.cache[key]=(result,row[0],len(raw));self.cache_bytes+=len(raw)
            while self.cache_bytes>self.max_cache_bytes or len(self.cache)>128:
                _,(_,_,size)=self.cache.popitem(last=False);self.cache_bytes-=size
        return result
    def close(self):
        if self.owns:self.conn.close()
        self.cache.clear();self.cache_bytes=0

class LazyRule(dict):
    def __init__(self,body,archive):super().__init__(body);self.archive=archive
    def __getitem__(self,key):
        if key=='_trades':return self.archive.get(dict.__getitem__(self,'_trade_ref'))['major']
        return dict.__getitem__(self,key)

class ComparisonPopulation:
    def __init__(self,path,archive):self.path=Path(path);self.archive=archive
    def __len__(self):
        return self.archive.conn.execute('SELECT COUNT(*) FROM comparison_rules').fetchone()[0]
    def __iter__(self):
        for body,priority in self.archive.conn.execute('SELECT c.body,p.priority FROM comparison_rules c JOIN priorities p ON c.direction=p.direction AND c.signature=p.signature ORDER BY p.priority'):
            r=json.loads(body);r['_execution_priority']=priority;yield LazyRule(r,self.archive)

def basic_reasons(base,m,stress,config,scale,availability=None,rejection=None):
    """Scale-aware gates (research_standard.research_gate_reasons). Without an explicit availability
    the rule's own matches per research segment stand in for it."""
    from .research_standard import research_gate_reasons
    if availability is None:
        matches={}
        for t in base:matches.setdefault(t.get('segment',''),set()).add(t['sid'])
        availability={segment:len(sids) for segment,sids in sorted(matches.items())}
    return research_gate_reasons(base,m,stress,config,scale,availability,rejection)

def diagnostic_variants(conditions,scale):
    from .selection import neighbors
    per=[];seen=set()
    for index,a in enumerate(conditions):
        alternatives=neighbors(a,scale)
        per.append(alternatives)
        for alt in alternatives:
            cs=conditions[:];cs[index]=alt;key=digest([{k:v for k,v in x.items() if k!='label'} for x in cs])
            if key not in seen:seen.add(key);yield 'single',index,cs
    # The full registered simultaneous numeric/semantic alternatives, not a
    # selected profitable subset. At most three conjunctive conditions exist.
    # Conditions without a registered neighbourhood stay fixed here and additionally
    # receive the quote-loss ordering check (needs_quote_loss_check).
    if sum(1 for x in per if x)>1:
        for cs in product(*[x if x else [conditions[i]] for i,x in enumerate(per)]):
            key=digest([{k:v for k,v in x.items() if k!='label'} for x in cs])
            if key not in seen:seen.add(key);yield 'joint',-1,list(cs)

def needs_quote_loss_check(conditions,scale):
    from .selection import neighbors
    return not conditions or any(not neighbors(a,scale) for a in conditions)

def priced_stress_trades(major,extra):
    """CSV/stress rows: s0–s3, minus 0.02, then pre-goal rejection. Do not index [*major, extra]."""
    if len(major)<5:raise ValueError('标准压力档案必须含分钟末四情景和进球前拒单')
    return [*major[:4],extra,major[4]]

def pregoal_disclosure(base,kept,scale,years,segments=None):
    """Per-rule disclosure of fills removed by the pre-goal rejection scenario."""
    from .selection import metrics,PREGOAL_REJECTION_POLICY
    kept_keys={(t['sid'],t['eid'],t['signal_eid']) for t in kept}
    removed=[t for t in base if (t['sid'],t['eid'],t['signal_eid']) not in kept_keys]
    return {'policy':PREGOAL_REJECTION_POLICY,'replacement_order':False,'rejected_n':len(removed),
            'rejected_net':sum(t['pnl'] for t in removed)/(2*scale),'metrics':metrics(kept,scale,years,segments),
            'removed_winners':sum(t['pnl']>0 for t in removed),'removed_losers':sum(t['pnl']<0 for t in removed),
            'removed_pushes':sum(t['pnl']==0 for t in removed),'assumed_rejections_not_confirmed':True,
            'evidence_basis':'后续一分钟明确封盘与比分变化的历史代理；不是成交回执或事前触发特征'}

def scope_years(events,labels):
    """Years with eligible valid quotes per (phase, market); a missing live year is not a sample failure."""
    years={}
    for e in events:
        label=labels.get(e['sid']) or {}
        if e['valid'] and label.get('eligible') and label.get('year'):years.setdefault((e['phase'],e['market']),set()).add(label['year'])
    return {key:sorted(values) for key,values in years.items()}

def direction_years(years_by_scope,direction):
    return years_by_scope.get((0 if direction.startswith('PRE') else 1,0 if direction.endswith(('OVER','UNDER')) else 1),[])

def family_report(x,config):
    m=x['execution_metrics'] if historical_objective(config) else x['metrics']
    return {'方向':x['direction'],'家族ID':x['family_id'],'可恢复表达发生数':x['expression_occurrences'],
            '收益口径':'分钟末历史净胜（剔除进球前拒单）' if historical_objective(config) else '触发瞬间研究净胜',
            '净胜':m['net'],'场次':m['n'],'触发瞬间净胜对照':x['metrics']['net'],'状态':x['status'],
            '原因':'；'.join(x['reasons']),'研究路径原因':'；'.join(x.get('research_reasons',[])),'标签':'；'.join(x['tags'])}

def select_standard(jobdir,events,labels,scale,config,update,should_pause):
    from .selection import Quotes,metrics,row_report,finish_selection
    jobdir=Path(jobdir);dest=jobdir/'results';dest.mkdir(exist_ok=True);db=dest/'standard_review.sqlite3'
    conn=sqlite3.connect(db)
    conn.execute('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,body TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS families(direction TEXT,id INTEGER,body TEXT,PRIMARY KEY(direction,id))')
    conn.execute('CREATE TABLE IF NOT EXISTS rules(id TEXT PRIMARY KEY,direction TEXT,signature TEXT,qualified INTEGER,body TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS neighbors(rule TEXT,key TEXT,body TEXT,PRIMARY KEY(rule,key))')
    conn.execute('CREATE TABLE IF NOT EXISTS stress(rule TEXT,scenario TEXT,body TEXT,PRIMARY KEY(rule,scenario))')
    conn.execute('CREATE TABLE IF NOT EXISTS cursors(direction TEXT PRIMARY KEY,body TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS comparison_rules(direction TEXT,signature TEXT,cost INTEGER,id TEXT,body TEXT,PRIMARY KEY(direction,signature))')
    conn.execute('CREATE TABLE IF NOT EXISTS priorities(direction TEXT,signature TEXT,priority INTEGER,PRIMARY KEY(direction,signature))')
    binding=digest({'version':VERSION,'engine':source_fingerprint(),'config':config,'input':read_json(jobdir/'prepared_manifest.json'),'mining':{d:read_json(jobdir/'mining'/d/'state.json') for d in config['directions']}})
    old=conn.execute("SELECT body FROM metadata WHERE key='binding'").fetchone()
    if old and old[0]!=binding:conn.close();raise ValueError('全池审查断点绑定改变')
    conn.execute("INSERT OR IGNORE INTO metadata VALUES('binding',?)",(binding,));conn.commit()
    archive=TradeArchive(db,conn);quotes=Quotes(events,labels,scale,config['stale_minutes'],config['quote_cache_entries'])
    execution_quotes=Quotes(events,labels,scale,config['stale_minutes'],config['quote_cache_entries'],minute_close=True)
    discount=scaled('0.05',scale);summaries=[];total_candidates=0;all_done=True
    # Sample gates follow each direction's evaluable matches per research segment, trigger window included.
    from .research_standard import direction_availability,research_gate_reasons,required_matches
    years=sorted({l['year'] for l in labels.values() if l.get('eligible') and l.get('year')})
    availability_by_direction=direction_availability(events,labels,config['directions'],config)
    try:
        for direction in config['directions']:
            if should_pause():raise Paused()
            root=jobdir/'mining'/direction
            from .search_integrity import verify_stopped_search
            mined=verify_stopped_search(root)
            if not mined:raise ValueError('全池审查缺少已封存的搜索状态证据')
            total_candidates+=mined.get('candidates',0)
            availability=availability_by_direction[direction];segments=list(availability)
            row=conn.execute('SELECT body FROM cursors WHERE direction=?',(direction,)).fetchone()
            cursor=json.loads(row[0]) if row else {'last_family':0,'active_family':0,'offset':0,'reviewed_occurrences':0,'normalized_tests':0,'status':'RUNNING'}
            def save():
                conn.execute('INSERT INTO cursors VALUES(?,?) ON CONFLICT(direction) DO UPDATE SET body=excluded.body',(direction,canonical(cursor)));conn.commit()
            if mined['status']=='NO_DATA' or mined['status']=='COMPLETE' and mined.get('candidates')==0:cursor['status']='COMPLETE';save()
            if cursor['status']!='COMPLETE':
                with np.load(root/'arrays.npz',allow_pickle=False) as z:base={k:z[k] for k in z.files}
                with AtomCatalog(root/'atoms.sqlite3') as atoms,StandardColumns(base,events,direction,root/'review_columns',config['max_feature_cache_mb']) as arrays:
                    exhausted=False
                    for family in candidate_families(root,money_threshold(config['min_profit'],2*scale)):
                        if family['id']<=cursor['last_family']:continue
                        if should_pause():save();raise Paused()
                        representative=next(family_conditions_from(family['block'],family['anchor'],family['prefix']))
                        ix=first_indices(representative,arrays,atoms);raw=quotes.trades(arrays['eid'][ix],arrays['side'][ix])
                        eids,sides=arrays['eid'][ix],arrays['side'][ix]
                        # Scenario order follows selection.SCENARIO_NAMES: s0 minute-close price, s1-s3 stress, s4 pre-goal rejection.
                        major=[execution_quotes.trades(eids,sides),*[execution_quotes.trades(eids,sides,delay,discount) for delay in (0,1,2)],execution_quotes.trades(eids,sides,reject_pregoal=True)]
                        historical=historical_objective(config)
                        # Historical facts: a fill the pre-goal rule rejects could not be executed; it stays a zero-payoff first signal.
                        decision_trades=major[4] if historical else major[0]
                        first_count=len(ix) if historical else len(raw)
                        first_net=sum(t['pnl'] for t in (decision_trades if historical else raw))
                        if (first_count,first_net)!=(family['n'],family['net']):raise ValueError('标准候选家族与首次触发按冻结取价口径复算不一致')
                        extra=execution_quotes.trades(eids,sides,0,scaled('0.02',scale))
                        m=metrics(raw,scale,years,segments);sm=[metrics(t,scale,years,segments) for t in major[1:4]];extra_m=metrics(extra,scale,years,segments)
                        research_reasons,research_tags=research_gate_reasons(raw,m,sm,config,scale,availability)
                        execution_m=metrics(decision_trades,scale,years,segments)
                        if historical:execution_m['price_basis']='historical_minute_close_pregoal_rejected'
                        pregoal=pregoal_disclosure(major[0],major[4],scale,years,segments)
                        # Execution prices decide qualification: sample, segment coverage, return/drawdown ratio
                        # over every priced scenario (stress and pre-goal rejection), concentration and quality.
                        er,et=research_gate_reasons(decision_trades,execution_m,sm,config,scale,availability,pregoal['metrics'])
                        if execution_m['net_i']<=money_threshold(config['min_profit'],2*scale):er.append('执行原价净胜未过门槛');et.append('EXECUTION_ECONOMICS_FAILED')
                        if not historical_objective(config) and any(t['quality'] for trades in major[1:] for t in trades):er.append('压力或拒单情景报价存在已知比分质量旗标');et.append('QUALITY_BLOCKED')
                        reasons=list(dict.fromkeys(['分钟末执行：'+x for x in er]));tags=list(dict.fromkeys(et))
                        signature=digest([(t['signal_eid'],t['side']) for t in raw]);ref=archive.put(signature,{'major':major,'research_raw':raw,'minus_002':extra})
                        representative_conditions=normalize_conditions([atoms[i] for i in representative],scale)
                        if representative_conditions is None:
                            atomic_json(dest/'review_invariant_failure.json',{'status':'BLOCKED','direction':direction,'family_id':family['id'],'atom_ids':list(representative),'conditions':[atoms[i] for i in representative],'reason':'盈利家族代表逻辑矛盾；不能降级后继续授予覆盖证明'})
                            raise ValueError('盈利家族代表逻辑矛盾，证据已保存到review_invariant_failure.json')
                        representative_id=rule_id(direction,representative_conditions)
                        comparison={'id':representative_id,'direction':direction,'conditions':representative_conditions,'metrics':m,'execution_metrics':execution_m,'stress':sm,'minus_002':extra_m,'pregoal_rejection':pregoal,'signature':signature,
                                    'quality':sum(t['quality'] for t in raw),'status':'对照用家族代表','reason':'；'.join(reasons),'tags':tags,'_trade_ref':ref,
                                    'coverage_sids':[t['sid'] for t in decision_trades],'research_reason':'；'.join(research_reasons),'research_tags':research_tags}
                        old_comparison=conn.execute('SELECT cost,id FROM comparison_rules WHERE direction=? AND signature=?',(direction,signature)).fetchone()
                        cost=(len(representative_conditions),representative_id)
                        if old_comparison is None or cost<old_comparison:conn.execute('INSERT OR REPLACE INTO comparison_rules VALUES(?,?,?,?,?)',(direction,signature,*cost,canonical(comparison)))
                        family_body={'direction':direction,'family_id':family['id'],'expression_occurrences':family['weight'],'metrics':m,'execution_metrics':execution_m,'stress':sm,'minus_002':extra_m,'pregoal_rejection':pregoal,'signature':signature,'status':'BASIC_FAILED' if reasons else 'DIRECT_REVIEW_REQUIRED','reasons':reasons,'tags':tags,'research_reasons':research_reasons,'research_tags':research_tags,'expansion':'standard_search.family_conditions_from with standard_plan.json and atoms.sqlite3; all original expressions retained'}
                        conn.execute('INSERT OR REPLACE INTO families VALUES(?,?,?)',(direction,family['id'],canonical(family_body)))
                        if reasons:
                            cursor.update(last_family=family['id'],active_family=0,offset=0,reviewed_occurrences=cursor['reviewed_occurrences']+family['weight']);save();continue
                        start=cursor['offset'] if cursor['active_family']==family['id'] else 0
                        cursor['active_family']=family['id']
                        for offset,ids in enumerate(family_conditions_from(family['block'],family['anchor'],family['prefix'],start),start):
                            if should_pause():save();raise Paused()
                            limit=int(config.get('standard_review_budget',0))
                            if limit and cursor['normalized_tests']>=limit:cursor['status']='BUDGET_STOP';save();exhausted=True;break
                            conditions=normalize_conditions([atoms[i] for i in ids],scale)
                            if conditions is None:
                                atomic_json(dest/'review_invariant_failure.json',{'status':'BLOCKED','direction':direction,'family_id':family['id'],'offset':offset,'atom_ids':list(ids),'conditions':[atoms[i] for i in ids],'reason':'盈利家族中出现逻辑矛盾，覆盖证明不一致'})
                                raise ValueError('盈利家族中出现逻辑矛盾，证据已保存到review_invariant_failure.json')
                            rid=rule_id(direction,conditions)
                            existing=conn.execute('SELECT signature FROM rules WHERE id=?',(rid,)).fetchone()
                            if existing is not None and existing[0]!=signature:raise ValueError('逻辑规范化改变了历史信号，拒绝复用')
                            if existing is None:
                                failure=False;tested=0
                                diagnostics=not historical_objective(config) or config.get('historical_diagnostics',False)
                                for mode,ai,cs in (diagnostic_variants(conditions,scale) if diagnostics else ()):
                                    hit=np.ones(len(arrays['eid']),np.bool_)
                                    for a in cs:hit &= arrays.atom_mask(a)
                                    indexes=np.flatnonzero(hit)
                                    if len(indexes):indexes=indexes[np.r_[True,arrays['mid'][indexes[1:]]!=arrays['mid'][indexes[:-1]]]]
                                    trades=execution_quotes.trades(arrays['eid'][indexes],arrays['side'][indexes],0,discount);nm=metrics(trades,scale,years)
                                    status='PASS' if nm['n'] and nm['net_i']>0 else 'NO_SAMPLE' if not nm['n'] else 'FAIL';failure|=status!='PASS';tested+=1
                                    record={'候选ID':rid,'类型':mode,'变动原子':ai,'变体':' AND '.join(a['label'] for a in cs),'场次':nm['n'],'减水净胜':nm['net'],'状态':status}
                                    conn.execute('INSERT OR REPLACE INTO neighbors VALUES(?,?,?)',(rid,digest(cs),canonical(record)))
                                if diagnostics and (not tested or needs_quote_loss_check(conditions,scale)):
                                    # A predeclared quote-loss diagnostic for baseline rules and for every rule
                                    # with a condition lacking a numeric/semantic neighbourhood; choose the second
                                    # qualifying quote, never a later profitable quote selected with labels.
                                    hit=np.ones(len(arrays['eid']),np.bool_)
                                    for a in conditions:hit &= arrays.atom_mask(a)
                                    from .selection import second_quote_indices
                                    chosen=second_quote_indices(hit,arrays['mid']);nm=metrics(execution_quotes.trades(arrays['eid'][chosen],arrays['side'][chosen],0,discount),scale,years)
                                    status='PASS' if nm['n'] and nm['net_i']>0 else 'NO_SAMPLE' if not nm['n'] else 'FAIL';failure|=status!='PASS'
                                    record={'候选ID':rid,'类型':'first_quote_missing','变动原子':-1,'变体':'首次满足报价丢失，使用第二次满足报价；减水0.05','场次':nm['n'],'减水净胜':nm['net'],'状态':status}
                                    conn.execute('INSERT OR REPLACE INTO neighbors VALUES(?,?,?)',(rid,'first_quote_missing',canonical(record)))
                                diagnostic_failure=failure
                                if historical_objective(config):failure=False
                                rule={'id':rid,'direction':direction,'conditions':conditions,'metrics':m,'execution_metrics':execution_m,'stress':sm,'minus_002':extra_m,'pregoal_rejection':pregoal,'signature':signature,'quality':sum(t['quality'] for t in raw),'diagnostics_run':diagnostics,'diagnostic_failure':diagnostic_failure,
                                      'status':'直接扰动未通过' if failure else '历史稳定性达标' if historical_objective(config) else '模拟比较资格','reason':'已登记单项/联合/语义扰动存在失败或无样本' if failure else '',
                                      '_trade_ref':ref,'normalization':'same-feature integer interval AND plus explicit default normalization; empirical equivalence is separate'}
                                conn.execute('INSERT INTO rules VALUES(?,?,?,?,?)',(rid,direction,signature,int(not failure),canonical(rule)))
                                scenario_trades=priced_stress_trades(major,extra)
                                if historical:scenario_trades[0]=decision_trades
                                for scenario,mm in enumerate([execution_m,*sm,extra_m,pregoal['metrics']]):
                                    rec={'候选ID':rid,'情景':scenario if scenario<4 else '减水0.02' if scenario==4 else '进球前报价拒单','场次':mm['n'],'净胜':mm['net'],'回撤':mm['drawdown'],'收益回撤比':mm['net']/mm['drawdown'] if mm['drawdown'] else None,'最大连亏':mm['streak'],'保留率':mm['n']/max(1,m['n']),'年份场次':canonical(mm['year_counts']),'研究段场次':canonical(mm.get('segment_counts',{})),'最小年度场次':min(mm['year_counts'].values(),default=0),'已知质量旗标':sum(t['quality'] for t in scenario_trades[scenario])}
                                    conn.execute('INSERT INTO stress VALUES(?,?,?)',(rid,str(scenario),canonical(rec)))
                                cursor['normalized_tests']+=1
                            cursor['offset']=offset+1;cursor['reviewed_occurrences']+=1
                            if cursor['offset']%64==0:
                                save();update(message='全池直接筛选与单项/联合扰动，按原始候选游标恢复',direction=direction,reviewed=cursor['reviewed_occurrences'],direct_rules=cursor['normalized_tests'])
                        if exhausted:break
                        cursor.update(last_family=family['id'],active_family=0,offset=0);save()
                    if not exhausted:cursor['status']='COMPLETE';save()
            if cursor['status']!='COMPLETE':all_done=False
            elif cursor['reviewed_occurrences']!=mined.get('candidates',0):
                raise ValueError(f"{direction} 全池复核表达发生数{cursor['reviewed_occurrences']}与搜索候选发生数{mined.get('candidates',0)}不一致，拒绝静默少审")
            q=conn.execute('SELECT COUNT(*) FROM rules WHERE direction=? AND qualified=1',(direction,)).fetchone()[0]
            available=sum(availability.values())
            summaries.append({'方向':DIRECTIONS[direction],'direction':direction,'候选数':mined.get('candidates',0),'资格数':q,'已选':0,'审查状态':cursor['status'],'已审查表达发生数':cursor['reviewed_occurrences'],
                              '方向可用场次':available,'所需场次':required_matches(config,available),'研究段可用场次':canonical(availability)})
        conn.commit()
        # Representatives share a priority based on the full historical signal
        # signature, frozen before portfolio comparison. Equivalent expressions
        # cannot change order precedence merely through a different strategy ID.
        reps={}
        for (body,) in conn.execute('SELECT body FROM rules WHERE qualified=1 ORDER BY id'):
            r=json.loads(body);key=(r['direction'],r['signature'])
            if key not in reps or (len(r['conditions']),r['id'])<(len(reps[key]['conditions']),reps[key]['id']):reps[key]=r
        qualified=[];ranking=[]
        # Frozen before portfolio comparison: worst priced scenario (stress and pre-goal rejection)
        # first, then more executable matches, fewer conditions; the signature only breaks exact ties.
        for direction,signature,body in conn.execute('SELECT direction,signature,body FROM comparison_rules'):
            r=json.loads(body);head=r.get('execution_metrics') or r['metrics']
            worst=head['net_i'] if historical_objective(config) else min([x['net_i'] for x in r['stress']]+([r['pregoal_rejection']['metrics']['net_i']] if r.get('pregoal_rejection') else []))
            ranking.append((-worst,-head['n'],len(r['conditions']),direction,signature))
        for priority,(*_,direction,signature) in enumerate(sorted(ranking)):
            conn.execute('INSERT OR REPLACE INTO priorities VALUES(?,?,?)',(direction,signature,priority))
        conn.commit()
        for key in sorted(reps):
            r=reps[key];r['_execution_priority']=conn.execute('SELECT priority FROM priorities WHERE direction=? AND signature=?',key).fetchone()[0];qualified.append(LazyRule(r,archive))
        stress_rows=RowStream(db,'SELECT body FROM stress ORDER BY rule,scenario');neighbor_rows=RowStream(db,'SELECT body FROM neighbors ORDER BY rule,key')
        csv_write(dest/'全部候选家族审查.csv',(family_report(x,config) for x in RowStream(db,'SELECT body FROM families ORDER BY direction,id')))
        csv_write(dest/'全部候选审查.csv',(row_report(r) for r in RowStream(db,'SELECT body FROM rules ORDER BY id')))
        population=ComparisonPopulation(db,archive)
        csv_write(dest/'高风险备选.csv',(row_report(r) for r in population if 'HIGH_RISK_ALTERNATIVE' in r.get('tags',[])))
        csv_write(dest/'低样本观察.csv',(row_report(r) for r in population if 'LOW_SAMPLE_OBSERVATION' in r.get('tags',[])))
        csv_write(dest/'直接邻域测试.csv',neighbor_rows);csv_write(dest/'单条报价压力.csv',stress_rows)
        from .pool_diagnostics import all_pool_diagnostics
        if historical_objective(config) and not config.get('historical_diagnostics',False):
            pool_risk={'status':'NOT_REQUESTED','scope':'optional pairwise risk diagnostics; all profitable families and required historical stability checks are preserved'}
        else:
            pool_risk=all_pool_diagnostics(dest/'all_pool_risk',population,events,labels,config,update,should_pause)
            if pool_risk['status']!='COMPLETE':all_done=False
        review={'complete':all_done,'research_standard':'FSL_HISTORICAL_V1: full selected history, minute-close profit pool above the frozen min_profit, historical duration/segment coverage and base-price return/drawdown; optional stress diagnostics do not gate selection' if historical_objective(config) else 'FSL_RESEARCH_STANDARD_V2: sample share of direction-available matches, research-segment coverage, return/drawdown ratio over every priced scenario','direction_availability':availability_by_direction,'candidate_expression_occurrences':total_candidates,'direct_normalized_rules':conn.execute('SELECT COUNT(*) FROM rules').fetchone()[0],
                'qualified_signature_groups':len(qualified),'family_ledger':'standard_review.sqlite3','original_expression_mapping':'../mining/<direction>/standard_plan.json + atoms.sqlite3 + search.sqlite3',
                'same_history_is_not_future_equivalence':True,'priority_basis':'direction_and_signal_signature_frozen_before_portfolio_search','all_pool_risk':pool_risk,'directions':summaries}
        atomic_json(dest/'全池审查范围.json',review)
        result=finish_selection(jobdir,events,labels,scale,config,update,should_pause,qualified,total_candidates,summaries,neighbor_rows,stress_rows,comparison_pool=population,review_complete=all_done)
        result['standard_review']=review
        if not all_done:result['selection_status']='PARTIAL_CANDIDATE_REVIEW'
        atomic_json(dest/'selection_summary.json',result);return result
    finally:archive.close();conn.close()
