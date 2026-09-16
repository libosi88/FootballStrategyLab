"""Machine-readable features, event schema and executable per-rule cases."""
from pathlib import Path
from .common import *
from .contracts import EVENT_FIELDS
from .reporting import feature_note
from .rules import unit,matches


def feature_record(name,scale):
    return {'feature':name,'unit':unit(name,scale),'definition':feature_note(name),
            'missing_value':MISSING,'missing_comparison_result':False,
            'implementation':'lab/standard_features.py' if name.startswith(('cross_','pre_','role_water_','stoppage_')) else 'lab/features.py + lab/standard_features.py',
            'terminal_labels_allowed':False}


def write_feature_registry(path,names,scale,scope):
    atomic_json(path,{'schema':'FSL_FEATURE_DICTIONARY_V1','scope':scope,'water_scale':scale,
        'features':[feature_record(n,scale) for n in sorted(set(names))],
        'event_order':'validate current quote; derive declared prior/current baselines; evaluate snapshot; persist next state. Invalid quotes reset reset-paths and keep closed cross-market state.',
        'path_semantics':{'projected':'one quote -> line-first token 1/2, otherwise water 3/4',
            'composite':'one quote -> one token with both attributes (13/14/23/24); never two ordered steps',
            'subsequence':'intermediate tokens allowed; selected final step must be latest token',
            'state':'persist on unchanged quote; span measures first to last path event',
            'pulse':'only the quote that creates the last step'},'label_fields_allowed':False})


def event_schema(contract):
    props={k:{'type':'integer'} for k in ('eid','mid','row','ts','market','phase','minute','line')}
    props.update({k:{'type':'string'} for k in ('sid','source','event_key','minute_raw','status')})
    props.update({k:{'type':'boolean'} for k in ('valid','closed','quality_blocked')})
    props.update({k:{'type':'array','items':{'type':'integer'},'minItems':2,'maxItems':2} for k in ('score','water')})
    props.update({k:{'const':v} for k,v in contract.items() if k!='schema'})
    props['market']['enum']=[0,1];props['phase']['enum']=[0,1];props['status']['enum']=['早','即','滚']
    for k in ('eid','mid','row'):props[k]['minimum']=0
    for k in ('sid','source','event_key'):props[k]['minLength']=1
    return {'$schema':'https://json-schema.org/draft/2020-12/schema','title':'FSL standard quote event',
        'type':'object','additionalProperties':False,'required':sorted(EVENT_FIELDS-{'quality_blocked'}),'properties':props,
        '$comment':'Also call contracts.validate_event: phase/status, valid/closed/price, paired missing scores and integer bounds are semantic constraints.'}


def scalar_cases(atom):
    f=atom['feature'];v=atom['value'];out=[]
    if f.startswith(('path','linepath')):return out
    if atom['op']=='eq':values=[v-1,v,v+1]
    elif atom['op'] in ('ge','le'):values=[v-1,v,v+1]
    else:values=[v-1,v,atom['upper']-1,atom['upper']]
    if f=='minute':values.append(-2)
    for value in [*dict.fromkeys(values),MISSING]:
        numeric_only=f=='minute' and atom['op']!='eq' and value<0
        expected=False if value==MISSING or numeric_only else (value==v if atom['op']=='eq' else value>=v if atom['op']=='ge' else value<=v if atom['op']=='le' else v<=value<atom['upper'])
        out.append({'kind':'scalar_atom_boundary','condition':atom,'feature_value':value,'expected':expected})
    return out


def integration_manifest(folder,packet):
    return {
        'schema':'FSL_LEAGUE_TRIGGER_HANDOFF_V1','league':packet['contract'].get('league'),'company':packet['contract'].get('company'),
        'roster_hash':packet.get('roster_hash'),'registry_key':digest([packet['contract'],packet.get('roster_hash')]),
        'research_objective':packet['execution_policy'].get('research_objective','validation'),
        'runtime':'Python 3.11-3.13','trigger_entrypoint':'lab.features.SignalEngine','stream_entrypoint':'app.py paper-stream',
        'input_schema':'event.schema.json','rules':'rules.json','execution_policy':'execution_config.json',
        'binding_files':{name:sha(Path(folder)/name) for name in ('rules.json','event.schema.json','execution_config.json','boundary_cases.json')},
        'target_project':'复盘系统','target_adapter_status':'NOT_INTEGRATED',
        'adapter_contract':'JSONL bridge or an independently replay-verified JavaScript equivalent; preserve league/company, integer units, first-signal order and core-slot limits',
        'legacy_strategies_replaced':False,'real_orders_sent':0}

def write_assets(root):
    root=Path(root)
    for folder in [root/'results',root/'results/lower_risk']:
        packet=read_json(folder/'rules.json')
        if packet is None:continue
        write_feature_registry(folder/'features.json',(a['feature'] for r in packet['rules'] for a in r['conditions']),packet['water_scale'],'all features referenced by this frozen roster')
        atomic_json(folder/'event.schema.json',event_schema(packet['contract']))
        atomic_json(folder/'execution_config.json',packet['execution_policy'])
        atomic_json(folder/'boundary_cases.json',boundary_packet(packet))
        atomic_json(folder/'integration_manifest.json',integration_manifest(folder,packet))

def path_cases(atom):
    if not atom['feature'].startswith(('path','linepath')):return []
    pattern=atom.get('pattern',[int(v) for v in str(atom['value'])]);k=len(pattern)
    def history(line_step=None,water_step=None,span=None):
        line=0;water=10**12;rows=[{'line':line,'water':[water,water],'ts':100,'event_key':'initial'}]
        for i,code in enumerate(pattern):
            attrs={code} if code<10 else {code//10,code%10}
            dl=atom.get('min_line_step',1) if line_step is None else line_step
            dw=atom.get('min_water_step',1) if water_step is None else water_step
            if 1 in attrs:line+=dl
            if 2 in attrs:line-=dl
            if 3 in attrs:water+=dw
            if 4 in attrs:water-=dw
            rows.append({'line':line,'water':[water,10**12],'ts':100+(span if i==k-1 and k>1 else 0),'event_key':str(i)})
        return rows
    positive=history(span=atom.get('span',0))
    cases=[{'kind':'path_exact_boundary','condition':atom,'history':positive,'current_key':positive[-1]['event_key'],'expected':True},
           {'kind':'path_missing_step','condition':atom,'history':positive[:-1],'current_key':positive[-2]['event_key'],'expected':False}]
    attributes=set().union(*({code} if code<10 else {code//10,code%10} for code in pattern))
    for key,override,codes in (('min_line_step','line_step',{1,2}),('min_water_step','water_step',{3,4})):
        if atom.get(key,1)>1 and attributes & codes:
            rows=history(**{override:atom[key]-1},span=atom.get('span',0))
            cases.append({'kind':key+'_below','condition':atom,'history':rows,'current_key':rows[-1]['event_key'],'expected':False})
    if 'span' in atom and k>1:
        rows=history(span=atom['span']+1)
        cases.append({'kind':'span_above','condition':atom,'history':rows,'current_key':rows[-1]['event_key'],'expected':False})
    unchanged=[*positive,{**positive[-1],'event_key':'unchanged'}]
    cases.append({'kind':'path_unchanged_pulse_or_state','condition':atom,'history':unchanged,'current_key':'unchanged','expected':not atom.get('pulse',False)})
    return cases

def boundary_packet(packet):
    return {'schema':'FSL_RULE_BOUNDARIES_V2','roster_hash':packet['roster_hash'],
        'rules':[{'strategy_id':r['id'],'atom_boundaries':[c for a in r['conditions'] for c in scalar_cases(a)],
                  'path_boundaries':[c for a in r['conditions'] for c in path_cases(a)],
                  'positive_and_closed_negative':'rule_replay_cases.jsonl.gz; actual historical prefix'} for r in packet['rules']],
        'scope':'complete current-roster scalar and path boundaries supplement real full-engine replay'}


def verify_scalar_cases(folder):
    folder=Path(folder);cases=read_json(folder/'boundary_cases.json');count=paths=0
    if cases is None:raise ValueError('缺少逐条边界样例')
    packet=read_json(folder/'rules.json')
    if not packet or cases!=boundary_packet(packet):raise ValueError('边界样例覆盖、内容或名单绑定不一致，拒绝空或过期验收')
    if read_json(folder/'event.schema.json')!=event_schema(packet['contract']):raise ValueError('交接事件Schema缺失或与冻结契约不一致')
    if read_json(folder/'execution_config.json')!=packet['execution_policy']:raise ValueError('交接执行配置缺失或与冻结政策不一致')
    if read_json(folder/'integration_manifest.json')!=integration_manifest(folder,packet):raise ValueError('交接接入清单缺失或绑定内容已改变')
    for rule in cases['rules']:
        for case in rule['atom_boundaries']:
            if matches(case['condition'],{case['condition']['feature']:case['feature_value']}) is not case['expected']:raise AssertionError('原子临界边界不一致: '+rule['strategy_id'])
            count+=1
        from .standard_features import path_matches
        for case in rule['path_boundaries']:
            if path_matches(case['history'],0,case['condition'],case['current_key']) is not case['expected']:raise AssertionError('路径临界边界不一致: '+rule['strategy_id'])
            paths+=1
    return {'status':'PASS','scalar_boundaries':count,'path_boundaries':paths}
