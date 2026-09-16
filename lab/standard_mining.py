"""Complete v3 dictionary -> external masks -> exact recoverable family search."""
from pathlib import Path
import os
import numpy as np, shutil
from .common import read_json,atomic_json,sha,MISSING,side_for
from .mining import build_direction,Paused
from .standard_atoms import build_standard_catalog,AtomCatalog
from .standard_columns import StandardColumns
from .standard_masks import MaskStore
from .standard_search import run_class_search

def mine_standard(jobdir,events,labels,scale,direction,config,update,should_pause,prepare_only=False):
    root=Path(jobdir)/'mining'/direction;root.mkdir(parents=True,exist_ok=True)
    update(direction=direction,stage='S1' if prepare_only else 'S2',message='冻结当前方向的标准字典' if prepare_only else '执行当前方向的标准搜索')
    from .standard_feature_cache import direction_identity,check_direction_cache
    binding,binding_details=direction_identity(jobdir,events,labels,scale,direction,config)
    from .search_integrity import verify_stopped_search
    stopped=verify_stopped_search(root,binding)
    if stopped and stopped.get('status') in ('COMPLETE','BUDGET_STOP','PAUSED','RESOURCE_BLOCKED','INTERRUPTED') and stopped.get('search_backend')!='standard_empty_scope':
        from .standard_atoms import verify_frozen_dictionary
        if verify_frozen_dictionary(root) is None:
            raise ValueError('已搜索的非空方向缺少字典封存证据，拒绝重新生成或复用')
    prepared=root/'standard_features.json';cache=root/'arrays.npz';meta=read_json(prepared)
    if meta:
        check_direction_cache(meta,binding,binding_details,cache)
        # A finished direction is already its own committed answer, and state.json is exactly
        # what run_class_search publishes on completion. Re-entering the mask phase would
        # rebuild a deterministic cache the checkpoint exists to avoid. Both frozen artefacts
        # are verified first, so this shortcut can never trust a tampered direction.
        finished=None if prepare_only else read_json(root/'state.json')
        if finished and finished.get('status') in ('COMPLETE','NO_DATA') and finished.get('binding',binding)==binding:
            from .standard_atoms import verify_frozen_dictionary
            verify_frozen_dictionary(root)
            if finished.get('status') in ('COMPLETE','NO_DATA'):
                from .search_integrity import verify_search_evidence
                verify_search_evidence(root,finished['search_backend'],binding)
            update(direction=direction,message='该方向已完成且冻结产物校验通过，直接复用已提交搜索账')
            return finished
        with np.load(cache,allow_pickle=False) as z:base={k:z[k] for k in z.files}
    else:
        update(direction=direction,message='建立v3完整因果基准、角色、跨市场和赛前摘要',stage='S1-S2')
        base=build_direction(events,labels,scale,direction,config)
        np.savez_compressed(cache,**base);atomic_json(prepared,{'binding':binding,'binding_details':binding_details,'sha256':sha(cache)})
    if should_pause():raise Paused()
    if not len(base['eid']):
        with AtomCatalog(root/'atoms.sqlite3',True) as atoms:atoms.commit()
        atomic_json(root/'dictionary.json',{'backend':'standard','atoms_file':'atoms.sqlite3','atom_count':0,'direction':direction})
        phase=0 if direction.startswith('PRE') else 1;market=0 if direction.endswith(('OVER','UNDER')) else 1
        # An empty evaluable subset does not prove missing source/settlement data
        # contained no opportunities. Explicit closures and known inapplicable
        # lines are the only zero-opportunity cases independent of those fields.
        from .research_standard import trigger_window_allows
        counts=dict(source_rows=0,closed_rows=0,nonapplicable_rows=0,
                    invalid_open_rows=0,outside_trigger_window_rows=0,ineligible_result_rows=0,
                    missing_live_score_rows=0,evaluable_rows=0)
        for e in events:
            if (e['phase'],e['market'])!=(phase,market):continue
            counts['source_rows']+=1
            if e['closed']:
                counts['closed_rows']+=1;continue
            if e['line']==MISSING:
                counts['invalid_open_rows']+=1;continue
            if side_for(direction,e['line']) is None:
                counts['nonapplicable_rows']+=1;continue
            if not e['valid']:
                counts['invalid_open_rows']+=1;continue
            # A declared trigger-window exclusion is policy, not a data gap.
            if not trigger_window_allows(e,config):
                counts['outside_trigger_window_rows']+=1;continue
            eligible=labels[e['sid']]['eligible']
            missing_score=bool(phase and market and MISSING in e['score'])
            if not eligible:counts['ineligible_result_rows']+=1
            if missing_score:counts['missing_live_score_rows']+=1
            if eligible and not missing_score:counts['evaluable_rows']+=1
        if counts['evaluable_rows']:
            raise ValueError('存在方向适用且结算字段完整的有效报价，但标准特征数组为空')
        reasons=[]
        if not counts['source_rows']:reasons.append('没有该市场/阶段的源报价')
        if counts['invalid_open_rows']:reasons.append('开放报价缺少合法盘口/水位，无法判定全部机会')
        if counts['ineligible_result_rows']:reasons.append('方向适用的有效报价没有合格赛果，无法评价收益')
        if counts['missing_live_score_rows']:reasons.append('滚球让球缺少当时比分，无法确定剩余让球结算基准')
        blocked={'S0':'；'.join(reasons)} if reasons else {}
        reason=('当前可评价子集为空，但S0数据或结算语义阻塞；不能据此判断完整方向零命中或没有盈利策略'
                if blocked else '该方向源报价均为明确封盘或已知盘口不适用；可执行基线精确为n=0,pnl=0')
        state={'status':'NO_DATA','next':1,'total':1,'raw_total':1,'remaining':0,'representatives':0,'equivalent_occurrences':0,'proven_zero':1,'proven_nonprofitable':0,'candidates':0,'search_backend':'standard_class_dfs','standard_scope_complete':not blocked,'blocked':blocked,'reason':reason,
               'empty_scope_counts':counts,'empty_scope_counting':'赛果不合格与缺当时比分计数可以重叠；其他排除项按源行依次分类',
               'zero_proof_scope':'仅冻结后可评价报价子集的M00基线；S0阻塞时不证明完整源方向零命中'}
        state.update(binding=binding,search_backend='standard_empty_scope')
        atomic_json(root/'state.json',state)
        from .search_integrity import seal_search_evidence
        seal_search_evidence(root,'standard_empty_scope',binding)
        return state
    with StandardColumns(base,events,direction,root/'virtual_columns',config.get('max_feature_cache_mb',128)) as columns:
        atoms,info=build_standard_catalog(root,columns,direction,scale,config,update,should_pause)
        try:
            dictionary_meta={**info,'backend':'standard','direction':direction,'atoms_sha256':sha(atoms.path)}
            atomic_json(root/'dictionary.json',dictionary_meta)
            from .handoff_assets import write_feature_registry
            write_feature_registry(root/'features.json',(r[0] for r in atoms.conn.execute('SELECT DISTINCT feature FROM atoms')),scale,'all frozen dictionary features before search')
            from .standard_atoms import W,C,T,X,P
            from math import comb
            core=atoms.ids(C);context=[i for i in core if not atoms[i]['feature'].startswith(('path','linepath'))];c=len(core);ctx=len(context)
            choose=lambda n,k:comb(n,k) if n>=k else 0
            from .standard_spec import max_conditions_for,grammar_of,modules_for
            counts={'M00':1,'M01':len(atoms.ids(W)),'M02':choose(c,2),'M03':choose(c,3) if max_conditions_for(config)>=3 else 0,
                    'M04':len(atoms.ids(T))*(1+ctx+choose(ctx,2)),
                    'M05':len(set(atoms.ids(W))-set(core))*(ctx+choose(ctx,2)),
                    'M06':len(atoms.ids(X))*(1+c+choose(c,2)),'M07':len(atoms.ids(P))*(1+c+choose(c,2))}
            frozen={'spec_version':info['spec_version'],'grammar':grammar_of(config),'max_conditions':max_conditions_for(config),'module_descriptions':modules_for(config),
                    'binding':binding,'direction':direction,'modules':counts,'total':sum(counts.values()),'blocked':info['blocked'],'dictionary_sha256':dictionary_meta['atoms_sha256']}
            atomic_json(root/'search_spec.json',frozen)
            if prepare_only:return frozen
            if should_pause():raise Paused()
            from .global_bound import prove_global_bound
            global_proof=prove_global_bound(root,base,atoms,config,scale,binding,info['blocked'])
            if global_proof is not None:
                if global_proof['raw_total']!=frozen['total']:raise ValueError('全域收益上界证明的表达分母不一致')
                update(direction=direction,message='全部事件逐场最大可得收益仍不过门槛，完整冻结表达由安全上界覆盖',evaluated=frozen['total'],total_rules=frozen['total'])
                return global_proof
            maximum=max(abs(int(base['pnl'].min())),abs(int(base['pnl'].max())))
            if len(set(base['mid'].tolist()))*maximum>2**63-1:raise OverflowError('标准收益累计超出int64范围')
            with MaskStore(root,columns,atoms,config,binding) as masks:
                stats=masks.prepare(update,should_pause);atomic_json(root/'mask_classes_summary.json',stats)
                result=run_class_search(root,atoms,masks,{**config,'_water_scale':scale},update,should_pause,binding,info['blocked'])
                if result['raw_total']!=frozen['total']:raise ValueError('事件归组后的表达映射与事先冻结分母不一致')
                return result
        finally:atoms.close()

# Deterministic functions of arrays.npz + atoms.sqlite3 under the same binding.
# Everything the frozen evidence, the review stage and the audit package read
# (arrays.npz, atoms.sqlite3, search.sqlite3, standard_plan.json, state.json,
# dictionary.json, search_spec.json, global_*.json) is never a member here.
DERIVED_CACHES=('masks.sqlite3','virtual_columns','review_columns')
EVIDENCE_NEVER_RELEASED=('arrays.npz','atoms.sqlite3','search.sqlite3','standard_plan.json',
                         'search_evidence.json',
                         'state.json','dictionary.json','dictionary_complete.json','search_spec.json',
                         'standard_features.json','features.json','mask_classes_summary.json',
                         'global_bound_plan.json','global_upper_proof.json')


def _entry_bytes(path):
    if path.is_dir():return sum(f.stat().st_size for f in path.rglob('*') if f.is_file())
    return path.stat().st_size

def cache_entries(root,names=DERIVED_CACHES):
    root=Path(root).resolve();entries=[]
    for name in names:
        if name in EVIDENCE_NEVER_RELEASED:raise ValueError('拒绝释放冻结证据: '+name)
        if name not in DERIVED_CACHES:raise ValueError('只允许释放明确登记的可重建缓存')
        candidates=[root/name]
        if name=='masks.sqlite3':candidates.extend(root/(name+suffix) for suffix in ('-wal','-shm','-journal'))
        for path in candidates:
            if path.is_symlink() or getattr(path,'is_junction',lambda:False)():raise ValueError('缓存路径是链接，拒绝清理')
            if not path.exists():continue
            if not path.resolve().is_relative_to(root):raise ValueError('缓存路径越界')
            if path.is_dir():
                for directory,dirs,files in os.walk(path,followlinks=False):
                    for child in (*dirs,*files):
                        p=Path(directory)/child
                        if p.is_symlink() or getattr(p,'is_junction',lambda:False)() or not p.resolve().is_relative_to(root):raise ValueError('缓存含链接或越界内容，拒绝清理')
            entries.append(path)
    return entries


def release_direction_caches(root,state,config,update=None,names=DERIVED_CACHES):
    """Drop rebuildable caches once a direction is finished; keep every resume path intact.

    An unfinished direction keeps everything: rebuilding a paused search's masks is
    precisely the work its checkpoint exists to avoid. Release is recorded, not silent,
    and the record names the call that reconstructs each entry.
    """
    root=Path(root);status=(state or {}).get('status')
    if not config.get('release_direction_caches',True):
        return {'released':[],'freed_bytes':0,'reason':'保留策略由配置关闭'}
    if status not in ('COMPLETE','NO_DATA'):
        return {'released':[],'freed_bytes':0,'reason':f'方向状态{status}尚可续跑，缓存保留'}
    released=[];freed=0
    for path in cache_entries(root,names):
            if not path.exists():continue
            size=_entry_bytes(path)
            try:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
            except OSError as error:
                released.append({'entry':path.name,'status':'RETAINED','error':str(error)});continue
            released.append({'entry':path.name,'status':'RELEASED','bytes':size});freed+=size
    record={'direction_status':status,'released':released,'freed_bytes':freed,
            'rebuild':'lab.standard_mining.mine_standard 在相同binding下由 arrays.npz + atoms.sqlite3 确定性重建全部已释放条目',
            'never_released':list(EVIDENCE_NEVER_RELEASED)}
    atomic_json(root/'cache_release.json',record)
    if update and freed:update(message=f'该方向已完成，释放可重建缓存 {freed/1048576:.0f} MiB')
    return record


def ordered_standard_events(events):
    """Market ties use no same-minute cross-market information."""
    ordered=sorted(events,key=lambda e:(e['mid'],e['ts'],e['row'],e['market'],e['phase']))
    return [{**e,'eid':i} for i,e in enumerate(ordered)]
