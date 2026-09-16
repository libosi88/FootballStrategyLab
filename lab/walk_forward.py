"""Real rolling rediscovery and separately frozen evaluation, grouped by match.

Each training fold rereads only its match rows before choosing numeric precision
and feature domains. The full-sample winners are never reused as fold winners.
"""
from datetime import date
import time
from .common import *
from .data import load_inputs
from .store import Store
from .standard_mining import ordered_standard_events
from .research_validation import evaluate_frozen

def split_matches(labels,cutoff,test_end):
    date.fromisoformat(cutoff);date.fromisoformat(test_end)
    if cutoff>=test_end:raise ValueError('滚动测试结束日期必须晚于训练截止日期')
    train={sid for sid,l in labels.items() if l['eligible'] and l['date']<=cutoff}
    test={sid for sid,l in labels.items() if l['eligible'] and cutoff<l['date']<=test_end}
    if train&test:raise ValueError('同场比赛跨入训练和测试')
    return train,test

def prepare_training_job(store,parent,labels,cutoff,test_end):
    train,test=split_matches(labels,cutoff,test_end)
    if not train or not test:return None,train,test
    config={**parent['config'],'research_partition':{'kind':'walk_forward_training','cutoff':cutoff,'train_match_ids_sha256':digest(sorted(train))}}
    config.pop('engine_hash',None);config.pop('created_version',None)
    jid=store.create(parent['league'],config,parent['manifest'],start_paused=True);root=store.root/jid
    events,training_labels,scale,audit=load_inputs(parent['manifest'],parent['league'],config['company'],included_sids=train)
    if config['profile']=='standard':events=ordered_standard_events(events)
    for label in training_labels.values():label['league']=parent['league']
    # Research segments are anchored at this fold's own last training match; test dates never shape them.
    from .research_standard import assign_segments
    assign_segments(training_labels)
    audit['partition']={'kind':'walk_forward_training','cutoff':cutoff,'train_matches':len(train),'test_matches_in_training':0,'precision_from_training_rows_only':True}
    write_jsonl(root/'events.jsonl.gz',events);atomic_json(root/'labels.json',training_labels);atomic_json(root/'data_audit.json',audit)
    atomic_json(root/'prepared_manifest.json',{p:sha(root/p) for p in ('events.jsonl.gz','labels.json','data_audit.json')})
    return jid,train,test

def run_walk_forward(workspace,source_job,folds,update=None,wait_seconds=0):
    from .pipeline import run
    if type(wait_seconds) is not int or wait_seconds<0:raise ValueError('等待秒数必须为非负整数；0表示等到该折训练结束')
    update=update or (lambda **kw:None);store=Store(workspace);parent=store.get(source_job)
    labels=read_json(store.root/source_job/'labels.json')
    if labels is None:raise ValueError('原任务尚未完成输入核对')
    prepared=read_json(store.root/source_job/'prepared_manifest.json',{})
    labels_hash=sha(store.root/source_job/'labels.json')
    if labels_hash!=prepared.get('labels.json'):raise ValueError('原任务标签与预处理校验记录不符，拒绝重新切分')
    spec={'source_job':source_job,'source_config':parent['config'],'source_manifest':parent['manifest'],'labels_sha256':labels_hash,'folds':folds,'method':'rediscover_then_freeze_then_evaluate_v2'}
    root=store.root/source_job/'walk_forward'/digest(spec)[:16];root.mkdir(parents=True,exist_ok=True)
    old=read_json(root/'plan.json')
    if old is not None and old!=spec:raise ValueError('滚动方案变化，必须另建版本')
    atomic_json(root/'plan.json',spec);results=[]
    for index,fold in enumerate(folds):
        cutoff,test_end=fold['cutoff'],fold['test_end'];foldroot=root/f'fold_{index:03d}';foldroot.mkdir(exist_ok=True)
        saved=read_json(foldroot/'state.json')
        train,test=split_matches(labels,cutoff,test_end)
        split={'train_match_ids_sha256':digest(sorted(train)),'test_match_ids_sha256':digest(sorted(test))}
        if saved and any(saved.get(k)!=v for k,v in split.items()):raise ValueError('恢复时训练/测试比赛集合改变')
        if saved and saved['status'] in ('EVALUATED','EVALUATED_PARTIAL_TRAINING_SEARCH'):results.append(saved);continue
        if saved and saved['status']=='NOT_APPLICABLE':results.append(saved);continue
        if saved:
            jid=saved['training_job'];train,test=split_matches(labels,cutoff,test_end)
        else:
            jid,train,test=prepare_training_job(store,parent,labels,cutoff,test_end)
            if jid is None:
                result={'status':'NOT_APPLICABLE',**split,'cutoff':cutoff,'test_end':test_end,'train_matches':len(train),'test_matches':len(test)}
                atomic_json(foldroot/'state.json',result);results.append(result);continue
            saved={'status':'TRAINING',**split,'training_job':jid,'cutoff':cutoff,'test_end':test_end,'train_matches':len(train),'test_matches':len(test)};atomic_json(foldroot/'state.json',saved)
        update(message='滚动截点重新发现、入池、筛选、确定N和优先级',cutoff=cutoff,training_job=jid)
        job=store.get(jid)
        if job['config'].get('research_partition',{}).get('train_match_ids_sha256')!=split['train_match_ids_sha256']:raise ValueError('训练任务的冻结比赛集合不符')
        if job['status'] in ('PAUSED','INTERRUPTED','ERROR'):store.control(jid,'resume')
        wait_started=time.monotonic()
        while store.get(jid)['status'] in ('QUEUED','RUNNING','PAUSING'):
            if store.get(jid)['status']=='QUEUED':run(workspace,jid)
            store.recover();current=store.get(jid)
            if current['status'] not in ('QUEUED','RUNNING','PAUSING'):break
            if wait_seconds and time.monotonic()-wait_started>=wait_seconds:
                update(message='工作区仍被占用或训练尚未结束；已保存进度，可稍后继续',training_job=jid,status=current['status']);break
            update(message='等待该截点的完整重新发现与筛选',training_job=jid,status=current['status'],progress=current['progress']);time.sleep(1)
        summary=read_json(store.root/jid/'summary.json')
        if summary is None or store.get(jid)['status'] not in ('DONE','PARTIAL_RESULT') or not (store.root/jid/'results/rules.json').is_file():
            saved['status']='PARTIAL_TRAINING';atomic_json(foldroot/'state.json',saved);results.append(saved);break
        # A finite-budget standard fold is still evaluated, but labelled as partial training search.
        training_complete=bool(summary['coverage'].get('local_search_complete')) and summary['selection_status']=='FINITE_LOCAL_SEARCH_COMPLETE' and (parent['config']['profile']!='standard' or bool(summary['coverage'].get('standard_search_complete')))
        packet=read_json(store.root/jid/'results/rules.json')
        frozen={'training_job':jid,'rules_sha256':sha(store.root/jid/'results/rules.json'),'packet_sha256':digest(packet),'test_match_ids_sha256':digest(sorted(test)),
                'cutoff':cutoff,'test_end':test_end,'reselection_allowed':False,'labels_sha256':labels_hash,'train_match_ids_sha256':split['train_match_ids_sha256'],'event_order':'standard_wall_minute_row' if parent['config']['profile']=='standard' else 'legacy_market_phase_order'}
        previous=read_json(foldroot/'frozen_before_test.json')
        if previous is not None and previous!=frozen:raise ValueError('已打开测试集后训练名单改变，不能复用此次评测')
        atomic_json(foldroot/'frozen_before_test.json',frozen)
        test_events,test_labels,test_scale,audit=load_inputs(parent['manifest'],parent['league'],parent['config']['company'],included_sids=test)
        target_scale=max(packet['water_scale'],test_scale)
        if target_scale!=test_scale:
            factor=target_scale//test_scale
            for e in test_events:e['water']=[v if v==MISSING else v*factor for v in e['water']];e['water_scale']=target_scale
        if parent['config']['profile']=='standard':test_events=ordered_standard_events(test_events)
        report=evaluate_frozen(packet,test_events,test_labels,target_scale,parent['config'])
        saved={**saved,'status':'EVALUATED' if training_complete else 'EVALUATED_PARTIAL_TRAINING_SEARCH','training_search_complete':training_complete,'frozen':frozen,'evaluation':report,'evaluation_is_historical_rolling_not_genuinely_future':True}
        atomic_json(foldroot/'state.json',saved);results.append(saved)
    final={'status':'COMPLETE' if len(results)==len(folds) and all(r['status'] in ('EVALUATED','NOT_APPLICABLE') for r in results) else 'PARTIAL',
           'method':spec['method'],'folds':results,'whole_match_splitting':True,'training_precision_and_domains_use_training_rows_only':True,
           'scope':'Retrospective rolling diagnostics on the current corrected archive. Each fold performs fresh discovery/selection. This does not reconstruct historical data revisions or certify future execution.'}
    atomic_json(root/'summary.json',final);return final
