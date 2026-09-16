"""Resumable all-pool empirical risk comparisons, never an independence certificate.

Every unordered pair of registered signature groups is accounted for. Pairs that share at
least one match get a detailed CSV row; pairs without any shared match are counted exactly
(also per direction pair) instead of one line each, so the output grows with real overlap
rather than with n*(n-1)/2.
"""
from pathlib import Path
import csv,io,os,hashlib,shutil
import numpy as np
from .common import canonical,digest,read_json,atomic_json,historical_objective
from .mining import Paused,ResourcePaused
from .sparse_plan import close_graph

FIELDS=['策略A','策略B','方向A','方向B','A场数','B场数','共同比赛','比赛Jaccard','A被B覆盖率','B被A覆盖率','相同报价合约','共同比赛同市场同侧','共同亏损场数','共同可观察比赛','共同可观察活跃并集','资料不足并集场数','活跃并集收益相关','退化说明']
CHUNK=64
# Budget/resource knobs gate how much is computed, never what a computed cell contains.
POOL_BUDGET_KEYS=('max_pool_pairs','max_run_minutes','max_output_mb','min_free_disk_mb','max_feature_cache_mb','standard_review_budget')

def all_pool_diagnostics(root,population,events,labels,config,update,should_pause):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    ids=[{'id':r['id'],'direction':r['direction'],'signature':r['signature'],'trade_sha256':r['_trade_ref']['sha256']} for r in population]
    sids=sorted(s for s,l in labels.items() if l['eligible']);sid_index={s:i for i,s in enumerate(sids)};n,m=len(ids),len(sids)
    from .diagnostics import observation_sets
    source_observed=observation_sets(events,labels,sorted({x['direction'] for x in ids}),config)
    observation_hash=digest([(k,sorted(v)) for k,v in sorted(source_observed.items())])
    historical=historical_objective(config)
    # Raising a stopped budget must resume the committed matrix, not invalidate its scope.
    binding=digest({'version':'FSL_ALL_POOL_RISK_V4','ids':ids,'matches':sids,'config':{k:v for k,v in config.items() if k not in POOL_BUDGET_KEYS},'observations':observation_hash})
    total_pairs=n*(n-1)//2
    scope={'scope':'all_registered_profitable_historical_signature_groups','future_independence_certified':False,'missing_data_treated_as_zero':False,
           'row_policy':'CSV lists every pair sharing at least one match; pairs without a shared match are counted exactly, not listed',
           'trade_basis':'historical_pregoal_rejected_scenario_4' if historical else 'minute_close_scenario_0','availability':'per_direction_evaluable_trigger_quotes'}
    if n==0 or m==0:
        result={'status':'COMPLETE','groups':n,'pairs':total_pairs,'pairs_evaluated':total_pairs,'pairs_with_shared_matches':0,'zero_overlap_pairs':total_pairs,'zero_overlap_by_direction':{},'all_pool_pairwise_done':True,**scope}
        atomic_json(root/'summary.json',result);return result
    meta=read_json(root/'matrix_state.json',{'binding':binding,'built':0,'chunk_hashes':[]})
    if meta['binding']!=binding:raise ValueError('全池风险矩阵范围改变（含引擎或研究口径升级）：请删除该任务的 all_pool_risk 目录后重算，或用原冻结源码继续')
    layout=(('pnl',np.int64),('present',np.bool_),('available',np.bool_),('side',np.int8),('contract','S32'))
    if not 0<=meta['built']<=n:raise ValueError('全池矩阵提交行数越界')
    if meta['built'] and any(not (root/(key+'.npy')).exists() for key,_ in layout):raise ValueError('已提交全池矩阵文件缺失，拒绝使用新建零矩阵冒充原结果')
    required=sum(n*m*np.dtype(dtype).itemsize+256 for key,dtype in layout if not (root/(key+'.npy')).exists())
    if required and shutil.disk_usage(root).free<required+config['min_free_disk_mb']*1048576:raise ResourcePaused('全池矩阵落盘空间不足，未缩小候选范围')
    arrays={}
    try:
        for key,dtype in layout:
            path=root/(key+'.npy');arrays[key]=np.lib.format.open_memmap(path,mode='r+' if path.exists() else 'w+',dtype=dtype,shape=(n,m))
            if arrays[key].shape!=(n,m) or arrays[key].dtype!=np.dtype(dtype):raise ValueError('全池矩阵形状/类型与冻结范围不符: '+key)
        # Committed rows are verified in blocks of CHUNK rows: one digest per array slice.
        def chunk_hash(start):
            stop=min(n,start+CHUNK)
            return digest([(key,hashlib.sha256(np.ascontiguousarray(arrays[key][start:stop]).tobytes()).hexdigest()) for key,_ in layout])
        if len(meta.get('chunk_hashes',[]))!=-(-meta['built']//CHUNK):raise ValueError('已提交风险矩阵缺少完整内容校验，不能续用旧断点')
        for number,expected in enumerate(meta['chunk_hashes']):
            if number%16==0 and should_pause():raise Paused()
            if chunk_hash(number*CHUNK)!=expected:raise ValueError('已提交全池风险矩阵内容改变')
        observed={k:{sid_index[s] for s in v if s in sid_index} for k,v in source_observed.items()}
        for row,r in enumerate(population):
            if row<meta['built']:continue
            if should_pause():raise Paused()
            for key in arrays:arrays[key][row]=0
            arrays['side'][row]=-1
            available=list(observed.get(r['direction'],()));arrays['available'][row,available]=True
            # Historical research compares the fills it counts (pre-goal rejected quotes removed); validation keeps scenario 0.
            for t in r['_trades'][4 if historical and len(r['_trades'])>4 else 0]:
                at=sid_index[t['sid']];arrays['pnl'][row,at]=t['pnl'];arrays['present'][row,at]=True;arrays['side'][row,at]=t['side']
                contract=(at,t['market'],t['phase'],t.get('quote_ts',t['ts']),t['side'],t['line'],t['water'],tuple(t['score']) if t['market']==1 else ())
                arrays['contract'][row,at]=hashlib.sha256(canonical(contract).encode()).digest()
            if (row+1)%CHUNK==0 or row+1==n:
                for a in arrays.values():a.flush()
                meta['built']=row+1;meta['chunk_hashes'].append(chunk_hash(row+1-((row+1-1)%CHUNK+1)))
                atomic_json(root/'matrix_state.json',meta);update(message='建立全池同场收益/覆盖矩阵',risk_groups_built=meta['built'],risk_groups_total=n)
        atomic_json(root/'population.json',{'binding':binding,'groups':ids,'matches':sids})
        state=read_json(root/'pair_state.json',{'binding':binding,'next_row':0,'pairs_evaluated':0,'pairs_with_shared_matches':0,'zero_overlap_pairs':0,'zero_overlap_by_direction':{},'bytes':0,'status':'RUNNING'})
        if state['binding']!=binding:raise ValueError('全池风险对照游标改变')
        path=root/'全池重叠与共同亏损.csv'
        if not 0<=state['next_row']<=n or state['bytes']<0 or not 0<=state['pairs_evaluated']<=total_pairs:raise ValueError('全池风险对照断点越界')
        if state['bytes'] and not path.is_file():raise ValueError('已提交全池对照CSV缺失，禁止补零冒充恢复')
        if path.exists() and path.stat().st_size<state['bytes']:raise ValueError('全池对照文件短于已提交断点')
        prefix_hash=hashlib.sha256()
        if state['bytes']:
            with path.open('rb') as checked:
                left=state['bytes']
                while left:
                    if should_pause():raise Paused()
                    chunk=checked.read(min(left,1048576))
                    if not chunk:raise ValueError('全池对照CSV校验期间缩短或消失')
                    prefix_hash.update(chunk);left-=len(chunk)
            if state.get('csv_sha256')!=prefix_hash.hexdigest():raise ValueError('全池对照CSV已提交内容校验失败')
        # Inverted match index: only groups that traded a common match can overlap.
        postings=[np.flatnonzero(arrays['present'][:,column]) for column in range(m)]
        directions=sorted({x['direction'] for x in ids});code={d:i for i,d in enumerate(directions)}
        direction_codes=np.array([code[x['direction']] for x in ids],np.int64)
        later_by_direction=np.zeros((n+1,len(directions)),np.int64)
        for row in range(n-1,-1,-1):
            later_by_direction[row]=later_by_direction[row+1];later_by_direction[row,direction_codes[row]]+=1
        with path.open('r+b' if path.exists() else 'w+b') as output:
            output.truncate(state['bytes']);output.seek(state['bytes'])
            if not state['bytes']:
                header=io.StringIO();writer=csv.DictWriter(header,fieldnames=FIELDS);writer.writeheader();data=('\ufeff'+header.getvalue()).encode('utf-8');output.write(data);prefix_hash.update(data)
            def save(status='RUNNING'):
                output.flush();os.fsync(output.fileno());state.update(bytes=output.tell(),status=status,csv_sha256=prefix_hash.hexdigest());atomic_json(root/'pair_state.json',state)
            limit=int(config.get('max_pool_pairs',0));last_saved=state['next_row']
            while state['next_row']<n:
                i=state['next_row']
                if should_pause():save('PAUSED');raise Paused()
                if limit and limit<=state['pairs_evaluated']<total_pairs:save('BUDGET_STOP');break
                columns=np.flatnonzero(arrays['present'][i])
                partners=np.unique(np.concatenate([postings[c] for c in columns])) if len(columns) else np.empty(0,np.int64)
                partners=partners[partners>i]
                a=arrays['present'][i];an=int(a.sum())
                for start in range(0,len(partners),256):
                    js=partners[start:start+256];b=arrays['present'][js];common=b&a;union=b|a
                    both=arrays['available'][js]&arrays['available'][i];active=both&union
                    x=arrays['pnl'][i].astype(float)*active;y=arrays['pnl'][js].astype(float)*active
                    count=active.sum(axis=1);sx=x.sum(axis=1);sy=y.sum(axis=1);vx=np.maximum(0,(x*x).sum(axis=1)-sx*sx/np.maximum(count,1));vy=np.maximum(0,(y*y).sum(axis=1)-sy*sy/np.maximum(count,1))
                    covariance=(x*y).sum(axis=1)-sx*sy/np.maximum(count,1);den=np.sqrt(vx*vy);corr=np.divide(covariance,den,out=np.zeros_like(den),where=den>0)
                    common_n=common.sum(axis=1);union_n=union.sum(axis=1);bn=b.sum(axis=1)
                    contracts=((arrays['contract'][js]==arrays['contract'][i])&common).sum(axis=1)
                    losses=((arrays['pnl'][js]<0)&(arrays['pnl'][i]<0)&common).sum(axis=1)
                    same_side=((arrays['side'][js]==arrays['side'][i])&common).sum(axis=1)
                    buffer=io.StringIO();writer=csv.DictWriter(buffer,fieldnames=FIELDS)
                    for offset,k in enumerate(js.tolist()):
                        same_market=ids[i]['direction'].endswith(('OVER','UNDER'))==ids[k]['direction'].endswith(('OVER','UNDER'))
                        good=count[offset]>1 and den[offset]>0
                        writer.writerow({'策略A':ids[i]['id'],'策略B':ids[k]['id'],'方向A':ids[i]['direction'],'方向B':ids[k]['direction'],
                            'A场数':an,'B场数':int(bn[offset]),'共同比赛':int(common_n[offset]),'比赛Jaccard':float(common_n[offset]/union_n[offset]) if union_n[offset] else None,
                            'A被B覆盖率':float(common_n[offset]/an) if an else None,'B被A覆盖率':float(common_n[offset]/bn[offset]) if bn[offset] else None,
                            '相同报价合约':int(contracts[offset]),'共同比赛同市场同侧':int(same_side[offset]) if same_market else 0,'共同亏损场数':int(losses[offset]),
                            '共同可观察比赛':int(both[offset].sum()),'共同可观察活跃并集':int(count[offset]),'资料不足并集场数':int((union[offset]&~both[offset]).sum()),
                            '活跃并集收益相关':float(np.clip(corr[offset],-1,1)) if good else None,'退化说明':'' if good else '有效共同观察不足或零方差；不能解释成无风险/独立'})
                    data=buffer.getvalue().encode('utf-8');output.write(data);prefix_hash.update(data)
                later=n-1-i;overlap=len(partners)
                zero=later_by_direction[i+1]-np.bincount(direction_codes[partners],minlength=len(directions))
                for d,value in zip(directions,zero.tolist()):
                    if value:
                        name=ids[i]['direction']+'|'+d;state['zero_overlap_by_direction'][name]=state['zero_overlap_by_direction'].get(name,0)+value
                state['pairs_evaluated']+=later;state['pairs_with_shared_matches']+=overlap;state['zero_overlap_pairs']+=later-overlap;state['next_row']=i+1
                if state['next_row']-last_saved>=CHUNK or output.tell()-state['bytes']>=8*1048576:
                    save();last_saved=state['next_row']
                    update(message='全池两两覆盖与共同亏损实际计算',risk_pairs=state['pairs_evaluated'],risk_pairs_total=total_pairs)
                    if shutil.disk_usage(root).free<config['min_free_disk_mb']*1048576:save('RESOURCE_BLOCKED');raise ResourcePaused('全池风险对照落盘空间不足，已提交游标保留')
            if state['next_row']==n:save('COMPLETE')
            elif state['status']=='RUNNING':save()
        done=state['status']=='COMPLETE' and state['pairs_evaluated']==total_pairs
        result={'status':state['status'],'groups':n,'pairs':total_pairs,'pairs_evaluated':state['pairs_evaluated'],'pairs_with_shared_matches':state['pairs_with_shared_matches'],
                'zero_overlap_pairs':state['zero_overlap_pairs'],'zero_overlap_by_direction':state['zero_overlap_by_direction'],'all_pool_pairwise_done':done,**scope}
        atomic_json(root/'summary.json',result);return result
    finally:
        for a in arrays.values():close_graph(a)
