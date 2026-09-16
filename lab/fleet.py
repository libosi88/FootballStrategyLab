"""Offline whole-league assignment. Machines never share a live SQLite workspace."""
from pathlib import Path
import os,copy
from .common import atomic_json,read_json,check_config,digest,sha,source_fingerprint,DEFAULT
from .data import inspect
from .store import Store

SCHEMA='FSL_OFFLINE_LEAGUE_PLAN_V1'

def needed_on_node(rec,leagues,company):
    """Inputs a node must hold: quote and index files of its own leagues plus every quality list."""
    if rec['kind']=='quotes' and rec.get('targets'):return any(l in leagues and c==company for l,c in rec['targets'])
    if rec['kind']=='index' and rec.get('leagues'):return bool(set(rec['leagues'])&set(leagues))
    return True

def make_plans(inputs,output,nodes,company,config=None):
    if type(nodes) is not int or not 1<=nodes<=256:raise ValueError('机器数量须为1—256')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    config=dict(config or {})
    if company and config.get('company') and company!=config['company']:raise ValueError('--company 与配置文件中的公司不同，请只保留一个')
    company=company or config.get('company') or DEFAULT['company']
    cfg=check_config({**config,'company':company})
    catalog=inspect(inputs,output/'inspection_cache')
    targets=[r for r in catalog['leagues'] if r['company']==company]
    if not targets:raise ValueError('没有所选公司的联赛')
    records=catalog['files']
    try:data_root=Path(os.path.commonpath([str(Path(r['path']).resolve().parent) for r in records]))
    except ValueError:raise ValueError('多机计划要求输入在同一磁盘的数据树内')
    relative=copy.deepcopy(catalog)
    for rec in relative['files']:rec['path']=Path(rec['path']).resolve().relative_to(data_root).as_posix()
    buckets=[{'node':i+1,'rows':0,'leagues':[]} for i in range(nodes)]
    for target in sorted(targets,key=lambda r:(-r.get('rows',0),r['league'])):
        slot=min(buckets,key=lambda b:(b['rows'],b['node']))
        slot['leagues'].append(target['league']);slot['rows']+=max(1,target.get('rows',0))
    paths=[output/f"node_{b['node']:03d}.json" for b in buckets]
    if any(p.exists() for p in paths):raise FileExistsError('节点计划已存在，请指定新输出目录；不覆盖已分配任务')
    required=[sorted((r for r in relative['files'] if needed_on_node(r,set(b['leagues']),company)),key=lambda r:r['path']) for b in buckets]
    for bucket,files in zip(buckets,required):bucket.update(required_files=len(files),required_bytes=sum(r.get('size_bytes',0) for r in files))
    for bucket,files,path in zip(buckets,required,paths):
        plan={'schema':SCHEMA,'node':bucket['node'],'engine_hash':source_fingerprint(),'company':company,'config':cfg,
              'source_data_root':str(data_root),'leagues':bucket['leagues'],'estimated_quote_rows':bucket['rows'],'catalog':relative,
              'required_files':[r['path'] for r in files],'required_bytes':bucket['required_bytes'],
              'assignment_basis':'greedy balance by source quote rows; candidate-dependent runtime is not guaranteed equal'}
        atomic_json(path,{**plan,'plan_hash':digest(plan)})
    summary={'schema':SCHEMA,'nodes':nodes,'leagues':len(targets),'source_data_root':str(data_root),'plans':[{'file':str(p),**b} for p,b in zip(paths,buckets)],
             'instructions':'Copy each node plan\'s required_files (paths relative to source_data_root, keeping folders) to that machine. Import the node plan with --data-root and an independent local workspace. Use --queue to enqueue; otherwise jobs stay paused.'}
    atomic_json(output/'fleet_summary.json',summary);return summary

def import_plan(plan_file,data_root,workspace,queue=False):
    plan=read_json(plan_file)
    if not isinstance(plan,dict) or plan.get('schema')!=SCHEMA:raise ValueError('未知多机计划格式')
    if plan.get('plan_hash')!=digest({k:v for k,v in plan.items() if k!='plan_hash'}):raise ValueError('多机计划内容改变')
    if plan.get('engine_hash')!=source_fingerprint():raise ValueError('机器软件与分配计划的引擎指纹不同，请部署同一版本')
    cfg=check_config(plan['config']);base=Path(data_root).resolve()
    catalog=copy.deepcopy(plan['catalog']);wanted=set(plan['leagues'])
    # Verify only the assigned leagues' quote files plus their index/quality inputs, once per file.
    files=[]
    for rec in catalog['files']:
        # Quote and index files of leagues assigned to other machines need not be copied here.
        if not needed_on_node(rec,wanted,cfg['company']):continue
        p=(base/rec['path']).resolve()
        if base not in p.parents or not p.is_file():raise ValueError('计划输入不存在或越出数据目录: '+rec['path'])
        if sha(p)!=rec['sha256']:raise ValueError('计划输入哈希不一致: '+rec['path'])
        rec['path']=str(p);files.append(rec)
    catalog['files']=files;catalog['input_paths']=[str(base)]
    if not wanted:return {'node':plan['node'],'jobs':[],'queued':queue}
    available={(r['league'],r['company']) for r in catalog['leagues']}
    if any((league,cfg['company']) not in available for league in wanted):raise ValueError('计划联赛不在冻结目录中')
    store=Store(workspace);jobs=[];reused=[]
    for league in sorted(wanted):
        # Identity is engine + league + company + that league's input hashes + research config, never the plan file,
        # so regenerated plans and repeated imports reuse the job, while a software update starts a new one.
        # Result indexes and exclusion lists also define the frozen input version.
        inputs=sorted((r['kind'],r['sha256']) for r in files if needed_on_node(r,{league},cfg['company']))
        request=digest(['fleet-league',plan['engine_hash'],league,cfg['company'],inputs,cfg])
        with store.conn() as c:previous=c.execute('SELECT job FROM requests WHERE key=?',(request,)).fetchone()
        if previous:
            jid=previous['job'];job=store.get(jid)
            # The identity is content-based; a moved data root must not silently reuse a job whose frozen inputs point elsewhere.
            bound={r['sha256']:os.path.normcase(str(Path(r['path']).resolve())) for r in job['manifest'].get('files',[]) if r.get('path')}
            moved=[r['path'] for r in files if r['sha256'] in bound and bound[r['sha256']]!=os.path.normcase(str(Path(r['path']).resolve()))]
            if moved:raise ValueError(f'任务 {jid} 的冻结输入仍指向首次导入时的数据位置，与本次 --data-root 不同（例如 {moved[0]}）；请把数据放回原位置后再导入，或在新的工作区导入')
            reused.append(jid)
            if queue and job['status']=='PAUSED' and not job['progress']:store.control(jid,'resume')
        else:
            jid=store.create(league,cfg,catalog,start_paused=not queue,request_id=request)
            atomic_json(store.root/jid/'fleet_provenance.json',{'plan_hash':plan['plan_hash'],'node':plan['node'],'identity':request})
        jobs.append(jid)
    return {'node':plan['node'],'jobs':jobs,'reused':reused,'queued':queue,'workspace':str(store.root),'instructions':'Start the local workstation to process queued jobs; never share this SQLite workspace between machines.'}
