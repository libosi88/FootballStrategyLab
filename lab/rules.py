"""Versioned finite search dictionary. This release is not the full v3 W/C/T."""
from itertools import product,combinations
from .common import *

LABELS={'line':'有符号盘口','absline':'盘口绝对值','water':'实际买入侧水位','otherwater':'对侧水位','minute':'数字分钟（-2=中场）','status':'源状态（0早1即2滚）',
'goal_diff':'主队减客队比分','abs_diff':'分差绝对值','total_goals':'当时总球','score_code':'比分编码(主*100+客)','role_diff':'实际买入队领先球数',
'line_init':'盘口相对同阶段首盘变化','line_from_min':'盘口相对此前最低变化','line_from_max':'盘口相对此前最高变化',
'water_init':'买入侧水位相对阶段首水','other_water_init':'对侧相对阶段首水','samewater':'买入侧相对该盘口首次水位','other_samewater':'对侧相对该盘口首次水位',
'water_from_min':'买入侧水位相对此前最低','water_from_max':'买入侧水位相对此前最高','other_from_min':'对侧水位相对此前最低','other_from_max':'对侧水位相对此前最高',
'water_from_current_min':'买入侧水位相对含当前最低','water_from_current_max':'买入侧水位相对含当前最高',
'line_from_current_min':'盘口相对含当前最低','line_from_current_max':'盘口相对含当前最高','other_from_current_min':'对侧水位相对含当前最低','other_from_current_max':'对侧水位相对含当前最高',
'other_water_prev_keep':'对侧相邻同盘水差/跨封盘保留','other_water_prev_reset':'对侧相邻同盘水差/封盘重置','other_water_prev_any_keep':'对侧相邻水差（不限同盘）/跨封盘保留','other_water_prev_any_reset':'对侧相邻水差（不限同盘）/封盘重置',
'linepath2_keep':'最近两次盘口变化方向/跨封盘保留（1升盘2降盘）','linepath2_reset':'最近两次盘口变化方向/封盘重置（1升盘2降盘）'}

def unit(feature,scale):
 if feature.endswith('_changes') or feature in ('cross_line_alignment','cross_init_alignment'):return 1
 if feature.startswith(('pre_line_','cross_line_','line_return_')) or feature in ('cross_line','line_same','line_from_current_min','line_from_current_max'):return 4
 if feature.startswith('other_'):return scale
 if (feature.startswith('window_') and feature.endswith('_line_init')) or feature.startswith('pmin_line'):return 4
 if feature in ('line','absline','line_init','line_from_min','line_from_max') or feature.startswith('line_prev_'):return 4
 if any(x in feature for x in ('water','from_min','from_max')) and feature not in ('line_from_min','line_from_max'):return scale
 return 1

def label(a,scale):
 f=a['feature'];title=LABELS.get(f,f)
 if f=='line_same':title='盘口相对该盘口首次出现的差值（恒零）'
 if f.startswith('line_return_'):title='盘口相对最近重返该盘口的差值（恒零）/'+f.split('_')[-1]
 if f.startswith('pre_'):
  title='已结束赛前阶段摘要/'+{'line_open':'首盘','line_close':'末次有效盘口','line_min':'最低盘口','line_max':'最高盘口','line_change':'净变盘','line_changes':'变盘次数','water_open':'买入侧首水','water_close':'买入侧末水','water_min':'买入侧最低水位','water_max':'买入侧最高水位','water_change':'买入侧净水差','water_changes':'买入侧水位变化次数','last_path1':'末一步路径','last_path2':'末两步路径','last_path3':'末三步路径','closed':'末状态封盘标记'}.get(f[4:],f[4:])
 if f.startswith('cross_'):
  title='另一市场前一完整分钟可见状态/'+{'age':'报价年龄（墙钟分钟）','phase':'阶段（0赛前1滚球）','line':'盘口','water0':'上侧水位','water1':'下侧水位','line_init':'相对阶段首盘','water0_init':'上侧相对首水','water1_init':'下侧相对首水','line_prev':'相邻变盘','water0_prev':'上侧相邻水差','water1_prev':'下侧相邻水差','line_alignment':'两市场相邻盘口同向标记','init_alignment':'两市场相对首盘同向标记'}.get(f[6:],f[6:])
 if f.startswith('role_water_'):title='当前买入角色在各历史时点的水位/'+f[len('role_water_'):]
 if f.startswith('stoppage_'):title={'stoppage_base':'明确补时所属半场（45/90）','stoppage_added':'明确补时分钟','stoppage_total':'明确补时累计分钟'}[f]
 if f.startswith('window_'):
  _,lo,hi,kind=f.split('_',3);title=f'绑定数字窗口[{lo},{hi})内首个有效报价以来的'+{'line_init':'盘口差','water_init':'买入侧水差','otherwater_init':'对侧水差'}[kind]
 if f.startswith('path'):title=f.replace('path','盘优先路径').replace('keep','跨封盘保留').replace('reset','封盘重置')+'（1升盘2降盘3同盘升水4同盘降水）'
 if f.startswith('water_prev'):title=('相邻有效买入侧水差（不限同盘）/' if '_any_' in f else '相邻有效同盘买入侧水差/')+('跨封盘保留' if f.endswith('keep') else '封盘重置')
 if f.startswith('returnwater'):title='买入侧相对最近重返该盘/'+f.split('_')[-1]
 if f.startswith('other_returnwater'):title='对侧相对最近重返该盘/'+f.split('_')[-1]
 u=unit(f,scale);fmt=lambda n:format(Decimal(int(n))/u,'f')
 if a['op']=='range':expr=title+'∈['+fmt(a['value'])+','+fmt(a['upper'])+')'
 else:expr=title+{'eq':'=','le':'≤','ge':'≥'}[a['op']]+fmt(a['value'])
 if 'span' in a:expr+=f"；首尾跨度≤{a['span']}墙钟分钟"
 if a.get('pulse'):expr+='；仅末步事件触发'
 if 'min_line_step' in a:expr+='；每个盘口步骤至少'+format(Decimal(a['min_line_step'])/4,'f')
 if 'min_water_step' in a:expr+='；每个同盘水位步骤至少'+format(Decimal(a['min_water_step'])/scale,'f')
 if a.get('sequence')=='subsequence':expr+='；允许插入其他事件，末步必须是当前最后投影事件'
 if a.get('event_model')=='composite':expr+='；同报价盘水双属性，模式'+str(a['pattern'])
 return expr

def matches(a,f):
 if 'sequence' in a or a.get('event_model')=='composite':
  from .standard_features import predicate_key
  return bool(f.get(predicate_key(a),False))
 v=f.get(a['feature'],MISSING)
 if v==MISSING:return False
 # Text half-time is minute=-2; numeric comparisons cover numeric minutes only.
 if a['feature']=='minute' and a['op']!='eq' and v<0:return False
 good=(v==a['value'] if a['op']=='eq' else v<=a['value'] if a['op']=='le' else v>=a['value'] if a['op']=='ge' else a['value']<=v<a['upper'])
 if not good:return False
 if 'span' in a:
  k=a['feature'].split('_')[0][4:];mode=a['feature'].split('_')[-1]
  if f.get(f'span{k}_{mode}',MISSING)==MISSING or f[f'span{k}_{mode}']>a['span']:return False
 if a.get('pulse') and f.get('pulse_'+a['feature'].split('_')[-1])!=1:return False
 for kind in ('line','water'):
  if 'min_'+kind+'_step' in a:
   k=a['feature'].split('_')[0][4:];mode=a['feature'].split('_')[-1];v=f.get(f'pmin_{kind}{k}_{mode}',MISSING)
   if v==MISSING or v<a['min_'+kind+'_step']:return False
 return True

def mask(a,arrays):
 if hasattr(arrays,'atom_mask'):return arrays.atom_mask(a)
 if 'sequence' in a or a.get('event_model')=='composite':
  from .standard_features import predicate_key
  key=predicate_key(a)
  if key not in arrays:raise ValueError('需要v3路径计算器，不能降级为普通路径')
  return arrays[key].astype(bool)
 v=arrays[a['feature']];valid=v!=MISSING
 if a['feature']=='minute' and a['op']!='eq':valid &= v>=0
 valid &= (v==a['value'] if a['op']=='eq' else v<=a['value'] if a['op']=='le' else v>=a['value'] if a['op']=='ge' else (v>=a['value'])&(v<a['upper']))
 if 'span' in a:
  k=a['feature'].split('_')[0][4:];mode=a['feature'].split('_')[-1];x=arrays[f'span{k}_{mode}'];valid &= (x!=MISSING)&(x<=a['span'])
 if a.get('pulse'):valid &= arrays['pulse_'+a['feature'].split('_')[-1]]==1
 for kind in ('line','water'):
  if 'min_'+kind+'_step' in a:
   k=a['feature'].split('_')[0][4:];mode=a['feature'].split('_')[-1];v=arrays[f'pmin_{kind}{k}_{mode}']
   valid &= (v!=MISSING)&(v>=a['min_'+kind+'_step'])
 return valid

def dictionary(arrays,direction,scale,profile):
 atoms=[];seen=set();pidx=[];tidx=[]
 def add(f,op,v,u=None,level=2,**kw):
  if f not in arrays:return
  a={'feature':f,'op':op,'value':int(v),**kw}
  if u is not None:a['upper']=int(u)
  k=canonical(a)
  if k in seen:return
  seen.add(k);i=len(atoms);a['label']=label(a,scale);atoms.append(a)
  if level>=1:pidx.append(i)
  if level>=2:tidx.append(i)
 def nums(f,values,core,ops=('le','ge')):
  for v in sorted(set(values)):
   for op in ops:add(f,op,v,level=2 if v in core else 1)
 def rng(f,lo,hi,level=2):add(f,'range',lo,hi,level=level)
 live=direction.startswith('LIVE');ah=not direction.endswith(('OVER','UNDER'))
 if profile=='smoke':
  nums('line',[-4,0,4,8,10,12,14,18],{0,8,12});nums('water',[(scale*x//100) for x in (70,85,95,105,120)],{(scale*95//100),(scale*105//100)})
  if live:
   for lo,hi in [(0,16),(16,31),(31,46),(46,61),(61,76),(76,91)]:rng('minute',lo,hi)
   for x in (-1,0,1):add('goal_diff','eq',x)
   nums('total_goals',[0,1,2],{0,1})
  for x in (-15,-10,10,15):add('samewater','le' if x<0 else 'ge',(scale*x//100))
  for c in (12,21,33,44):add('path2_keep','eq',c)
  return atoms,pidx,[]
 # Broad side-symmetric thresholds. Full observed line domain; water anchors + data range on configured step.
 for f in ('line','absline','line_init'):
  valid=arrays[f][arrays[f]!=MISSING]
  vals=list(range(int(valid.min()),int(valid.max())+1)) if valid.size else []
  nums(f,vals,set((-4,0,2,4,8,10,12,14,18)))
  for x in vals:add(f,'eq',x,level=1)
 for f in ('water','otherwater'):
  vals=[int(Decimal(str(x))*scale) for x in (.3,.5,.7,.75,.8,.85,.9,.95,1,1.05,1.1,1.2,1.25,1.5,2)]
  nums(f,vals,set(int(Decimal(str(x))*scale) for x in (.7,.85,.95,1,1.05,1.2)))
  for lo,hi in [(.7,.85),(.85,1),(.95,1.05),(1,1.2),(1.15,1.25)]:rng(f,int(Decimal(str(lo))*scale),int(Decimal(str(hi))*scale))
 for f in ('samewater','water_prev_keep','water_prev_reset','water_init','returnwater_keep','returnwater_reset','water_from_min','water_from_max','other_samewater'):
  nums(f,[(scale*x//100) for x in (-30,-20,-15,-10,-5,0,5,10,15,20,30)],set((scale*x//100) for x in (-15,-10,10,15)) if f in ('samewater','water_prev_keep') else set())
 for f in ('line_from_min','line_from_max','line_prev_keep','line_prev_reset'):
  nums(f,[-4,-3,-2,-1,0,1,2,3,4],set())
 for lo,hi in [(0,2),(2,4),(4,8),(6,10),(8,12),(12,14)]:rng('absline' if ah else 'line',lo,hi)
 if live:
  for lo,hi in [(0,16),(16,31),(31,46),(0,46),(46,61),(61,76),(76,91),(46,91),(0,91)]:rng('minute',lo,hi)
  add('minute','eq',-2)
  for lo in range(0,81,5):rng('minute',lo,lo+10,level=1)
  for f,vals in [('goal_diff',[-3,-2,-1,0,1,2,3]),('abs_diff',[0,1,2,3]),('total_goals',[0,1,2,3,4,5])]:
   for v in vals:
    add(f,'eq',v,level=2 if v in (-1,0,1) else 1)
   nums(f,vals,{0,1} if f!='goal_diff' else {-1,1})
  if ah:
   for x in (-2,-1,0,1,2):add('role_diff','eq',x,level=1)
  for x in sorted(set(arrays['score_code'][arrays['score_code']!=MISSING])):add('score_code','eq',x,level=1)
 else:
  add('status','eq',0);add('status','eq',1)
 for mode in ('keep','reset'):
  for c in (11,12,21,22):add('linepath2_'+mode,'eq',c)
  for k in (1,2,3):
   for bits in product(range(1,5),repeat=k):
    code=int(''.join(map(str,bits)));add(f'path{k}_{mode}','eq',code,level=2)
    # Ordinary state AND pulse paths participate in the finite core pair/triple space.
    add(f'path{k}_{mode}','eq',code,level=2,pulse=True)
    if k==3:
     for span in (15,30,45):add(f'path{k}_{mode}','eq',code,level=0,pulse=True,span=span)
     for span in (15,30,45):
      for line_step,water_pct in ((1,5),(2,10)):
       kw={}
       if any(b in (1,2) for b in bits):kw['min_line_step']=line_step
       if any(b in (3,4) for b in bits):kw['min_water_step']=scale*water_pct//100
       add(f'path{k}_{mode}','eq',code,level=0,pulse=True,span=span,**kw)
 if live:
  from .feature_extensions import WINDOWS
  for lo,hi in WINDOWS:
   for kind,values in [('line_init',(-4,-3,-2,-1,0,1,2,3,4)),('water_init',tuple(scale*x//100 for x in (-20,-15,-10,-5,0,5,10,15,20))),('otherwater_init',tuple(scale*x//100 for x in (-15,-5,5,15)))]:
    for value in values:
     for op in ('le','ge'):add(f'window_{lo}_{hi}_{kind}',op,value,level=0)
 if profile=='expanded':
  # New fine atoms are evaluated unary AND with one/two finite-core contexts by TaskPlan.
  for f in ('water','otherwater','samewater','water_init'):
   v=arrays[f][arrays[f]!=MISSING]
   if not v.size:continue
   step=max(1,scale//100)
   if (int(v.max())-int(v.min()))//step>3000:raise ValueError('扩展网格超过3000个阈值，请审查数据，不自动截断')
   for x in range((int(v.min())//step)*step,int(v.max())+1,step):
    for op in ('eq','le','ge'):add(f,op,x,level=0)
 # All registered finite-core atoms participate up to order three, irrespective of profit.
 tidx=pidx[:]
 return atoms,pidx,tidx

def tasks(atoms,pairs,triples):
 # A complete deterministic lexicographic expression space. No profit-based pruning.
 yield ()
 for i in range(len(atoms)):yield (i,)
 yield from combinations(pairs,2)
 yield from combinations(triples,3)

def rule_id(direction,conds):
 return direction+'_'+digest([{k:v for k,v in a.items() if k!='label'} for a in conds])[:24]
