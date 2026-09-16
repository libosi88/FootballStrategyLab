"""Explicit offline evaluation, original-expression export and JSON paper input."""
from pathlib import Path
import sys,json,time,os,tempfile
from .common import *

def export_candidates(jobdir,output):
    from .standard_atoms import AtomCatalog,verify_frozen_dictionary
    from .standard_search import candidate_families,family_conditions_from
    from .search_integrity import verify_search_evidence,PARTIAL_SEARCH_STATUSES
    from .rules import rule_id
    target=Path(output).resolve()
    if target.exists():raise FileExistsError('候选导出不能覆盖已有文件，请指定新文件名')
    root=Path(jobdir);config=read_json(root/'config.json');audit=read_json(root/'data_audit.json');scale=audit['water_scale']
    def rows():
        for direction in config['directions']:
            folder=root/'mining'/direction;state=read_json(folder/'state.json')
            if not isinstance(state,dict):raise ValueError('候选导出缺少搜索状态: '+direction)
            status=state.get('status');backend=state.get('search_backend','standard_class_dfs')
            if status=='NO_DATA':
                verify_search_evidence(folder,'standard_empty_scope',state.get('binding'))
                continue
            if status=='COMPLETE' and backend in ('standard_class_dfs','standard_global_bound'):
                evidence_backend=backend
            elif status in PARTIAL_SEARCH_STATUSES and backend=='standard_class_dfs':
                evidence_backend='standard_class_dfs_partial'
            else:raise ValueError('候选导出需要已封存的完成或暂停搜索证据: '+direction)
            verify_search_evidence(folder,evidence_backend,state.get('binding'))
            if backend=='standard_global_bound':continue
            if verify_frozen_dictionary(folder) is None:raise ValueError('候选字典缺少冻结证据，拒绝导出')
            emitted=0
            with AtomCatalog(folder/'atoms.sqlite3') as atoms:
                for family in candidate_families(folder,money_threshold(config['min_profit'],2*scale)):
                    for ids in family_conditions_from(family['block'],family['anchor'],family['prefix']):
                        conditions=[atoms[i] for i in ids]
                        emitted+=1
                        yield {'direction':direction,'strategy_id':rule_id(direction,conditions),'module':family['block']['module'],'family_id':family['id'],
                               'atom_ids':list(ids),'conditions':conditions,'n':family['n'],'net_i':family['net'],'pnl_denominator':2*scale,
                               'profit_basis':'historical_minute_close_pregoal_rejected' if historical_objective(config) else 'instant_quote',
                               'count_basis':'first signals; an unfillable or pre-goal-rejected signal has zero payoff and is never replaced' if historical_objective(config) else 'first trades',
                               'state':'PROFITABLE_ORIGINAL_EXPRESSION','future_equivalence_assumed':False}
            if type(state.get('candidates')) is not int or emitted!=state['candidates']:
                raise ValueError('候选导出条数与封存搜索账不一致: '+direction)
            verify_search_evidence(folder,evidence_backend,state.get('binding'))
    target.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix=target.name+'.partial_',suffix='.gz' if str(target).endswith('.gz') else '.tmp',dir=target.parent);os.close(fd);temporary=Path(name)
    try:
        write_jsonl(temporary,rows())
        if os.name=='nt':
            deadline=time.monotonic()+15
            while True:
                try:os.rename(temporary,target);break
                except PermissionError:
                    if time.monotonic()>=deadline:raise
                    time.sleep(.05)
        else:os.link(temporary,target);temporary.unlink()
    finally:
        if temporary.exists():temporary.unlink()
    return {'status':'EXPORTED','output':str(target)}

def evaluate_packet(rules_file,inputs,output):
    from .data import inspect,load_inputs
    from .standard_mining import ordered_standard_events
    from .research_validation import evaluate_frozen
    root=Path(output);root.mkdir(parents=True,exist_ok=True);packet=read_json(rules_file)
    frozen={'packet_sha256':sha(rules_file),'contract':packet['contract'],'evaluation_plan':'fixed_roster_no_reselection_v1'}
    previous=read_json(root/'frozen_before_input.json')
    if previous is not None and previous!=frozen:raise ValueError('此评测已冻结另一份名单，请另建版本')
    atomic_json(root/'frozen_before_input.json',frozen)
    man=inspect(inputs,root/'import_cache');cfg=check_config({'company':packet['contract']['company'],'match_cap':packet['execution_policy']['match_cap'],'stale_minutes':packet['execution_policy']['stale_minutes']})
    events,labels,scale,audit=load_inputs(man,packet['contract']['league'],packet['contract']['company'])
    target=max(scale,packet['water_scale'])
    if target!=scale:
        for e in events:e['water']=[v if v==MISSING else v*(target//scale) for v in e['water']];e['water_scale']=target
    ordering='standard_wall_minute_row' if packet['execution_policy'].get('feature_version')=='v3' else 'legacy_market_phase_order'
    result=evaluate_frozen(packet,ordered_standard_events(events) if ordering=='standard_wall_minute_row' else events,labels,target,cfg)
    result['event_order']=ordering
    result.update(input_manifest=man,data_audit=audit,scope='User-designated evaluation data; independence depends on whether this data was previously used. No automatic live approval.')
    atomic_json(root/'evaluation.json',result);return result

def validate_stream_paths(rules_file,state_file,input_file,output_file):
    protected=[Path(rules_file).resolve()]
    if input_file!='-':protected.append(Path(input_file).resolve())
    targets=[Path(state_file).resolve()]
    if output_file is not None:targets.append(Path(output_file).resolve())
    for target in targets:
        for original in protected:
            if target==original or target.exists() and original.exists() and target.samefile(original):raise ValueError('模拟输入、规则、状态库和输出的可写路径不能重合')
        protected.append(target)

STREAM_MESSAGES={'quote':{'type','event'},'history':{'type','events'},'match_start':{'type','sid'},'watermark':{'type','before','sid'},'match_end':{'type','sid'},'receipt':{'type','intent_id','receipt_id','status','filled_units','confirmed'}}

def stream_message(runner,message):
    if not isinstance(message,dict):raise ValueError('每行必须是JSON对象')
    kind=message.get('type')
    if kind is not None and (kind not in STREAM_MESSAGES or set(message)-STREAM_MESSAGES[kind]):raise ValueError('控制消息含未知类型或字段')
    if kind=='watermark' and type(message.get('before')) is not int:raise ValueError('完整分钟水位线必须为整数时间')
    if kind is None:return runner.feed(message)
    if kind=='quote':return runner.feed(message['event'])
    if kind=='history':return runner.backfill(message['events'])
    if kind=='match_start':runner.declare_new_match(message['sid']);return {'status':'HISTORY_DECLARED_COMPLETE'}
    if kind=='watermark':return {'intents':runner.flush(message['before'],message.get('sid'))}
    if kind=='match_end':return {'intents':runner.finish_match(message['sid'])}
    return runner.dispatcher.receipt(message['intent_id'],message['receipt_id'],message['status'],message.get('filled_units','0'),message.get('confirmed',False))

def paper_stream(rules_file,state_file,input_file,output_file=None,archive_history=False,auto_fill=False,checkpoint_every=1):
    from .paper_runner import StreamingPaperRunner
    validate_stream_paths(rules_file,state_file,input_file,output_file)
    packet=read_json(rules_file);source=sys.stdin if input_file=='-' else open(input_file,encoding='utf-8-sig')
    try:output=sys.stdout if output_file is None else open(output_file,'w',encoding='utf-8')
    except BaseException:
        if source is not sys.stdin:source.close()
        raise
    rejected=0
    try:
        with StreamingPaperRunner(state_file,packet,archive_history,auto_fill,checkpoint_every=checkpoint_every) as runner:
            for number,line in enumerate(source,1):
                if not line.strip():continue
                try:result=stream_message(runner,json.loads(line))
                except (ValueError,TypeError,KeyError,AttributeError) as error:
                    # Late, sealed, malformed or illegal messages are rejected and recorded; the
                    # stream continues. A persistence fault leaves the coordinator unusable and stops.
                    if runner._faulted or runner._closed:raise
                    rejected+=1;result={'status':'MESSAGE_REJECTED','line':number,'error':str(error)}
                    runner._persist(runner.dispatcher.alarm,'stream_message_rejected',{'line':number,'error':str(error)})
                output.write(canonical(result)+'\n');output.flush()
            if archive_history:output.write(canonical({'intents':runner.flush()})+'\n');output.flush()
    finally:
        if source is not sys.stdin:source.close()
        if output is not sys.stdout:output.close()
    return {'status':'PAPER_ONLY','state_database':str(Path(state_file).resolve()),'real_orders_sent':0,'rejected_messages':rejected}
