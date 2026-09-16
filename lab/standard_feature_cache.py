"""Build all requested direction caches in one causal pass, bounded by disk."""
from pathlib import Path
import tempfile,os,shutil
import numpy as np
from .common import *
from .contracts import event_contract
from .features import FEATURE_NAMES,release_match_state
from .standard_features import StandardFeatureStream,EXTRA_FIELDS
from .sparse_plan import close_graph
from .mining import Paused,ResourcePaused


def direction_binding(jobdir,events,labels,scale,direction,config):
    return direction_identity(jobdir,events,labels,scale,direction,config)[0]

def direction_identity(jobdir,events,labels,scale,direction,config):
    data=read_json(Path(jobdir)/'prepared_manifest.json') or digest({'events':events,'labels':labels,'scale':scale})
    engine=source_fingerprint()
    context={'version':VERSION,'engine':engine,'direction':direction,'water_scale':scale,'config':config,'input':data}
    details={'version':VERSION,'engine':engine,'direction':direction,'water_scale':scale,'config_sha256':digest(config),'input_sha256':digest(data)}
    return digest(context),details

def check_direction_cache(metadata,binding,details,path):
    if metadata.get('binding')!=binding:
        old=metadata.get('binding_details') or {}
        if old and (old.get('engine')!=details['engine'] or old.get('version')!=details['version']):
            reason='SOURCE_REVISION_MISMATCH';message='缓存绑定源码与当前源码不同，可能在运行中编辑过源码，或正在复用另一版本；请固定旧源码复核，或新建研究'
        elif old and old.get('config_sha256')!=details['config_sha256']:
            reason='CONFIGURATION_MISMATCH';message='冻结参数已改变；请使用原参数或新建任务'
        elif old and (old.get('direction')!=details['direction'] or old.get('water_scale')!=details['water_scale']):
            reason='SCOPE_OR_UNIT_MISMATCH';message='联赛方向或水位刻度与缓存不一致'
        elif old and old.get('input_sha256')!=details['input_sha256']:
            reason='INPUT_DATA_MISMATCH';message='输入内容或预处理清单已改变；请重新检查输入并新建任务'
        elif old:reason='CACHE_BINDING_MISMATCH';message='缓存身份摘要不一致，已记录字段不足以进一步定位'
        else:reason='CACHE_BINDING_MISMATCH';message='旧缓存只有聚合身份摘要，不能进一步判定源码、参数或输入中的哪项变化；拒绝混用'
        raise ValueError(f'[{reason}] 缓存身份不匹配：{message}')
    if not Path(path).is_file():raise ValueError('[CACHE_FILE_MISSING] 缓存完整性失败：数组文件缺失')
    if sha(path)!=metadata.get('sha256'):raise ValueError('[CACHE_CONTENT_CHANGED] 缓存完整性失败：数组内容与保存的SHA-256不符')


def prepare_standard_features(jobdir,events,labels,scale,config,update,should_pause):
    jobdir=Path(jobdir);pending=[];bindings={};details_by_direction={};counts={}
    for direction in config['directions']:
        root=jobdir/'mining'/direction;root.mkdir(parents=True,exist_ok=True)
        binding,details=direction_identity(jobdir,events,labels,scale,direction,config);bindings[direction]=binding;details_by_direction[direction]=details
        metadata=read_json(root/'standard_features.json')
        if metadata:
            check_direction_cache(metadata,binding,details,root/'arrays.npz')
        else:pending.append(direction);counts[direction]=0
    if not pending:return {'status':'REUSED','directions':len(bindings)}
    names=sorted(FEATURE_NAMES|EXTRA_FIELDS|{'eid','mid','side','pnl','year','ts','quality'});name_set=set(names);lookup={}
    for direction in pending:
        lookup.setdefault((0 if direction.startswith('PRE') else 1,0 if direction.endswith(('OVER','UNDER')) else 1),[]).append(direction)
    from .research_standard import trigger_window_allows
    def targets(event):
        if not event['valid'] or not labels[event['sid']]['eligible'] or event['phase'] and event['market'] and event['score'][0]==MISSING:return ()
        # Early-market 早 quotes still feed every causal feature; they are only never trigger rows.
        if not trigger_window_allows(event,config):return ()
        return [(d,s) for d in lookup.get((event['phase'],event['market']),()) if (s:=side_for(d,event['line'])) is not None]
    # Unevaluable quotes are never silently dropped: every exclusion is counted per direction.
    excluded={d:{'closed_or_invalid':0,'ineligible_result':0,'missing_live_score':0,'outside_trigger_window':0,'nonapplicable_line':0} for d in pending}
    for event in events:
        chosen=targets(event);scope=lookup.get((event['phase'],event['market']),())
        for direction,side in chosen:counts[direction]+=1
        if len(chosen)==len(scope):continue
        taken={d for d,_ in chosen}
        for direction in scope:
            if direction in taken:continue
            row=excluded[direction]
            if not event['valid']:row['closed_or_invalid']+=1
            elif not labels[event['sid']]['eligible']:row['ineligible_result']+=1
            elif event['phase'] and event['market'] and event['score'][0]==MISSING:row['missing_live_score']+=1
            elif not trigger_window_allows(event,config):row['outside_trigger_window']+=1
            else:row['nonapplicable_line']+=1
    required=sum(counts.values())*len(names)*8
    if shutil.disk_usage(jobdir).free<2*required+1048576+int(config['min_free_disk_mb'])*1048576:raise ResourcePaused('全部方向特征临时矩阵及提交缓存磁盘不足；未缩小规则/数据范围')
    positions={d:0 for d in pending};matrices={};historical_pnl=None
    if historical_objective(config):
        from .historical_pricing import minute_close_payoffs
        historical_pnl=minute_close_payoffs(events,labels,scale)
    with tempfile.TemporaryDirectory(prefix='feature_preparation_',dir=jobdir) as td:
        try:
            for direction,n in counts.items():
                if n:matrices[direction]=np.lib.format.open_memmap(Path(td)/(direction+'.npy'),mode='w+',dtype=np.int64,shape=(n,len(names)))
            stream=StandardFeatureStream(contract=event_contract(events[0]) if events else None,cross_stale_minutes=config.get('cross_stale_minutes'),cross_stale_minutes_prematch=config.get('cross_stale_minutes_prematch'));previous=None
            for number,event in enumerate(events):
                if number%2048==0:
                    if should_pause():raise Paused()
                    update(stage='S1',direction='ALL',message='一次因果扫描生成全部方向特征',feature_events=number,feature_events_total=len(events),raw_events=len(events))
                if previous is not None and event['sid']!=previous:release_match_state(stream,previous)
                previous=event['sid'];snapshots=stream.feed(event);choices=targets(event)
                if not choices:continue
                if snapshots is None:raise ValueError('可评价报价没有因果快照')
                label=labels[event['sid']]
                margin=sum(label['final']) if event['market']==0 else label['final'][0]-label['final'][1]-(event['score'][0]-event['score'][1] if event['phase'] else 0)
                quality=int(bool(event['phase'] and event['score'][0]!=MISSING and any(event['score'][i]>label['final'][i] for i in (0,1))))
                sides={}
                for side in {s for _,s in choices}:
                    values={**snapshots[side],'eid':event['eid'],'mid':event['mid'],'side':side,
                        'pnl':int(historical_pnl[event['eid'],side]) if historical_pnl is not None else settlement(event['line'],event['water'][side],margin,side,scale),'year':label['year'],'ts':event['ts'],'quality':quality}
                    if set(values)-name_set:raise ValueError('全方向缓存遇到未登记特征，拒绝遗漏列')
                    sides[side]=[values.get(name,MISSING) for name in names]
                for direction,side in choices:matrices[direction][positions[direction]]=sides[side];positions[direction]+=1
            if positions!=counts:raise ValueError('全方向特征缓存行数不符')
            for direction in pending:
                if should_pause():raise Paused()
                matrix=matrices.get(direction)
                base={name:matrix[:,i] for i,name in enumerate(names)} if matrix is not None else {'eid':np.array([],np.int64),'mid':np.array([],np.int64)}
                root=jobdir/'mining'/direction;path=root/'arrays.npz';temporary=root/'arrays.npz.tmp'
                try:
                    with temporary.open('wb') as f:np.savez_compressed(f,**base);f.flush();os.fsync(f.fileno())
                    atomic_replace(temporary,path)
                    atomic_json(root/'standard_features.json',{'binding':bindings[direction],'binding_details':details_by_direction[direction],'sha256':sha(path),'method':'single_causal_pass_all_directions_v1','excluded_quote_rows':excluded[direction]})
                finally:
                    if temporary.exists():temporary.unlink()
                del base
                if matrix is not None:close_graph(matrix);matrices.pop(direction)
                update(stage='S1',direction=direction,message='已提交该方向完整特征缓存',feature_events=len(events),feature_events_total=len(events))
        finally:
            for matrix in matrices.values():close_graph(matrix)
    return {'status':'COMPLETE','directions_built':len(pending),'event_passes':1,'direction_rows':counts,'excluded_quote_rows':excluded,'temporary_matrix_bytes':required}
