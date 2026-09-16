"""Shared data contracts; no network, account credentials, or order endpoints."""
from pathlib import Path
from decimal import Decimal, InvalidOperation,ROUND_FLOOR
from functools import lru_cache
from datetime import datetime
import csv, json, hashlib, os, re, tempfile, gzip, time

MISSING = -(2**60)
VERSION = '0.7.0'
SEMANTICS = 'FSL_event_v1'
ROOT = Path(__file__).resolve().parent.parent
DIRECTIONS = {f'{p}_{d}':f'{s}{c}' for p,s in [('PRE','赛前'),('LIVE','滚球')] for d,c in [('GIVE','让球方'),('RECEIVE','受让方'),('PK_HOME','平手主'),('PK_AWAY','平手客'),('OVER','大球'),('UNDER','小球'),('HOME','固定主'),('AWAY','固定客')]}
CORE = [k for k in DIRECTIONS if k.split('_',1)[1] not in ('HOME','AWAY')]
# Risk and sample gates scale with the evidence: absolute streak/drawdown limits reward small lucky samples.
DEFAULT = dict(profile='standard', company='皇冠', min_profit='10', stress_min_profit='10',
 min_matches=40, min_matches_share='0.25', min_matches_ceiling=80, min_segment_share='0.5', min_return_drawdown_ratio='2',
 match_cap=4, directions=list(DIRECTIONS), max_rules_per_direction=0,
 select_budget=12, selection_profit_tolerance='0.50', selection_profit_tolerance_share='0.05',
 stale_minutes=5, chunk_size=2048, second_slot_enabled=False, live_enabled=False,
 portfolio_min_return_drawdown_ratio='2', conservative_portfolio_min_return_drawdown_ratio='3',
 selection_eval_budget=200000, selection_checkpoint_every=64,
 safe_nohit_pruning=True, max_mask_mb=1024, min_free_disk_mb=256, quote_cache_entries=100000,
 cross_stale_minutes=5,cross_stale_minutes_prematch=1440,cross_stale_basis='FSL_CROSS_AGE_LIVE_5M_PREMATCH_24H_V2',
 prematch_trigger_status='即',search_grammar='compact_v1',
 holdout_months=12,holdout_max_share='0.5',holdout_min_orders=30,package_handoff=True,
 max_feature_cache_mb=128,standard_node_budget=0,standard_review_budget=0,bootstrap_repetitions=200,max_pool_pairs=0,
 release_direction_caches=True,max_run_minutes=0,max_output_mb=0,alternative_sample_ratio_min='0.75',alternative_sample_ratio_max='2')
# The previous validation policy remains explicitly available for old regression/evaluation workflows.
VALIDATION_DEFAULT={**DEFAULT,'research_objective':'validation','min_history_years':0,'historical_diagnostics':True}
DEFAULT={**VALIDATION_DEFAULT,'research_objective':'historical','min_profit':'10','stress_min_profit':'0',
         'holdout_months':0,'min_history_years':2,'historical_diagnostics':False,'prematch_trigger_status':'any',
         # No move or evaluation cap: the local search runs to its own stop. Exact incremental replay keeps each
         # neighbouring roster cheap, so checkpoints are written every 4096 evaluations (and at least every 30 s).
         'select_budget':0,'selection_eval_budget':0,'selection_checkpoint_every':4096}

from .history_policy import DEFAULTS as HISTORY_POLICY_DEFAULTS
DEFAULT = {**HISTORY_POLICY_DEFAULTS, **DEFAULT}
VALIDATION_DEFAULT = {**HISTORY_POLICY_DEFAULTS, **VALIDATION_DEFAULT,
    'historical_policy_version':'LEGACY_VALIDATION_V1','portfolio_segment_checks':False,
    'segment_basis':'end_anchored_365','minor_segment_loss_policy':'disclose','missing_result_status':'trial'}

def historical_objective(config):return config.get('research_objective','validation')=='historical'
# Retired absolute gates are refused with their replacement instead of being silently ignored.
DEPRECATED_CONFIG = {'min_year_matches':'min_segment_share（按研究段实际比赛数折算）','max_streak':'min_return_drawdown_ratio（连亏只作报表列）',
 'max_drawdown':'min_return_drawdown_ratio（净胜÷最大回撤）','portfolio_max_drawdown':'portfolio_min_return_drawdown_ratio',
 'conservative_portfolio_max_drawdown':'conservative_portfolio_min_return_drawdown_ratio'}

def plain(value):
 s='' if value is None else str(value).strip()
 return s[2:-1] if s.startswith('="') and s.endswith('"') else s

def validate_date(value,where=''):
 value=plain(value)
 try:
  if datetime.strptime(value,'%Y-%m-%d').strftime('%Y-%m-%d')!=value:raise ValueError()
 except ValueError:raise ValueError(f'{where}: 日期必须为有效的 YYYY-MM-DD')
 return value

def verify_input_records(records):
 for rec in records:
  if sha(rec['path'])!=rec['sha256']:raise ValueError('原始输入内容改变，必须新建研究版本: '+str(rec['path']))

def validate_csv_header(names,where=''):
 """Reject duplicate CSV columns before converting rows to dictionaries."""
 if names is None:raise ValueError(f'{where}: CSV缺少表头')
 if any(not isinstance(name,str) or not name.strip() for name in names):raise ValueError(f'{where}: CSV含空白列名')
 duplicates=sorted({name for name in names if names.count(name)>1})
 if duplicates:raise ValueError(f'{where}: CSV表头重复: '+','.join(duplicates))

def validate_quote_fields(date,closed,where=''):
 try:
  value=plain(date)
  if datetime.strptime(value,'%Y-%m-%d').strftime('%Y-%m-%d')!=value:raise ValueError()
 except ValueError:raise ValueError(f'{where}: 日期必须为有效的 YYYY-MM-DD')
 value=plain(closed)
 if value not in ('','是','否'):raise ValueError(f'{where}: 封盘仅支持空白/否（开放）或是（封盘），实际为 {value!r}')
 return value=='是'

QUALITY_SCOPES={'all','all_crown','csl_crown','pinbo','all_pinbo'}
def validate_quality_row(row,where=''):
 for key in ('sId','reason','scope','source'):
  if not plain(row.get(key)):raise ValueError(f'{where}: 质量清单缺少 {key}')
 if plain(row['scope']) not in QUALITY_SCOPES:raise ValueError(f"{where}: 未知质量清单scope {plain(row['scope'])!r}，拒绝忽略排除要求")

@lru_cache(16384)
def water_decimal_places(value):
 try:
  number=Decimal(plain(value));places=max(0,-number.as_tuple().exponent) if number.is_finite() else 0
 except InvalidOperation:return 0
 if places>6:raise ValueError('水位精度超过本版6位支持范围')
 return places

def money_threshold(value,denominator):
 """Exact integer boundary for nonnegative monetary <= / strict > comparisons."""
 if type(denominator) is not int or denominator<=0:raise ValueError('金额刻度分母必须为正整数')
 try:amount=Decimal(plain(value))*denominator
 except (InvalidOperation,ValueError,TypeError):raise ValueError('金额阈值不可解释')
 if not amount.is_finite() or amount<0 or amount>2**63-1:raise ValueError('金额阈值超出受支持的非负整数比较范围')
 return int(amount.to_integral_value(rounding=ROUND_FLOOR))

@lru_cache(100000)
def scaled(s, scale):
 try:
  x=Decimal(plain(s))*scale
  if not x.is_finite() or x != x.to_integral_value() or abs(x)>10**12:return MISSING
  return int(x)
 except (InvalidOperation,ValueError,TypeError): return MISSING

@lru_cache(20000)
def _score_tuple(s):
 m=re.fullmatch(r'(\d+)\s*[-:]\s*(\d+)',plain(s))
 return (int(m[1]),int(m[2])) if m else (MISSING,MISSING)

def score(s):
 return list(_score_tuple(s))

@lru_cache(300000)
def timestamp(s):
 try:return int((datetime.strptime(plain(s),'%Y-%m-%d %H:%M')-datetime(1970,1,1)).total_seconds()//60)
 except (ValueError,TypeError):return MISSING

def sha(path):
 h=hashlib.sha256()
 with open(path,'rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()

def canonical(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(x):return hashlib.sha256(canonical(x).encode()).hexdigest()
def atomic_replace(source,destination,timeout=15.0):
 """Atomic publication with a bounded Windows sharing/access retry window."""
 deadline=time.monotonic()+max(0,float(timeout));delay=.02
 while True:
  try:return os.replace(source,destination)
  except OSError as error:
   transient=os.name=='nt' and (isinstance(error,PermissionError) or getattr(error,'winerror',None) in (5,32,33))
   remaining=deadline-time.monotonic()
   if not transient or remaining<=0:raise
   time.sleep(min(delay,remaining));delay=min(delay*2,.5)

def atomic_json(path, data, *, compact=False):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 fd,tmp=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
 try:
  with os.fdopen(fd,'w',encoding='utf-8') as f:
   json.dump(data,f,ensure_ascii=False,indent=None if compact else 2,separators=(',',':') if compact else None,allow_nan=False);f.flush();os.fsync(f.fileno())
  atomic_replace(tmp,path)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
def read_json(p,default=None):
 # Windows briefly denies a read while another process atomically replaces the same file.
 for attempt in range(40):
  try:return json.loads(Path(p).read_text(encoding='utf-8-sig'))
  except FileNotFoundError:return default
  except PermissionError:
   if attempt==39:raise
   time.sleep(.05)

def csv_write(path,rows,fields=None):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 from itertools import chain
 iterator=iter(rows);first=next(iterator,None)
 fields=fields or (list(first) if first is not None else ['状态'])
 with open(path,'w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader()
  for r in chain(() if first is None else (first,),iterator):
   # Never export untrusted Excel formulas; numeric negatives remain numbers.
   w.writerow({k:("'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) and not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?',v.strip()) else v) for k,v in r.items()})

def write_jsonl(path, rows):
 op=gzip.open if str(path).endswith('.gz') else open
 with op(path,'wt',encoding='utf-8') as f:
  for r in rows:f.write(canonical(r)+'\n')
def read_jsonl(path):
 op=gzip.open if str(path).endswith('.gz') else open
 with op(path,'rt',encoding='utf-8') as f:
  for line in f:
   if line.strip():yield json.loads(line)

def settlement(line, water, margin, side, water_scale=100):
 """P&L in 1/(2*water_scale) stake units; line is in quarter goals."""
 lo=(line//2)*2;hi=lo if line%2==0 else lo+2;sign=1 if side==0 else -1
 return sum(water if sign*(margin*4-p)>0 else -water_scale if sign*(margin*4-p)<0 else 0 for p in (lo,hi))

def side_for(direction,line):
 d=direction.split('_',1)[1]
 if d in ('OVER','HOME'):return 0
 if d in ('UNDER','AWAY'):return 1
 if d=='PK_HOME':return 0 if line==0 else None
 if d=='PK_AWAY':return 1 if line==0 else None
 if line==0:return None
 if d=='GIVE':return 0 if line>0 else 1
 if d=='RECEIVE':return 1 if line>0 else 0
 raise ValueError('不支持方向: '+direction)

# Modules that cannot change research numbers (UI, job registry, locks, reports, packaging, release tooling).
# They stay in release_fingerprint, but editing them does not orphan paused research checkpoints.
NON_SEMANTIC_MODULES=('cli.py','fleet.py','launcher.py','locking.py','maintenance.py','migration.py','packaging.py','release.py','reporting.py','resources.py','server.py','store.py')
EXECUTION_ONLY_MODULES=('paper_execution.py','paper_runner.py','paper_verification.py','verify.py','handoff_assets.py')

def source_fingerprint():
 """Computation identity for checkpoints, caches and research binding. The browser UI and the
 NON_SEMANTIC_MODULES are outside it (release_hash covers them), so such edits keep paused research resumable."""
 files=[*[p for p in sorted((ROOT/'lab').glob('*.py')) if p.name not in (*NON_SEMANTIC_MODULES,*EXECUTION_ONLY_MODULES)],*[ROOT/name for name in ('requirements.txt','requirements.lock') if (ROOT/name).is_file()]]
 return digest({'schema':'FSL_RESEARCH_INPUTS_V6','non_research_excluded':list((*NON_SEMANTIC_MODULES,*EXECUTION_ONLY_MODULES)),'files':[(p.relative_to(ROOT).as_posix(),sha(p)) for p in sorted(files)]})

def execution_fingerprint():
 """Runtime identity; execution repairs cannot reuse old S6 evidence."""
 files=[*sorted((ROOT/'lab').glob('*.py')),*[ROOT/name for name in ('requirements.txt','requirements.lock') if (ROOT/name).is_file()]]
 return digest({'schema':'FSL_RUNTIME_INPUTS_V1','files':[(p.relative_to(ROOT).as_posix(),sha(p)) for p in sorted(files)]})

def _decimal(z,key,low,high,*,low_open=False,high_open=False):
 try:n=Decimal(str(z[key]))
 except (InvalidOperation,ValueError,TypeError):raise ValueError(key+'必须为数字')
 if not n.is_finite() or n<low or n>high or low_open and n==low or high_open and n==high:raise ValueError(f'{key}超出允许范围')
 z[key]=format(n,'f')
 return n

def check_config(c):
 if not isinstance(c,dict):raise ValueError('配置必须为JSON对象')
 metadata={'engine_hash','created_version','research_partition','benchmark_scope','acceptance_scope','_water_scale','fleet_assignment'}
 retired=sorted(set(c)&set(DEPRECATED_CONFIG))
 if retired:raise ValueError('配置字段已废弃（绝对门槛不随样本量变化）：'+'；'.join(f'{k} → 改用 {DEPRECATED_CONFIG[k]}' for k in retired))
 unknown=set(c)-set(DEFAULT)-metadata
 if unknown:raise ValueError('未知配置字段，拒绝静默使用默认值: '+','.join(sorted(unknown)))
 objective=c.get('research_objective','validation' if c.get('profile','standard')!='standard' else DEFAULT['research_objective'])
 if objective not in ('historical','validation'):raise ValueError('研究目标必须为historical或validation')
 z=dict(DEFAULT if objective=='historical' else VALIDATION_DEFAULT);z.update(c)
 for key in ('safe_nohit_pruning','release_direction_caches','package_handoff','historical_diagnostics','live_enabled','second_slot_enabled','portfolio_segment_checks','complementarity_research'):
  if type(z[key]) is not bool:raise ValueError(key+'必须为布尔值')
 if z['profile'] not in ('smoke','routine','expanded','standard'):raise ValueError('未知搜索规格')
 if historical_objective(z) and z['profile']!='standard':raise ValueError('全历史研究请使用standard规格；旧有限档位仅用于validation回归')
 if z.get('live_enabled') or z.get('second_slot_enabled'):raise ValueError('此软件不提供真实下注/第二笔执行')
 if not isinstance(z['directions'],list) or not z['directions'] or any(not isinstance(d,str) or d not in DIRECTIONS for d in z['directions']):raise ValueError('方向配置无效')
 z['directions']=list(dict.fromkeys(z['directions']))
 for key in ('min_matches','min_matches_ceiling','max_rules_per_direction','match_cap','select_budget','stale_minutes','chunk_size','selection_eval_budget','selection_checkpoint_every','max_mask_mb','min_free_disk_mb','quote_cache_entries','max_feature_cache_mb','standard_node_budget','standard_review_budget','bootstrap_repetitions','max_pool_pairs','max_run_minutes','max_output_mb','holdout_months','holdout_min_orders','min_history_years'):
  try:n=Decimal(str(z[key]))
  except (InvalidOperation,ValueError,TypeError):raise ValueError(key+'必须为非负整数')
  if not n.is_finite() or n<0 or n!=n.to_integral_value():raise ValueError(key+'必须为非负整数')
  z[key]=int(n)
 if not 1<=z['match_cap']<=24 or not 0<=z['select_budget']<=100:raise ValueError('模拟名额/选择预算不合法（组合改动预算0—100，0表示直到局部停止）')
 if not 1<=z['chunk_size']<=65536 or not 0<=z['stale_minutes']<=1440:raise ValueError('分块或报价陈旧度参数不合法')
 for key in ('min_profit','stress_min_profit','selection_profit_tolerance'):
  _decimal(z,key,0,Decimal(2**63-1)/2000000)
 if z['min_matches']>z['min_matches_ceiling']:raise ValueError('单条最低场次下限不能高于场次要求封顶')
 _decimal(z,'min_matches_share',0,1,low_open=True);_decimal(z,'min_segment_share',0,1)
 _decimal(z,'min_return_drawdown_ratio',0,1000);_decimal(z,'selection_profit_tolerance_share',0,1,high_open=True)
 if _decimal(z,'conservative_portfolio_min_return_drawdown_ratio',0,1000)<_decimal(z,'portfolio_min_return_drawdown_ratio',0,1000):raise ValueError('较低风险组合的收益回撤比要求不能低于默认组合')
 _decimal(z,'holdout_max_share',0,1,low_open=True,high_open=True)
 if z['holdout_months']>60:raise ValueError('样本外留出月数须为0—60')
 if type(z['min_history_years']) is not int or not 0<=z['min_history_years']<=50:raise ValueError('最少历史覆盖年数须为0—50的整数')
 for key in ('alternative_sample_ratio_min','alternative_sample_ratio_max'):_decimal(z,key,0,10**6)
 if not 1<=z['selection_checkpoint_every']<=10000 or z['max_mask_mb']<1 or z['quote_cache_entries']<1:raise ValueError('资源/选择断点参数无效')
 if not Decimal('0')<Decimal(z['alternative_sample_ratio_min'])<=1<=Decimal(z['alternative_sample_ratio_max']):raise ValueError('替代样本比例范围必须包含1')
 if not isinstance(z['company'],str) or not plain(z['company']):raise ValueError('公司必须为非空文本')
 if z['prematch_trigger_status'] not in ('即','any'):raise ValueError('赛前触发盘口只支持“即”（即时盘）或“any”（早盘与即时盘）')
 if z['search_grammar'] not in ('compact_v1','balanced_v1','v3_full'):raise ValueError('搜索语法只支持compact_v1、balanced_v1或v3_full')
 if z['segment_basis'] not in ('calendar_year','end_anchored_365','fixed_365'):raise ValueError('未知研究段划分')
 if not isinstance(z['segment_anchor_date'],str):raise ValueError('研究段锚点必须为日期字符串')
 if z['segment_anchor_date']:validate_date(z['segment_anchor_date'])
 if z['segment_basis']=='fixed_365' and not z['segment_anchor_date']:raise ValueError('固定365天研究段必须提供冻结锚点')
 if z['minor_segment_loss_policy'] not in ('block','disclose'):raise ValueError('小段亏损政策必须为block或disclose')
 if z['missing_result_status'] not in ('exclude','trial','archive_contract'):raise ValueError('未知完场状态缺失政策')
 if not isinstance(z['archive_result_contract'],str):raise ValueError('文件级完场契约必须为来源说明文本')
 if z['missing_result_status']=='archive_contract' and not z['archive_result_contract'].strip():raise ValueError('文件级完场契约不可为空')
 for key in ('segment_evidence_min_matches','complementarity_pair_budget'):
  try:n=Decimal(str(z[key]))
  except (InvalidOperation,ValueError,TypeError):raise ValueError(key+'必须为非负整数')
  if not n.is_finite() or n<0 or n!=n.to_integral_value():raise ValueError(key+'必须为非负整数')
  z[key]=int(n)
 if z['segment_evidence_min_matches']<1:raise ValueError('研究段证据最少场次须大于0')
 _decimal(z,'major_segment_share',0,1,low_open=True)
 _decimal(z,'max_best_segment_profit_share',0,1,low_open=True)
 if z['historical_policy_version'] not in ('FSL_HISTORY_SEGMENT_SAFE_V2','LEGACY_VALIDATION_V1'):raise ValueError('未知历史研究政策版本')
 for key,limit in (('cross_stale_minutes',1440),('cross_stale_minutes_prematch',10080)):
  if z[key] is None:continue
  try:n=Decimal(str(z[key]))
  except (InvalidOperation,ValueError,TypeError):raise ValueError(key+'必须为完整分钟')
  if not n.is_finite() or n<1 or n>limit or n!=n.to_integral_value():raise ValueError(f'{key}须为1—{limit}完整分钟，或明确留空阻塞跨市场条件')
  z[key]=int(n)
 # Either window alone is silently reinterpreted downstream, so both must be set or both left empty.
 if (z['cross_stale_minutes'] is None)!=(z['cross_stale_minutes_prematch'] is None):raise ValueError('滚球与赛前跨市场陈旧度必须同时填写或同时留空：赛前跨市场陈旧度不能单独留空（会沿用滚球口径），滚球陈旧度也不能单独留空（赛前设置会被丢弃）；请各填1—10080分钟，或把两个陈旧度同时留空以阻塞跨市场条件')
 if z['max_feature_cache_mb']<1:raise ValueError('特征缓存预算须为正数')
 if z['bootstrap_repetitions']>20000:raise ValueError('单次重采样超过20000次，请使用明确分批验证方案')
 if z['profile']=='standard' and historical_objective(z):
  if z['holdout_months']!=0:raise ValueError('全历史标准研究必须使用全部选定历史，不能扣除留出期')
 elif z['profile']=='standard':
  changed=[k for k,v in STANDARD_RESEARCH_THRESHOLDS.items() if not _same_setting(z[k],v)]
  if changed:raise ValueError('standard档研究标准固定（净胜与压力净胜>10；场次≥max(40,方向可用场次×25%)且至多要求80场；各研究段覆盖≥期望的50%；收益回撤比≥2；组合收益回撤比2／较低风险3；利润容差max(0.5单位,5%)；留出最近12个月作样本外；赛前只在即时盘触发；跨市场陈旧度滚球5分钟、赛前24小时）；需要其他门槛请使用旧有限档位，结果不能标为标准研究。改动项: '+','.join(changed))
 if ('cross_stale_minutes' in c or 'cross_stale_minutes_prematch' in c) and 'cross_stale_basis' not in c:z['cross_stale_basis']='EXPLICIT_FROZEN_RESEARCH_CONFIGURATION'
 return z

STANDARD_RESEARCH_THRESHOLDS={'min_profit':'10','stress_min_profit':'10','min_matches':'40','min_matches_share':'0.25','min_matches_ceiling':'80','min_segment_share':'0.5',
 'min_return_drawdown_ratio':'2','portfolio_min_return_drawdown_ratio':'2','conservative_portfolio_min_return_drawdown_ratio':'3',
 'selection_profit_tolerance':'0.50','selection_profit_tolerance_share':'0.05','holdout_months':'12','holdout_max_share':'0.5','holdout_min_orders':'30',
 'prematch_trigger_status':'即','cross_stale_minutes':'5','cross_stale_minutes_prematch':'1440'}

def standard_thresholds(config):
 return {'holdout_months':0} if historical_objective(config) else STANDARD_RESEARCH_THRESHOLDS

def _same_setting(value,expected):
 try:return Decimal(str(value))==Decimal(str(expected))
 except (InvalidOperation,ValueError,TypeError):return str(value)==str(expected)

def standard_thresholds_match(config):
 if historical_objective(config):
  try:return config['holdout_months']==0
  except (KeyError,TypeError):return False
 try:return all(_same_setting(config[k],v) for k,v in STANDARD_RESEARCH_THRESHOLDS.items())
 except (KeyError,TypeError):return False

# Command-line trial safety net (0 in DEFAULT means unlimited); the workstation UI starts from DEFAULT without it.
# The compact grammar needs a few tens of thousands of nodes per direction, so this cap rarely binds.
TRIAL_BUDGETS={'standard_node_budget':200000,'max_run_minutes':0,'max_output_mb':20000}
