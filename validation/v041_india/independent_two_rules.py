"""Independent two-rule audit from raw CSV. No production lab imports."""
from pathlib import Path
from decimal import Decimal,ROUND_FLOOR,ROUND_CEILING
from collections import defaultdict,Counter
from datetime import datetime
from itertools import groupby
from bisect import bisect_right
import csv,gzip,hashlib,json,time

ROOT=Path(__file__).resolve().parents[2]
JOB=ROOT/'workspace'/'20260913_020848_c15048'
OUT=ROOT/'validation'/'v041_india'
BASE=Path(r'C:\Users\Administrator\Desktop\球探皇冠平博完整指数数据\按国家分类数据')
HOME='LIVE_HOME_485b2317024b242b0aa9b61f';RECV='LIVE_RECEIVE_0ce1c3a6223664555fba1c97'
def plain(x):
    t=(x or '').strip()
    return t[2:-1] if t.startswith('="') and t.endswith('"') else t
def timestamp(x):return int((datetime.strptime(x,'%Y-%m-%d %H:%M')-datetime(1970,1,1)).total_seconds()/60)
def hfile(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()
def readgz(p):
    with gzip.open(p,'rt',encoding='utf-8') as f:return [json.loads(x) for x in f]
def js(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def write(p,obj):p.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
def sc(s):return [int(t) for t in s.replace(':','-').split('-')]
start=time.monotonic();inputs={};labels={};rows=[]
for y in (2024,2025,2026):
    p=BASE/'印度'/'印度超（皇冠开盘·326场）'/str(y)/'皇冠'/f'比赛索引_印度超_{y}.csv';inputs[str(p)]=hfile(p)
    with p.open(encoding='utf-8-sig',newline='') as f:
        for raw in csv.DictReader(f):
            r={k:plain(v) for k,v in raw.items()}
            if r['状态']=='完':labels[r['sId']]={'final':sc(r['全场比分']),'year':int(r['日期'][:4]),'kickoff':timestamp(r['开球时间'])}
    p=BASE/'皇冠开盘至少200场'/f'完整指数_印度超_{y}.csv';inputs[str(p)]=hfile(p)
    with p.open(encoding='utf-8-sig',newline='') as f:
        for row,raw in enumerate(csv.DictReader(f),2):
            r={k:plain(v) for k,v in raw.items()};assert r['联赛']=='印度超' and r['公司']=='皇冠'
            live=r['状态']=='滚';market=0 if r['盘口类型']=='大小球' else 1;closed=r['封盘']=='是'
            valid=not closed and bool(r['盘口数值']) and bool(r['上水/大球']) and bool(r['下水/小球'])
            line=int(Decimal(r['盘口数值'])*4) if r['盘口数值'] else None
            water=[int(Decimal(r[k])*100) if r[k] else None for k in ('上水/大球','下水/小球')]
            valid=bool(valid and min(water)>0 and (market==1 or line>=0))
            event={'sid':r['sId'],'market':market,'phase':int(live),'row':row,'ts':timestamp(r['变化时间']),'valid':valid,'line':line,'water':water,'score':sc(r['当时比分']) if live else [0,0], 'minute':int(r['比赛分钟']) if r['比赛分钟'].isdigit() else None,'event_key':inputs[str(p)]+':'+str(row)}
            assert sc(r['全场比分'])==labels[r['sId']]['final']
            rows.append(event)
sids=sorted({r['sid'] for r in rows});mid={sid:i for i,sid in enumerate(sids)}
rows.sort(key=lambda e:(e['sid'],e['market'],e['phase'],e['ts'],e['row']))
for eid,e in enumerate(rows):e['eid']=eid;e['mid']=mid[e['sid']]
groups=defaultdict(list)
for e in rows:groups[(e['sid'],e['market'])].append(e)
for g in groups.values():g.sort(key=lambda e:(e['ts'],e['row']))
times={key:[e['ts'] for e in g] for key,g in groups.items()}
signals=[];first_water={};fired=set()
for e in rows:
    if e['phase']!=1 or e['market']!=1 or not e['valid']:continue
    first_water.setdefault((e['sid'],e['line']),e['water'][0])
    conditions={HOME:e['water'][0]>=95 and e['water'][0]-first_water[(e['sid'],e['line'])]<=-10,
                RECV:e['line']<0 and e['minute'] is not None and 46<=e['minute']<61}
    for rid,hit in conditions.items():
        if hit and (rid,e['sid']) not in fired:
            fired.add((rid,e['sid']))
            signals.append({**{k:e[k] for k in ('sid','eid','event_key','ts','line','score')},'side':0,'water':e['water'][0],'strategy_id':rid,'direction':'LIVE_HOME' if rid==HOME else 'LIVE_RECEIVE'})

def settle(e,water,side):
    # Asian quarter line = equal stakes on the two adjoining half-goal lines.
    # Live AH is settled on goals AFTER the contemporaneous score, never FT margin alone.
    lab=labels[e['sid']];remaining=(lab['final'][0]-e['score'][0])-(lab['final'][1]-e['score'][1])
    line=Decimal(e['line'])/4;doubled=line*2
    halves=[doubled.to_integral_value(rounding=ROUND_FLOOR)/2,doubled.to_integral_value(rounding=ROUND_CEILING)/2]
    pnl=Decimal(0)
    for halfline in halves:
        advantage=(Decimal(remaining)-halfline)*(1 if side==0 else -1)
        pnl+=Decimal(water)/100/2 if advantage>0 else Decimal('-.5') if advantage<0 else 0
    assert pnl*200==(pnl*200).to_integral_value()
    return int(pnl*200)

def trade(signal,scenario):
    e=rows[signal['eid']];v=e;delay={0:0,1:0,2:1,3:2}[scenario];reduction=0 if scenario==0 else 5
    if delay:
        key=(e['sid'],e['market']);i=bisect_right(times[key],e['ts']+delay)-1
        if i<0:return None
        v=groups[key][i]
        if not v['valid'] or v['phase']!=e['phase'] or v['line']!=e['line'] or v['score']!=e['score'] or e['ts']+delay-v['ts']>5:return None
    water=v['water'][0]-reduction
    if water<=0:return None
    return {'sid':e['sid'],'mid':e['mid'],'eid':v['eid'],'signal_eid':e['eid'],'ts':e['ts']+delay,'quote_ts':v['ts'],'line':v['line'],'side':0,'water':water,'score':v['score'],'market':1,'phase':1,'year':labels[e['sid']]['year'],'sort_time':labels[e['sid']]['kickoff'],'pnl':settle(v,water,0),'stake':1,'quality':0,'strategy_id':signal['strategy_id'],'direction':signal['direction'],'priority':0 if signal['strategy_id']==HOME else 1}
def slot(t,policy):
    if policy=='legacy':return t['direction']
    return 'LIVE_PK_HOME' if t['line']==0 else 'LIVE_GIVE' if t['line']>0 else 'LIVE_RECEIVE'
def dispatch(offers,policy):
    accepted=[];rejected=[];used=set();contracts=set();stakes=Counter()
    ordered=sorted(offers,key=lambda t:(t['ts'],t['sid'],t['priority'],t['eid']))
    for key,it in groupby(ordered,key=lambda t:(t['ts'],t['sid'])):
        batch=list(it)
        for t in batch:
            sk=(t['sid'],slot(t,policy));q=t['eid'] if policy=='legacy' else t['quote_ts']
            contract=(t['sid'],1,1,q,0,t['line'],t['water'],tuple(t['score']))
            reason='同方向首单已占用' if sk in used else '相同报价合约重复' if contract in contracts else '整场模拟投入达上限' if stakes[t['sid']]>=4 else None
            # Both audited rules always buy the actual home side, so no opposite-side batch exists.
            if reason:rejected.append({**t,'reason':reason})
            else:accepted.append(t);used.add(sk);contracts.add(contract);stakes[t['sid']]+=1
    return accepted,rejected
def metrics(trades):
    ordered=sorted(trades,key=lambda x:(x['sort_time'],x['sid'],x['ts'],x['eid']));net=peak=drawdown=run=longest=0;outs=Counter();years=defaultdict(lambda:{'n':0,'net_i':0})
    for t in ordered:
        p=t['pnl'];net+=p;peak=max(peak,net);drawdown=max(drawdown,peak-net)
        if p<0:run+=1
        elif p>0:run=0
        longest=max(longest,run);years[str(t['year'])]['n']+=1;years[str(t['year'])]['net_i']+=p
        outs['push' if p==0 else 'loss' if p==-200 else 'half_loss' if p==-100 else 'win' if p==2*t['water'] else 'half_win' if p==t['water'] else 'unclassified']+=1
    return {'n':len(trades),'net_i':net,'net_units':net/200,'max_drawdown_units':drawdown/200,'max_loss_streak_ignore_push':longest,'outcomes':dict(outs),'years':dict(years),'matches':len({t['sid'] for t in trades})}
def diff(a,b):
    aa={json.dumps(x,sort_keys=True,ensure_ascii=False) for x in a};bb={json.dumps(x,sort_keys=True,ensure_ascii=False) for x in b}
    return {'missing':len(bb-aa),'extra_or_changed':len(aa-bb),'missing_samples':[json.loads(x) for x in list(bb-aa)[:5]],'extra_samples':[json.loads(x) for x in list(aa-bb)[:5]],'duplicate_rows_actual':len(a)-len(aa),'duplicate_rows_expected':len(b)-len(bb)}
packet=js(JOB/'results'/'rules.json');assert [r['id'] for r in packet['rules']]==[HOME,RECV]
report={'status':'RUNNING','job_id':JOB.name,'job_engine_hash':js(JOB/'config.json')['engine_hash'],'independent_implementation':'Python standard library only; no lab modules imported','rules':[HOME,RECV],'input_hashes':inputs,'scope':'Recreates raw-CSV first signals and quarter-line HK settlement for exactly two frozen LIVE AH rules; separately reproduces legacy execution and compares v3 core-slot counterfactual for the same roster. No search/selection correctness or independent external match-result certification.', 'signal_comparison':diff(signals,readgz(JOB/'results'/'golden_signals.jsonl.gz')),'scenarios':{},'rule_metrics':{},'frozen_artifact_hashes':{}}
for rid in (HOME,RECV):
    tt=[trade(s,0) for s in signals if s['strategy_id']==rid];mm=metrics(tt);expected=next(r['metrics'] for r in packet['rules'] if r['id']==rid)
    report['rule_metrics'][rid]={'independent':mm,'reported':expected,'core_totals_match':mm['n']==expected['n'] and mm['net_i']==expected['net_i'] and mm['max_drawdown_units']==expected['drawdown'] and mm['max_loss_streak_ignore_push']==expected['streak']}
for scenario in range(4):
    offers=[t for s in signals if (t:=trade(s,scenario)) is not None];orders,rejected=dispatch(offers,'legacy');core,core_rejected=dispatch(offers,'core');directory=JOB/'results'
    expected=readgz(directory/f'orders_s{scenario}.jsonl.gz');expected_rejections=readgz(directory/f'rejected_s{scenario}.jsonl.gz');by=defaultdict(list)
    for t in orders:by[(t['sid'],slot(t,'core'))].append(t)
    aliases=[{'sid':sid,'slot':sl,'orders':v} for (sid,sl),v in by.items() if len(v)>1]
    lower,_=dispatch([t for t in offers if t['strategy_id']==HOME],'legacy')
    report['scenarios'][str(scenario)]={'offers':len(offers),'order_comparison':diff(orders,expected),'rejection_comparison':diff(rejected,expected_rejections),'lower_risk_order_comparison':diff(lower,readgz(directory/'lower_risk'/f'orders_s{scenario}.jsonl.gz')),'legacy_metrics':metrics(orders),'core_slot_same_roster_metrics':metrics(core),'core_slot_extra_rejections':len(core_rejected)-len(rejected),'core_slot_duplicate_groups_in_legacy':len(aliases),'core_slot_duplicate_examples':aliases[:3]}
    with gzip.open(OUT/f'independent_india_two_rules_s{scenario}.jsonl.gz','wt',encoding='utf-8') as f:
        for t in orders:f.write(json.dumps(t,ensure_ascii=False,sort_keys=True)+'\n')
with gzip.open(OUT/'independent_india_two_rules_signals.jsonl.gz','wt',encoding='utf-8') as f:
    for s in signals:f.write(json.dumps(s,ensure_ascii=False,sort_keys=True)+'\n')
for p in [JOB/'results'/'rules.json',JOB/'results'/'golden_signals.jsonl.gz',
          *[JOB/'results'/f'orders_s{s}.jsonl.gz' for s in range(4)],
          *[JOB/'results'/f'rejected_s{s}.jsonl.gz' for s in range(4)],
          *[JOB/'results'/'lower_risk'/f'orders_s{s}.jsonl.gz' for s in range(4)]]:report['frozen_artifact_hashes'][str(p)]=hfile(p)
report['input_hashes_unchanged']=all(hfile(Path(p))==h for p,h in inputs.items())
comparisons=[report['signal_comparison'],*[v[k] for v in report['scenarios'].values() for k in ('order_comparison','rejection_comparison','lower_risk_order_comparison')]]
report['all_scoped_comparisons_pass']=all(x['missing']==x['extra_or_changed']==x['duplicate_rows_actual']==x['duplicate_rows_expected']==0 for x in comparisons) and all(v['core_totals_match'] for v in report['rule_metrics'].values()) and report['input_hashes_unchanged']
report['status']='PASS_TWO_RULE_SIGNALS_SETTLEMENT_LEGACY_POLICY' if report['all_scoped_comparisons_pass'] else 'FAIL'
report['v3_core_policy_status']='LEGACY_PORTFOLIO_NOT_V3_CORE_SLOT_COMPLIANT' if any(v['core_slot_duplicate_groups_in_legacy'] for v in report['scenarios'].values()) else 'PASS_FOR_THESE_SIGNALS'
report['elapsed_seconds']=time.monotonic()-start
write(OUT/'independent_india_two_rules.json',report)
print(json.dumps({'status':report['status'],'signal_comparison':report['signal_comparison'],'rule_metrics':{k:v['independent'] for k,v in report['rule_metrics'].items()},'scenarios':{k:{kk:vv for kk,vv in v.items() if not kk.endswith('examples')} for k,v in report['scenarios'].items()},'v3_core_policy_status':report['v3_core_policy_status']},ensure_ascii=False,indent=2))
