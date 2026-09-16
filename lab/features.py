"""Causal, serializable per-market state. Terminal labels are never an input."""
from .common import MISSING, side_for, SEMANTICS, DIRECTIONS, canonical, digest
from copy import deepcopy
from .contracts import LABEL_FIELDS, validate_event, resolve_contract, semantic_rule, STATE_SCHEMA
from .feature_extensions import bound_window_features, window_feature_names
import json

MAX_EVENT_KEYS_PER_MATCH = 100000

def identity_scope(e):
 return canonical([e[k] for k in ('provider','league','company','market_period','water_scale','sid')])

def release_match_state(stream,sid):
 """Only for a known-complete match boundary; signal engine keeps a tombstone."""
 import json
 for key in list(stream.state):
  if key.startswith('['):
   parts=json.loads(key)
   if len(parts)>5 and parts[5]==sid:del stream.state[key]
 identities=stream.state.get('_event_identities',{})
 for key in list(identities):
  if json.loads(key)[5]==sid:del identities[key]
 extra=stream.state.get('_standard_v3')
 if extra:
  extra.get('clocks',{}).pop(sid,None)
  for name in ('markets','pregame','history','roles','windows','path_states'):
   store=extra.get(name,{})
   for key in list(store):
    raw=key.rsplit('|',1)[0] if name=='roles' else key
    if json.loads(raw)[0]==sid:del store[key]

class FeatureStream:
 def __init__(self,state=None,contract=None):
  self.state=deepcopy(state) if state is not None else {};self.last_key=None;self.contract=contract
  if any(k.startswith('[') for k in self.state) and '_event_identities' not in self.state:raise ValueError('旧特征快照缺少完整事件身份历史，必须用原版读取或完整回补')
  identities=self.state.get('_event_identities',{})
  if not isinstance(identities,dict) or any(not isinstance(v,dict) or len(v)>MAX_EVENT_KEYS_PER_MATCH for v in identities.values()):raise ValueError('事件身份快照结构或容量无效')
  for key,s in self.state.items():
   if key.startswith('[') and s.get('last_order') is not None:
    seen=identities.get(canonical(json.loads(key)[:6]),{})
    if seen.get(s.get('last_key'))!=digest(json.loads(s['last_payload'])):raise ValueError('特征快照与事件身份历史不一致，拒绝恢复')
 def preflight(self,e):
  """Validate ordering and identity without changing any feature state."""
  validate_event(e,self.contract)
  seen=self.state.get('_event_identities',{}).get(identity_scope(e),{})
  if e['event_key'] in seen:
   if seen[e['event_key']]!=digest(e):raise ValueError('同一事件ID内容改变，不能作为重复消息忽略')
   return True
  if len(seen)>=MAX_EVENT_KEYS_PER_MATCH:raise ValueError('单场事件身份容量已满；拒绝新事件，不能遗忘旧ID后继续执行')
  key=canonical([e[k] for k in ('provider','league','company','market_period','water_scale','sid','market','phase')])
  s=self.state.get(key)
  if not s:return False
  order=[e['ts'],e['row']]
  if s['last_order'] is not None and order<=s['last_order']:raise ValueError('迟到/乱序或不明确的同位置事件')
  return False
 def feed(self,e):
  if self.preflight(e):return None
  validate_event(e,self.contract)
  key=canonical([e[k] for k in ('provider','league','company','market_period','water_scale','sid','market','phase')]);s=self.state.get(key)
  if s is None:
   s={'last_order':None,'prev_keep':None,'prev_reset':None,'first':None,'same':{},'return_keep':None,'return_reset':None,'minmax':None,'windows':{},
    'hist':{mode:{'line':[],'0':[],'1':[]} for mode in ('keep','reset')}};self.state[key]=s
  order=[e['ts'],e['row']]
  if s['last_order'] is not None:
   if order<s['last_order']:raise ValueError('迟到/乱序事件：不得追溯产生历史订单')
   if order==s['last_order']:
    if e['event_key']==s.get('last_key'):
     if s.get('last_payload')!=canonical(e):raise ValueError('同一事件ID内容改变，不能作为重复消息忽略')
     return None
    raise ValueError('不明确的同位置事件')
  s['last_order']=order;s['last_key']=e['event_key'];s['last_payload']=canonical(e)
  self.state.setdefault('_event_identities',{}).setdefault(identity_scope(e),{})[e['event_key']]=digest(e)
  if not e['valid']:
   s['prev_reset']=None;s['return_reset']=None;s['hist']['reset']={'line':[],'0':[],'1':[]};return None
  L=e['line'];w=e['water'];ts=e['ts'];cur=[L,*w]
  if s['first'] is None:s['first']=cur
  if str(L) not in s['same']:s['same'][str(L)]=w[:]
  if s['minmax'] is None:s['minmax']=[L,L,w[0],w[0],w[1],w[1]]
  base={'line':L,'absline':abs(L),'minute':e['minute'],'status':{'早':0,'即':1,'滚':2}[e['status']],
        'line_init':L-s['first'][0],'line_from_min':L-s['minmax'][0],'line_from_max':L-s['minmax'][1]}
  ch,ca=e['score'];scoreok=ch!=MISSING and ca!=MISSING
  base.update(goal_diff=ch-ca if scoreok else MISSING,abs_diff=abs(ch-ca) if scoreok else MISSING,
              total_goals=ch+ca if scoreok else MISSING,score_code=ch*100+ca if scoreok and ch<100 and ca<100 else MISSING)
  sidefeatures=[]
  for side in (0,1):
   f=base.copy();other=1-side
   f.update(water=w[side],otherwater=w[other],water_init=w[side]-s['first'][side+1],
     other_water_init=w[other]-s['first'][other+1],samewater=w[side]-s['same'][str(L)][side],other_samewater=w[other]-s['same'][str(L)][other],
     water_from_min=w[side]-s['minmax'][2+side*2],water_from_max=w[side]-s['minmax'][3+side*2],
     other_from_min=w[other]-s['minmax'][2+other*2],other_from_max=w[other]-s['minmax'][3+other*2],
     water_from_current_min=w[side]-min(w[side],s['minmax'][2+side*2]),
     role_diff=(ch-ca)*(1 if side==0 else -1) if scoreok and e['market']==1 else MISSING)
   for mode in ('keep','reset'):
    prev=s['prev_'+mode]
    if prev is None or prev[0]!=L:s['return_'+mode]=w[:]
    f['returnwater_'+mode]=w[side]-s['return_'+mode][side]
    f['other_returnwater_'+mode]=w[other]-s['return_'+mode][other]
    dp=L-prev[0] if prev is not None else MISSING
    dw=w[side]-prev[side+1] if prev is not None else MISSING
    f['line_prev_'+mode]=dp;f['water_prev_'+mode]=dw if dp==0 else MISSING
    f['water_prev_any_'+mode]=dw
    code=0 if prev is None else 1 if dp>0 else 2 if dp<0 else 3 if dw>0 else 4 if dw<0 else 0
    hist=s['hist'][mode][str(side)]
    if code:hist.append([code,ts,abs(dp),abs(dw)]);del hist[:-3]
    f['pulse_'+mode]=int(code!=0)
    for k in (1,2,3):
     f[f'path{k}_{mode}']=sum(a[0]*10**j for j,a in enumerate(reversed(hist[-k:]))) if len(hist)>=k else MISSING
     f[f'span{k}_{mode}']=hist[-1][1]-hist[-k][1] if len(hist)>=k else MISSING
     seg=hist[-k:] if len(hist)>=k else []
     f[f'pmin_line{k}_{mode}']=min((x[2] for x in seg if x[0] in (1,2)),default=2**60-1) if seg else MISSING
     f[f'pmin_water{k}_{mode}']=min((x[3] for x in seg if x[0] in (3,4)),default=2**60-1) if seg else MISSING
    lh=s['hist'][mode]['line']
    if side==0 and prev is not None and dp!=0:lh.append([1 if dp>0 else 2,ts]);del lh[:-2]
    f['linepath2_'+mode]=lh[-2][0]*10+lh[-1][0] if len(lh)>=2 else MISSING
   f.update(bound_window_features(s,e,side))
   sidefeatures.append(f)
  ext=s['minmax'];s['minmax']=[min(ext[0],L),max(ext[1],L),min(ext[2],w[0]),max(ext[3],w[0]),min(ext[4],w[1]),max(ext[5],w[1])]
  s['prev_keep']=cur;s['prev_reset']=cur
  return sidefeatures

FEATURE_NAMES={'line','absline','minute','status','line_init','line_from_min','line_from_max','goal_diff','abs_diff','total_goals','score_code','role_diff','water','otherwater','water_init','other_water_init','samewater','other_samewater','water_from_min','water_from_max','other_from_min','other_from_max','water_from_current_min'}
FEATURE_NAMES.update(f'{name}_{mode}' for mode in ('keep','reset') for name in ('returnwater','other_returnwater','line_prev','water_prev','water_prev_any','pulse','path1','path2','path3','span1','span2','span3','linepath2'))

FEATURE_NAMES.update(window_feature_names())
FEATURE_NAMES.update(f'pmin_{kind}{k}_{mode}' for kind in ('line','water') for k in (1,2,3) for mode in ('keep','reset'))

def validate_rules(rules):
 from .standard_features import EXTRA_FIELDS
 from .standard_spec import time_windows
 allowed_windows=set(time_windows('W'))
 ids=set()
 for r in rules:
  if not isinstance(r.get('id'),str) or r['id'] in ids:raise ValueError('规则ID缺失或重复')
  ids.add(r['id'])
  if r.get('direction') not in DIRECTIONS:raise ValueError('未知方向')
  if r.get('semantics',SEMANTICS)!=SEMANTICS:raise ValueError('不兼容的规则语义版本')
  if r.get('entry_policy','FIRST_VALID_MATCH_EVENT')!='FIRST_VALID_MATCH_EVENT':raise ValueError('不支持的首次触发政策')
  if r.get('operator','AND')!='AND':raise ValueError('不支持的组合算符')
  if 'stage' in r and r['stage']!=r['direction'].split('_',1)[0]:raise ValueError('规则阶段与方向不一致')
  if 'priority' in r and (type(r['priority']) is not int or r['priority']<0):raise ValueError('规则优先级必须为非负整数')
  if r.get('live_enabled',False) is not False or r.get('second_slot_enabled',False) is not False:raise ValueError('本引擎不授权真实下注或第二笔')
  if not isinstance(r.get('conditions'),list):raise ValueError('规则条件必须为数组')
  for a in r['conditions']:
   if not isinstance(a,dict) or set(a)-{'feature','op','value','upper','label','span','pulse','min_line_step','min_water_step','sequence','event_model','pattern'}:raise ValueError('条件含未实现字段，拒绝静默忽略')
   if 'pulse' in a and type(a['pulse']) is not bool:raise ValueError('pulse必须为布尔值')
   feature=a.get('feature');window_ok=False
   if isinstance(feature,str) and feature.startswith('window_'):
    try:
     _,lo,hi,kind=feature.split('_',3);window_ok=(int(lo),int(hi)) in allowed_windows and kind in ('line_init','water_init','otherwater_init')
    except (ValueError,TypeError):pass
   if feature not in FEATURE_NAMES and feature not in EXTRA_FIELDS and not window_ok:raise ValueError('尚未支持的特征: '+str(feature))
   if a.get('op') not in ('eq','le','ge','range'):raise ValueError('未知比较符')
   if type(a.get('value')) is not int:raise ValueError('机器阈值必须为整数刻度')
   if a['op']=='range' and (type(a.get('upper')) is not int or a['upper']<=a['value']):raise ValueError('区间无效')
   if ('span' in a or 'pulse' in a or 'min_line_step' in a or 'min_water_step' in a) and not a['feature'].startswith(('path1_','path2_','path3_')):raise ValueError('跨度/脉冲必须绑定明确路径')
   if 'span' in a and (type(a['span']) is not int or a['span']<0):raise ValueError('路径跨度无效')
   for key in ('min_line_step','min_water_step'):
    if key in a and (type(a[key]) is not int or a[key]<=0):raise ValueError('路径步幅必须为正整数刻度')
   if 'sequence' in a or 'event_model' in a or 'pattern' in a:
    if not feature.startswith(('path1_','path2_','path3_')) or a['op']!='eq':raise ValueError('路径顺序语义必须绑定明确路径等式')
    if a.get('sequence','contiguous') not in ('contiguous','subsequence'):raise ValueError('未知连续/可插入路径语义')
    if a.get('event_model','projected') not in ('projected','composite'):raise ValueError('未知盘水事件模型')
    if a.get('event_model')=='composite':
     k=int(feature[4]);pattern=a.get('pattern')
     if a['value']!=0:raise ValueError('复合路径value固定为0，执行模式只能由pattern声明')
     if not isinstance(pattern,list) or len(pattern)!=k or any(type(v) is not int or v not in (1,2,3,4,13,14,23,24) for v in pattern):raise ValueError('复合路径模式不合法')
   if isinstance(feature,str) and feature.startswith(('path1_','path2_','path3_','linepath2_')) and a.get('event_model')!='composite':
    k=2 if feature.startswith('linepath') else int(feature[4]);pattern=[int(v) for v in str(a['value'])] if a['value']>=0 else []
    alphabet=(1,2) if feature.startswith('linepath') else (1,2,3,4)
    if a['op']!='eq' or len(pattern)!=k or any(v not in alphabet for v in pattern) or 'pattern' in a:raise ValueError('投影路径长度、编码或模式不合法')

class SignalEngine:
 def __init__(self,rules,state=None,*,contract=None,execution_policy=None):
  validate_rules(rules)
  self.rules=deepcopy(rules)
  self.contract=resolve_contract(self.rules,contract)
  self.execution_policy=deepcopy(execution_policy or {'mode':'signals_only','live_enabled':False,'second_slot_enabled':False})
  if self.execution_policy.get('live_enabled') or self.execution_policy.get('second_slot_enabled'):raise ValueError('真实/第二笔执行未授权')
  self.binding=digest({'contract':self.contract,'rules':sorted([semantic_rule(r) for r in self.rules],key=lambda r:r['id']),
                       'execution_policy':self.execution_policy,'state_schema':STATE_SCHEMA})
  if state is not None:
   if state.get('schema')!=STATE_SCHEMA or state.get('binding_hash')!=self.binding:raise ValueError('规则/契约/政策已改变，拒绝复用旧触发状态')
   payload={k:v for k,v in state.items() if k!='payload_hash'}
   if state.get('payload_hash')!=digest(payload):raise ValueError('状态完整性校验失败')
   if not isinstance(state.get('features'),dict) or not isinstance(state.get('fired'),list):raise ValueError('状态结构无效')
  from .standard_features import StandardFeatureStream,EXTRA_FIELDS
  atoms=[a for r in self.rules for a in r['conditions']]
  standard=self.execution_policy.get('feature_version')=='v3' or any('sequence' in a or 'event_model' in a or a['feature'] in EXTRA_FIELDS or a['feature'].startswith('window_') and a['feature'] not in FEATURE_NAMES for a in atoms)
  self.standard=standard;self.history_ready=set((state or {}).get('history_ready',[]));self.seen_scopes=set((state or {}).get('seen_scopes',[]));self.closed_matches=set((state or {}).get('closed_matches',[]))
  self.stream=StandardFeatureStream((state or {}).get('features'),self.contract,atoms,self.execution_policy.get('cross_stale_minutes'),self.execution_policy.get('cross_stale_minutes_prematch')) if standard else FeatureStream((state or {}).get('features'),self.contract)
  self.fired=set((state or {}).get('fired',[]))
  self.bygroup={}
  for r in self.rules:
   p,d=r['direction'].split('_',1);key=(0 if p=='PRE' else 1,0 if d in ('OVER','UNDER') else 1)
   self.bygroup.setdefault(key,[]).append(r)
 def feed(self,e):
  from .rules import matches
  if e['sid'] in self.closed_matches:raise ValueError('已归档比赛的迟到报价不得追溯触发')
  snap=self.stream.feed(e)
  scope=canonical([e['sid'],e['market'],e['phase']]);self.seen_scopes.add(scope)
  if snap is None:return []
  if self.standard and scope not in self.history_ready:return []
  # The frozen prematch trigger window: early-market quotes update state but never trigger.
  from .research_standard import trigger_window_allows
  if not trigger_window_allows(e,self.execution_policy):return []
  found=[]
  for r in self.bygroup.get((e['phase'],e['market']),[]):
   key=canonical([e['sid'],r['id']])
   if key in self.fired:continue
   side=side_for(r['direction'],e['line'])
   if side is None:continue
   # AH live contract requires contemporaneous score; OU does not.
   if e['market']==1 and e['phase']==1 and e['score'][0]==MISSING:continue
   if all(matches(a,snap[side]) for a in r['conditions']):
    evidence={a['feature']:snap[side].get(a['feature'],MISSING) for a in r['conditions']}
    if self.standard:
     from .standard_features import predicate_key
     evidence={'history_complete':True,'atoms':[{'feature':a['feature'],'condition':{k:v for k,v in a.items() if k!='label'},
      'value':snap[side].get(predicate_key(a),False) if 'sequence' in a or a.get('event_model')=='composite' else snap[side].get(a['feature'],MISSING),
      'path_events':snap[side].get(predicate_key(a)+'_evidence',[])} for a in r['conditions']]}
    self.fired.add(key);found.append({'strategy_id':r['id'],'direction':r['direction'],'sid':e['sid'],'eid':e['eid'],'event_key':e['event_key'],'ts':e['ts'],'side':side,'line':e['line'],'water':e['water'][side],'score':e['score'],'evidence':evidence})
  return found
 def mark_history_complete(self,sid,market=None,phase=None):
  if not isinstance(sid,str) or not sid or market not in (None,0,1) or phase not in (None,0,1):raise ValueError('历史范围标记无效')
  markets=(0,1) if market is None else (market,);phases=(0,1) if phase is None else (phase,)
  scopes=[canonical([sid,m,p]) for m in markets for p in phases]
  if any(scope in self.seen_scopes and scope not in self.history_ready for scope in scopes):raise ValueError('已在历史未确认时接收报价；必须重新回补，不能把当前报价冒充首盘')
  self.history_ready.update(scopes)
 def is_history_ready(self,e):return not self.standard or canonical([e['sid'],e['market'],e['phase']]) in self.history_ready
 def close_match(self,sid):
  release_match_state(self.stream,sid);self.closed_matches.add(sid)
  # A closed match can never feed again; keep only its tombstone so snapshots stay bounded.
  prefix=canonical([sid])[:-1]+','
  self.history_ready={x for x in self.history_ready if not x.startswith(prefix)}
  self.seen_scopes={x for x in self.seen_scopes if not x.startswith(prefix)}
  self.fired={x for x in self.fired if not x.startswith(prefix)}
 def snapshot(self):
  payload={'schema':STATE_SCHEMA,'binding_hash':self.binding,'features':deepcopy(self.stream.state),'fired':sorted(self.fired),'semantics':SEMANTICS,'history_ready':sorted(self.history_ready),'seen_scopes':sorted(self.seen_scopes),'closed_matches':sorted(self.closed_matches)}
  return {**payload,'payload_hash':digest(payload)}
