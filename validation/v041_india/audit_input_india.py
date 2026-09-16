"""Read-only source audit; writes reports only under validation/v041_india."""
from pathlib import Path
import csv,json,sys,collections,datetime,time
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from lab.common import plain,score,timestamp,scaled,MISSING,sha,source_fingerprint,atomic_json
from lab.catalog import build_catalog
from lab.data import load_inputs
BASE=Path(r'C:\Users\Administrator\Desktop\球探皇冠平博完整指数数据\按国家分类数据')
OUT=ROOT/'validation'/'v041_india'
paths=[BASE/'皇冠开盘至少200场'/f'完整指数_印度超_{y}.csv' for y in (2024,2025,2026)]
start=time.monotonic();initial={str(p):sha(p) for p in paths}
manifest=build_catalog(paths,OUT/'input_audit_catalog',[str(p) for p in paths])
atomic_json(OUT/'india_input_manifest.json',manifest)
events,labels,scale,audit=load_inputs(manifest,'印度超','皇冠')
report={'engine_hash':source_fingerprint(),'source_hashes':initial,'files':[],'production_audit':audit,'catalog':manifest['leagues'],'findings':{},'samples':{}}
counts=collections.Counter();samples=collections.defaultdict(list);sidrows=collections.defaultdict(list);quality=[]
def note(kind,path,rn,row):
    counts[kind]+=1
    if len(samples[kind])<12:samples[kind].append({'file':str(path),'row':rn,**{k:plain(row.get(k)) for k in ('sId','盘口类型','状态','比赛分钟','当时比分','全场比分','封盘','变化时间','开球时间','日期','比赛状态','盘口数值','上水/大球','下水/小球')}})
for path in paths:
    counter={k:collections.Counter() for k in ('league','company','market','phase','result_status','closed','minute')};sids=set();dates=[];n=0;duplicates=0;seen=set();by_sid=collections.defaultdict(list)
    with path.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f);header=reader.fieldnames
        for rn,row in enumerate(reader,2):
            n+=1;r={k:plain(v) for k,v in row.items()};sid=r['sId'];sids.add(sid);dates.append(r['日期']);by_sid[sid].append((rn,r));sidrows[sid].append((path,rn,r))
            for key,col in [('league','联赛'),('company','公司'),('market','盘口类型'),('phase','状态'),('result_status','比赛状态'),('closed','封盘'),('minute','比赛分钟')]:counter[key][r[col]]+=1
            key=tuple(row.items());duplicates+=key in seen;seen.add(key)
            if r['封盘'] not in ('','是'):note('unknown_closed_value',path,rn,r)
            if timestamp(r['变化时间'])==MISSING:note('invalid_quote_time',path,rn,r)
            if timestamp(r['开球时间'])==MISSING:note('invalid_kickoff_time',path,rn,r)
            try:datetime.date.fromisoformat(r['日期'])
            except ValueError:note('invalid_date',path,rn,r)
            if score(r['全场比分'])[0]==MISSING:note('missing_final',path,rn,r)
            if r['状态']=='滚' and score(r['当时比分'])[0]==MISSING:note('missing_live_score',path,rn,r)
            if r['状态']=='滚' and not(r['比赛分钟'].isdigit() or r['比赛分钟']=='中场'):note('unknown_live_minute',path,rn,r)
            if r['封盘']!='是' and (scaled(r['盘口数值'],4)==MISSING or min(scaled(r[k],scale) for k in ('上水/大球','下水/小球'))<=0):note('invalid_open_price',path,rn,r)
            if r['状态']=='滚' and score(r['当时比分'])[0]!=MISSING and score(r['全场比分'])[0]!=MISSING and any(a>b for a,b in zip(score(r['当时比分']),score(r['全场比分']))):note('live_score_exceeds_final',path,rn,r)
            if score(r['半场比分'])[0]!=MISSING and score(r['全场比分'])[0]!=MISSING and any(a>b for a,b in zip(score(r['半场比分']),score(r['全场比分']))):note('half_score_exceeds_final',path,rn,r)
    report['files'].append({'path':str(path),'sha256':initial[str(path)],'headers':header,'rows':n,'matches':len(sids),'date_min':min(dates),'date_max':max(dates),'counters':{k:dict(v) for k,v in counter.items()},'exact_duplicate_rows':duplicates,'sids':sorted(sids)})
for sid,rows in sidrows.items():
    for fields,kind in [(('日期','开球时间','全场比分','比赛状态'),'label_conflict'),(('主队','客队'),'team_conflict')]:
        if len({tuple(row[k] for k in fields) for _,_,row in rows})>1:
            p,rn,row=rows[0];note(kind,p,rn,row)
    groups=collections.defaultdict(list)
    for p,rn,r in rows:groups[(r['盘口类型'],r['状态']=='滚')].append((p,rn,r))
    for (market,live),group in groups.items():
        group.sort(key=lambda q:(timestamp(q[2]['变化时间']),q[1]));last_score=None;last_min=None;same_times=collections.defaultdict(set)
        for p,rn,r in group:
            same_times[r['变化时间']].add((r['当时比分'],r['盘口数值'],r['上水/大球'],r['下水/小球'],r['状态'],r['封盘'],r['比赛分钟']))
            sc=score(r['当时比分'])
            if live and sc[0]!=MISSING:
                if last_score is not None and any(a<b for a,b in zip(sc,last_score)):note('score_decrease_within_market',p,rn,r)
                last_score=sc
            if live and r['比赛分钟'].isdigit():
                mn=int(r['比赛分钟'])
                if last_min is not None and mn<last_min:note('minute_decrease_within_market',p,rn,r)
                last_min=mn
        counts['same_minute_multi_state_groups']+=sum(len(v)>1 for v in same_times.values())
    live=[timestamp(r['变化时间']) for _,_,r in rows if r['状态']=='滚']
    if live:
        for p,rn,r in rows:
            if r['状态'] in ('早','即') and timestamp(r['变化时间'])>=min(live):note('pre_quote_at_or_after_first_live',p,rn,r)
    indexpath=BASE/'印度'/'印度超（皇冠开盘·326场）'/rows[0][2]['日期'][:4]/'皇冠'/f"比赛索引_印度超_{rows[0][2]['日期'][:4]}.csv"
    if indexpath.exists():
        with indexpath.open(encoding='utf-8-sig',newline='') as f:
            found=[{k:plain(v) for k,v in r.items()} for r in csv.DictReader(f) if plain(r.get('sId'))==sid]
        if len(found)!=1:note('index_identity_nonunique',*rows[0])
        elif any(found[0][k]!=rows[0][2][('比赛状态' if k=='状态' else k)] for k in ('全场比分','日期','开球时间','状态')):note('index_quote_label_mismatch',*rows[0])
for rec in manifest['files']:
    if rec['kind']!='quality':continue
    with Path(rec['path']).open(encoding='utf-8-sig',newline='') as f:
        for rn,r in enumerate(csv.DictReader(f),2):
            if plain(r['sId']) in labels:quality.append({'path':rec['path'],'row':rn,**r,'production_eligible':labels[plain(r['sId'])]['eligible']})
report['quality_rows_matching_india']=quality
report['year_eligible']={str(y):sum(l['eligible'] and l['year']==y for l in labels.values()) for y in (2024,2025,2026)}
report['label_sources']=dict(collections.Counter(l['label_source'] for l in labels.values()))
report['direction_quote_counts']={f'{phase}_{market}':sum(e['phase']==phase and e['market']==market and e['valid'] and labels[e['sid']]['eligible'] for e in events) for phase in (0,1) for market in (0,1)}
report['findings']=dict(counts);report['samples']=dict(samples)
report['ineligible_labels']={s:l for s,l in labels.items() if not l['eligible']}
report['input_hashes_unchanged']=all(sha(p)==h for p,h in initial.items())
report['elapsed_seconds']=time.monotonic()-start
atomic_json(OUT/'india_input_audit.json',report)
print(json.dumps({k:v for k,v in report.items() if k not in ('files','production_audit','samples','source_hashes')},ensure_ascii=False,indent=2))
