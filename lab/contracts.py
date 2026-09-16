"""Strict boundary for event streams, exported rules and persisted signal state.

This is a content/integrity contract, not authentication of external market data.
Legacy files must be explicitly adapted; no silent unit conversion or old-state reuse.
"""
from copy import deepcopy
from .common import SEMANTICS, MISSING, digest

CONTRACT_SCHEMA = 'FSL_contract_v2'
STATE_SCHEMA = 'FSL_signal_state_v5'
RULE_SCHEMA = 'FSL_rule_v2'
PERIOD = 'REGULATION_FULL_TIME'
PROVIDER = 'titan007_archive'

LABEL_FIELDS = {'final', 'ft_h', 'ft_a', 'final_score', 'half_time_score', 'half_time',
                '全场比分', '半场比分', 'result_status', '比赛状态', 'finished', 'is_finished'}
CONTRACT_FIELDS = ('schema', 'provider', 'league', 'company', 'market_period',
                   'water_scale', 'line_scale', 'semantics')
EVENT_FIELDS = {'sid','mid','eid','market','phase','ts','source','row','minute','minute_raw',
                'status','score','closed','line','water','valid','quality_blocked','event_key','provider',
                'league','company','market_period','water_scale','line_scale','semantics'}


def make_contract(league, company='皇冠', water_scale=100, provider=PROVIDER):
    return validate_contract(dict(schema=CONTRACT_SCHEMA, provider=provider, league=league,
                                  company=company, market_period=PERIOD, water_scale=water_scale,
                                  line_scale=4, semantics=SEMANTICS))


def validate_contract(c):
    if not isinstance(c, dict) or set(c) != set(CONTRACT_FIELDS):
        raise ValueError('缺少或未知的规则/事件契约字段')
    if c['schema'] != CONTRACT_SCHEMA or c['semantics'] != SEMANTICS:
        raise ValueError('规则/事件语义版本不兼容')
    if c['market_period'] != PERIOD or c['line_scale'] != 4:
        raise ValueError('本版仅支持已声明常规全场/剩余让球与1/4盘口单位')
    if type(c['water_scale']) is not int or c['water_scale'] not in (100,1000,10000,100000,1000000):
        raise ValueError('水位必须声明受支持的十进制整数刻度')
    for k in ('provider','league','company'):
        if not isinstance(c[k], str) or not c[k].strip():
            raise ValueError('契约范围字段缺失: '+k)
    return deepcopy(c)


def bind_event(e, c):
    """Explicit source adapter only: do not use to relabel arbitrary external feeds."""
    c = validate_contract(c)
    additions = {k:c[k] for k in CONTRACT_FIELDS if k != 'schema'}
    for k, v in additions.items():
        if k in e and e[k] != v:
            raise ValueError('输入已有范围/单位与适配契约冲突: '+k)
    out = {**e, **additions}
    validate_event(out, c)
    return out


def event_contract(e):
    return validate_contract({'schema':CONTRACT_SCHEMA, **{k:e.get(k) for k in CONTRACT_FIELDS if k!='schema'}})


def validate_event(e, c=None):
    if not isinstance(e, dict):
        raise ValueError('报价事件必须为对象')
    if LABEL_FIELDS.intersection(e):
        raise ValueError('触发器拒绝终场/半场终值/完赛标签字段')
    unknown = set(e)-EVENT_FIELDS
    if unknown:
        raise ValueError('未登记的事件字段: '+','.join(sorted(unknown)))
    if c is None:
        c = event_contract(e)
    for k in CONTRACT_FIELDS:
        if k != 'schema' and e.get(k) != c[k]:
            raise ValueError('报价项目/单位/语义与规则不一致: '+k)
    for k in ('eid','mid','row','ts','market','phase','minute','line'):
        if type(e.get(k)) is not int:
            raise ValueError('事件整数字段无效: '+k)
    if e['eid'] < 0 or e['mid'] < 0 or e['row'] < 0 or e['ts'] == MISSING:
        raise ValueError('事件序号或时间无效')
    if e['market'] not in (0,1) or e['phase'] not in (0,1):
        raise ValueError('不支持的市场/阶段')
    if {'早':0,'即':0,'滚':1}.get(e.get('status')) != e['phase']:
        raise ValueError('源报价状态与阶段冲突')
    for k in ('sid','source','event_key','minute_raw'):
        if not isinstance(e.get(k),str) or (k!='minute_raw' and not e[k]):
            raise ValueError('事件身份字段无效: '+k)
    if type(e.get('valid')) is not bool or type(e.get('closed')) is not bool:
        raise ValueError('有效/封盘状态必须为布尔值')
    if 'quality_blocked' in e and type(e['quality_blocked']) is not bool:
        raise ValueError('报价质量阻塞标记必须为布尔值')
    for k in ('water','score'):
        if not isinstance(e.get(k),list) or len(e[k]) != 2 or any(type(v) is not int for v in e[k]):
            raise ValueError('报价/比分整数数组无效: '+k)
    if (e['score'][0] == MISSING) != (e['score'][1] == MISSING):
        raise ValueError('比分必须同时完整或同时缺失')
    if any(v<0 and v!=MISSING for v in e['score']):
        raise ValueError('比分不得为负')
    expected = not e['closed'] and e['line']!=MISSING and min(e['water'])>0 and (e['market']==1 or e['line']>=0) and not e.get('quality_blocked',False)
    if e['valid'] != expected:
        raise ValueError('valid与封盘/盘口/水位不一致')
    if max((abs(v) for v in [e['line'], *e['water']] if v!=MISSING),default=0)>10**12:
        raise ValueError('报价超出已声明整数范围')
    return c


def semantic_rule(r):
    """Only executable semantics, not labels or historical metrics."""
    return {'id':r['id'], 'direction':r['direction'], 'semantics':r.get('semantics', SEMANTICS),
            'conditions':[{k:v for k,v in a.items() if k!='label'} for a in r['conditions']],
            'priority':r.get('priority'), 'version':r.get('version'),
            'entry_policy':r.get('entry_policy','FIRST_VALID_MATCH_EVENT'),
            'live_enabled':r.get('live_enabled',False),
            'second_slot_enabled':r.get('second_slot_enabled',False)}


def bind_rule(r, c):
    c = validate_contract(c)
    rr = deepcopy(r)
    for k in ('league','company','provider','water_scale','line_scale','market_period','semantics'):
        if k in rr and rr[k]!=c[k]:
            raise ValueError('规则字段与契约冲突: '+k)
        rr[k]=c[k]
    rr['contract']=c
    rr['rule_schema']=RULE_SCHEMA
    rr['entry_policy']='FIRST_VALID_MATCH_EVENT'
    rr['content_hash']=digest({'rule':semantic_rule(rr),'contract':c})
    return rr


def resolve_contract(rules, explicit=None):
    if explicit is not None:
        c = validate_contract(explicit)
    elif rules and all(isinstance(r.get('contract'),dict) for r in rules):
        c = validate_contract(rules[0]['contract'])
    else:
        raise ValueError('必须提供明确项目/公司/单位契约；旧规则请先显式适配')
    for r in rules:
        if r.get('rule_schema',RULE_SCHEMA)!=RULE_SCHEMA:
            raise ValueError('规则schema不兼容')
        if 'contract' in r and validate_contract(r['contract'])!=c:
            raise ValueError('同一引擎禁止混合不同项目契约')
        for k in ('league','company','provider','water_scale','line_scale','market_period','semantics'):
            if k in r and r[k]!=c[k]:
                raise ValueError('规则范围/单位与项目不一致: '+k)
        h = digest({'rule':semantic_rule(r),'contract':c})
        if 'content_hash' in r and h!=r['content_hash']:
            raise ValueError('规则内容哈希不符，禁止静默修改')
    return c
