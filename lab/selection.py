"""Descriptive research selection; it never authorizes live bets."""
from pathlib import Path
from collections import defaultdict,OrderedDict
import numpy as np, csv, copy
from math import erfc
from .common import *
from .rules import rule_id, mask
from .mining import first_indices,Paused
from .portfolio_search import PortfolioSearch
from .portfolio_replay import MatchReplayScorer,ReplayUnsupported
from .research_standard import direction_availability,required_matches,research_gate_reasons,segment_shortfalls,portfolio_feasible,decision_metrics
from .contracts import event_contract, bind_rule
from .review_cache import load_review,save_review
from .history_policy import portfolio_qualifies



def _metrics(trades,scale,years,segments=None):
 """Descriptive per-scenario metrics. Calendar years are report columns; research segments
 (end-anchored 12-month windows) carry the time-coverage and concentration gates."""
 if type(scale) is not int or scale<=0:raise ValueError('水位刻度必须为正整数，不能作为零分母')
 ordered=sorted(trades,key=lambda x:(x['sort_time'],x['sid'],x['ts'],x['eid']))
 p=np.array([x['pnl'] for x in ordered],dtype=np.int64);den=2*scale;n=len(p)
 keys=sorted(set(segments or ())|{t['segment'] for t in ordered if 'segment' in t})
 if not n:return {'n':0,'net_i':0,'drawdown_i':0,'denominator':den,'net':0.,'roi':None,'drawdown':0.,'streak':0,'streak_push_break':0,'underwater':0,'worst20':None,'worst50':None,'year_counts':{str(y):0 for y in years},'year_net':{str(y):0. for y in years},'remove_top5':0.,'remove_best_year':0.,'segment_counts':{s:0 for s in keys},'segment_net':{s:0. for s in keys},'remove_best_segment':None,'pnl_sd':None,'z':None,'p_one_sided':None,'outcome_counts':{'win':0,'half_win':0,'push':0,'half_loss':0,'loss':0,'unclassified':0},'stake_total':0,'max_streak_loss':0.,'max_streak_push_break_loss':0.}
 curve=np.r_[np.int64(0),np.cumsum(p,dtype=np.int64)];peaks=np.maximum.accumulate(curve);dd=int(np.max(peaks-curve));run=brun=longest=blongest=uw=maxuw=0
 for i,z in enumerate(p):
  if z<0:run+=1;brun+=1
  elif z>0:run=brun=0
  else:brun=0
  longest=max(longest,run);blongest=max(blongest,brun)
  uw=uw+1 if curve[i+1]<peaks[i+1] else 0;maxuw=max(maxuw,uw)
 yc={str(y):sum(t['year']==y for t in ordered) for y in years}
 yn={str(y):sum(t['pnl'] for t in ordered if t['year']==y)/den for y in years}
 win=np.sort(p[p>0]);net=int(p.sum())/den
 result={'n':n,'net_i':int(p.sum()),'drawdown_i':dd,'denominator':den,'net':net,'roi':net/n,'drawdown':dd/den,'streak':longest,'streak_push_break':blongest,'underwater':maxuw,
 'year_counts':yc,'year_net':yn,'remove_top5':(int(p.sum())-int(win[-5:].sum()))/den,'remove_best_year':net-max(yn.values(),default=0)}
 for k in (20,50):result['worst'+str(k)]=int(np.min(curve[k:]-curve[:-k]))/den if n>=k else None
 outcomes={'win':0,'half_win':0,'push':0,'half_loss':0,'loss':0,'unclassified':0}
 for t in ordered:
  v=t['pnl'];w=t.get('water')
  key='push' if v==0 else 'loss' if v==-2*scale else 'half_loss' if v==-scale else 'win' if w is not None and v==2*w else 'half_win' if w is not None and v==w else 'unclassified'
  outcomes[key]+=1
 loss=broken_loss=worst_loss=worst_broken_loss=0
 for v in p:
  if v<0:loss-=int(v);broken_loss-=int(v)
  elif v>0:loss=broken_loss=0
  else:broken_loss=0
  worst_loss=max(worst_loss,loss);worst_broken_loss=max(worst_broken_loss,broken_loss)
 result.update(outcome_counts=outcomes,outcome_classification_status='PASS' if not outcomes['unclassified'] else 'UNCLASSIFIED_PRESENT',positive_trade_count=len(win),remove_top5_removed_count=min(5,len(win)),remove_top5_sample_status='AT_LEAST_FIVE_WINNERS' if len(win)>=5 else 'FEWER_THAN_FIVE_WINNERS',stake_total=sum(t.get('stake',1) for t in ordered),max_streak_loss=worst_loss/den,max_streak_push_break_loss=worst_broken_loss/den)
 result['roi']=net/result['stake_total'] if result['stake_total'] else None
 # Whole calendar days between the first and last match day, so kickoff clock times cannot decide a boundary.
 result['history_days']=max(0,int(ordered[-1]['sort_time']//1440-ordered[0]['sort_time']//1440))
 sc={s:0 for s in keys};sn={s:0 for s in keys}
 for t in ordered:
  if 'segment' in t:sc[t['segment']]+=1;sn[t['segment']]+=t['pnl']
 result.update(segment_counts=sc,segment_net={s:v/den for s,v in sn.items()},remove_best_segment=(int(p.sum())-max(sn.values()))/den if len(sn)>=2 else None)
 if n>=2 and float(np.std(p,ddof=1))>0:
  sd=float(np.std(p/den,ddof=1));z=float(np.mean(p/den))/(sd/n**.5)
  result.update(pnl_sd=sd,z=z,p_one_sided=0.5*erfc(z/2**.5))
 else:result.update(pnl_sd=None if n<2 else 0.0,z=None,p_one_sided=None)
 return result

def metrics(trades,scale,years,segments=None):
 """Preserve legacy statistics and add exact segment evidence for qualification."""
 trades=list(trades);result=_metrics(trades,scale,years,segments)
 nets={s:0 for s in result.get('segment_counts',{})}
 for trade in trades:
  if 'segment' in trade:nets[trade['segment']]=nets.get(trade['segment'],0)+int(trade['pnl'])
 result['segment_net_i']=nets;result.setdefault('history_days',0)
 positive=sum(max(0,v) for v in nets.values());best=max(nets.values(),default=0)
 result['best_segment_positive_profit_share']=best/positive if positive else None
 result['remove_best_segment_retention']=(result['net_i']-best)/result['net_i'] if len(nets)>1 and result['net_i']>0 else None
 result['return_drawdown_status']='NO_OBSERVED_DRAWDOWN' if result['n'] and not result['drawdown_i'] else 'DEFINED' if result['drawdown_i'] else 'NO_ORDERS'
 return result

class Quotes:
 def __init__(self,events,labels,scale,stale,cache_entries=100000,*,minute_close=False):
  if type(scale) is not int or scale<=0:raise ValueError('水位刻度必须为正整数')
  self.minute_close=minute_close;self.events=events;self.labels=labels;self.scale=scale;self.stale=stale;self.groups=defaultdict(list)
  for e in events:self.groups[(e['sid'],e['market'],e['phase'])].append(e['eid'])
  self.times={};self.position={}
  for key,ids in self.groups.items():
   ids.sort(key=lambda i:(events[i]['ts'],events[i]['row']));self.times[key]=np.array([events[i]['ts'] for i in ids],dtype=np.int64)
   for at,i in enumerate(ids):self.position[i]=at
  self.cache=OrderedDict();self.cache_entries=cache_entries
 def remember(self,key,value):
  self.cache[key]=value
  if len(self.cache)>self.cache_entries:self.cache.popitem(last=False)
  return value
 def pregoal_rejected(self,quote_eid,execution_ts):
  """Archive-only rejection: after a live fill the same match market closes and shows a
  different score within one full minute. The order is rejected and never refilled.
  A blank live score is not evidence of a goal; the last known score up to the fill is the base."""
  v=self.events[int(quote_eid)]
  if v['phase']!=1:return False
  ids=self.groups[(v['sid'],v['market'],v['phase'])];closed=changed=False;position=self.position[int(quote_eid)]
  known=next((self.events[ids[at]]['score'] for at in range(position,-1,-1) if self.events[ids[at]]['score'][0]!=MISSING),None)
  if known is None:return False
  for at in range(position+1,len(ids)):
   u=self.events[ids[at]]
   if u['ts']>execution_ts+1:break
   closed=closed or bool(u.get('closed',False))
   changed=changed or (u['score'][0]!=MISSING and u['score']!=known)
   if closed and changed:return True
  return False
 def trade(self,eid,side,delay=0,reduce=0,reject_pregoal=False):
  key=(int(eid),int(side),delay,reduce,bool(reject_pregoal))
  if key in self.cache:
   self.cache.move_to_end(key);return self.cache[key]
  e=self.events[int(eid)];v=e
  if delay or self.minute_close:
   g=(e['sid'],e['market'],e['phase']);ids=self.groups[g];pos=int(np.searchsorted(self.times[g],e['ts']+delay,side='right'))-1
   if pos<0:return self.remember(key,None)
   v=self.events[ids[pos]]
   if not v['valid'] or v['phase']!=e['phase'] or v['line']!=e['line'] or e['ts']+delay-v['ts']>self.stale:
    return self.remember(key,None)
   if e['market']==1 and v['score']!=e['score']:
    return self.remember(key,None) # remaining-goal contract baseline must not drift
  lab=self.labels[e['sid']]
  if not lab['eligible'] or not v['valid']:return self.remember(key,None)
  w=v['water'][int(side)]-reduce
  if w<=0:return self.remember(key,None)
  margin=sum(lab['final']) if v['market']==0 else lab['final'][0]-lab['final'][1]-(v['score'][0]-v['score'][1] if v['phase'] else 0)
  if v['market']==1 and v['phase'] and v['score'][0]==MISSING:return self.remember(key,None)
  if reject_pregoal and self.pregoal_rejected(v['eid'],e['ts']+delay):return self.remember(key,None)
  t={'sid':e['sid'],'mid':e['mid'],'eid':v['eid'],'signal_eid':e['eid'],'ts':e['ts']+delay,'quote_ts':v['ts'],
     'line':v['line'],'side':int(side),'water':w,'score':v['score'],'market':v['market'],'phase':v['phase'],'year':lab['year'],'segment':lab.get('segment',''),
     'sort_time':lab['kickoff'] if lab['kickoff']!=MISSING else timestamp(lab['date']+' 12:00'),
     'pnl':settlement(v['line'],w,margin,int(side),self.scale),'stake':1,'quality':int(v['phase']==1 and v['score'][0]!=MISSING and any(v['score'][i]>lab['final'][i] for i in (0,1)))}
  return self.remember(key,t)
 def trades(self,eids,sides,delay=0,reduce=0,reject_pregoal=False):
  return [x for i,s in zip(eids,sides) if (x:=self.trade(int(i),int(s),delay,reduce,reject_pregoal)) is not None]

PREGOAL_REJECTION_POLICY='explicit_live_close_and_score_change_within_one_minute_v2'
SCENARIO_NAMES=('分钟末执行原价','同分钟减水0.05','延后1分钟减水0.05','延后2分钟减水0.05','进球前报价拒单')

def scenario_count(config):
 """Standard review adds the pre-goal rejection scenario; legacy profiles keep four."""
 return 5 if config.get('profile')=='standard' else 4

def priced_scenario_count(policy=None,config=None):
 """How many scenarios the frozen policy prices. Prefer the packet over the job profile."""
 if policy is not None:return 5 if policy.get('pregoal_rejection')==PREGOAL_REJECTION_POLICY else 4
 return scenario_count(config or {})

def dispatch(roster,scenario,cap,keep_rejections=False,priority_mode='strategy_id_lexical'):
 """One first slot per named direction, match cumulative cap, same-minute batch."""
 offers=[]
 if priority_mode not in ('strategy_id_lexical','frozen_rule_priority'):raise ValueError('未知执行优先级政策')
 key=(lambda r:(r.get('_execution_priority',r.get('priority',0)),r['id'])) if priority_mode=='frozen_rule_priority' else (lambda r:r['id'])
 for pri,r in enumerate(sorted(roster,key=key)):
  priority=r.get('_execution_priority',r.get('priority',pri)) if priority_mode=='frozen_rule_priority' else pri
  for t in r['_trades'][scenario]:offers.append({**t,'strategy_id':r['id'],'direction':r['direction'],'priority':priority})
 offers.sort(key=lambda x:(x['ts'],x['sid'],x['priority'],x['eid']))
 orders=[];reject=[];used=set();stake=defaultdict(int);contracts=set();i=0
 while i<len(offers):
  j=i+1
  while j<len(offers) and (offers[j]['ts'],offers[j]['sid'])==(offers[i]['ts'],offers[i]['sid']):j+=1
  batch=offers[i:j];opp=defaultdict(set)
  def slot(t):
   if priority_mode!='frozen_rule_priority' or t['market']==0:return t['direction']
   prefix='PRE' if t['phase']==0 else 'LIVE'
   if t['line']==0:return prefix+('_PK_HOME' if t['side']==0 else '_PK_AWAY')
   giving=t['side']==(0 if t['line']>0 else 1)
   return prefix+('_GIVE' if giving else '_RECEIVE')
  for t in batch:
   if (t['sid'],slot(t)) not in used:opp[t['market']].add(t['side'])
  for t in batch:
   dkey=(t['sid'],slot(t));quote=t.get('quote_ts',t['ts']) if priority_mode=='frozen_rule_priority' else t['eid']
   contract=(t['sid'],t['market'],t['phase'],quote,t['side'],t['line'],t['water'],tuple(t['score']) if t['market']==1 else ())
   reason=''
   if dkey in used:reason='同方向首单已占用'
   elif contract in contracts:reason='相同报价合约重复'
   elif len(opp[t['market']])>1:reason='同分钟同市场相反信号'
   elif stake[t['sid']]>=cap:reason='整场模拟投入达上限'
   else:used.add(dkey);contracts.add(contract);stake[t['sid']]+=1;orders.append(t)
   if reason and keep_rejections:reject.append({**t,'reason':reason})
  i=j
 return orders,reject

def portfolio_metrics(orders,scale,years):
 m=metrics(orders,scale,years);by=defaultdict(list)
 for t in orders:by[t['sid']].append(t)
 aggregate=[{**v[0],'pnl':sum(t['pnl'] for t in v)} for v in by.values()]
 a=metrics(aggregate,scale,years)
 m.update(matches=len(by),drawdown_match_i=a['drawdown_i'],drawdown_match=a['drawdown'],max_match_stake=max(map(len,by.values()),default=0),worst_match_net=min((t['pnl']/(2*scale) for t in aggregate),default=0))
 segment_matches=defaultdict(int)
 for sid,rows in by.items():
  if len({r.get('segment','') for r in rows})!=1:raise ValueError('同一比赛的研究段不一致')
  segment_matches[rows[0].get('segment','')]+=1
 m['segment_match_counts']=dict(sorted(segment_matches.items()))
 by_day=defaultdict(int)
 for t in orders:by_day[t['sort_time']//1440]+=t['pnl']
 cumulative=peak=dd=0
 for day in sorted(by_day):
  cumulative+=by_day[day];peak=max(peak,cumulative);dd=max(dd,peak-cumulative)
 m.update(drawdown_day_i=dd,drawdown_day=dd/(2*scale),drawdown_risk_basis='match_order')
 return m

def row_report(r):
 # Standard rules qualify on minute-close executable prices, so those are the headline columns.
 raw=r['metrics'];m=r.get('execution_metrics') or raw;s=r.get('stress',[]);pg=r.get('pregoal_rejection') or {};pm=pg.get('metrics') or {}
 return {'候选ID':r['id'],'方向':DIRECTIONS[r['direction']],'完整条件':' AND '.join(a['label'] for a in r['conditions']) or '无附加条件',
 '收益口径':('分钟末历史净胜（剔除进球前拒单）' if m.get('price_basis')=='historical_minute_close_pregoal_rejected' else '分钟末执行原价') if r.get('execution_metrics') else '触发瞬间研究报价',
 '历史覆盖天数':m.get('history_days',0),
 '场次':m['n'],'净胜':m['net'],'ROI':m['roi'],'最大回撤_比赛排序':m['drawdown'],'最大连亏_忽略走盘':m['streak'],
 '触发瞬间研究场次':raw['n'],'触发瞬间研究净胜':raw['net'],'触发瞬间研究回撤':raw['drawdown'],'触发瞬间研究连亏':raw['streak'],
 '进球前报价拒单笔数':pg.get('rejected_n'),'拒单移除净胜':pg.get('rejected_net'),'拒单情景净胜':pm.get('net'),'拒单情景回撤':pm.get('drawdown'),
 '年份场次':canonical(m['year_counts']),'年份净胜':canonical(m['year_net']),'最差20笔':m['worst20'],'最差50笔':m['worst50'],
 '已知质量旗标':r.get('quality',0),'三情景最低净胜':min((x['net'] for x in s),default=None),
 '状态':r['status'],'原因':r.get('reason',''),'逐笔签名':r.get('signature',''),
 '实际本金':m.get('stake_total',m['n']),'全赢':m.get('outcome_counts',{}).get('win'),'半赢':m.get('outcome_counts',{}).get('half_win'),'走盘':m.get('outcome_counts',{}).get('push'),
 '半输':m.get('outcome_counts',{}).get('half_loss'),'全输':m.get('outcome_counts',{}).get('loss'),'未分类结算':m.get('outcome_counts',{}).get('unclassified',0),'结算分类状态':m.get('outcome_classification_status','NO_TRADES'),'走盘打断最大连亏':m['streak_push_break'],
 '忽略走盘最大连亏损失':m.get('max_streak_loss'),'走盘打断最大连亏损失':m.get('max_streak_push_break_loss'),'最长未恢复笔数':m['underwater'],
 '去掉最盈利5笔净胜':m['remove_top5'],'实际移除盈利笔数':m.get('remove_top5_removed_count',0),'盈利笔数':m.get('positive_trade_count',0),'移除5笔样本状态':m.get('remove_top5_sample_status','NO_TRADES'),'去掉最好一年净胜':m['remove_best_year'],'规则条件数':len(r['conditions']),
 '研究段场次':canonical(m.get('segment_counts',{})),'研究段净胜':canonical(m.get('segment_net',{})),'去掉最好研究段净胜':m.get('remove_best_segment'),
 '收益回撤比':m['net']/m['drawdown'] if m.get('drawdown') else None,'z值':m.get('z'),'单侧p值_未校正':m.get('p_one_sided')}

def second_quote_indices(hit,mids):
 """Fixed quote-loss diagnostic; never select by later profit or final score."""
 seen={};chosen=[]
 for i in np.flatnonzero(hit):
  mid=int(mids[i]);seen[mid]=seen.get(mid,0)+1
  if seen[mid]==2:chosen.append(i)
 return np.asarray(chosen,np.int64)

NEIGHBOR_MINUTES=5
NEIGHBOR_WATER_PCT=5

def neighbors(a,scale):
 """Registered perturbations with a real difference: water ±0.05, line ±0.25, minute ±5, integer ±1.
 A ±0.01 water step matched almost the same matches and was a near-certain pass."""
 f=a['feature'];water_step=max(1,scale*NEIGHBOR_WATER_PCT//100)
 if f.startswith(('path','linepath')):
  out=[]
  from .rules import label
  if f.startswith('path'):
   b=copy.deepcopy(a);b['pulse']=not a.get('pulse',False);b['label']=label(b,scale);out.append(b)
  b=copy.deepcopy(a);b['feature']=f.replace('_keep','_reset') if f.endswith('_keep') else f.replace('_reset','_keep');b['label']=label(b,scale);out.append(b)
  for key,delta in (('span',NEIGHBOR_MINUTES),('min_line_step',1),('min_water_step',water_step)):
   if key not in a:continue
   for step in (-delta,delta):
    if a[key]+step<(0 if key=='span' else 1):continue
    b=copy.deepcopy(a);b[key]+=step
    b['label']=label(b,scale);out.append(b)
  return out
 from .standard_atoms import feature_kind
 kind=feature_kind(f)
 if f=='minute':steps=[-NEIGHBOR_MINUTES,NEIGHBOR_MINUTES]
 elif kind=='line':steps=[-1,1]
 elif kind=='water':steps=[-water_step,water_step]
 elif kind=='integer':steps=[-1,1]
 else:return []
 out=[]
 for step in steps:
  if f=='minute' and a['value']<0:continue # text half-time has no numeric neighbourhood
  b=copy.deepcopy(a);b['value']+=step
  if 'upper' in b:b['upper']+=step
  if f=='minute' and b['value']<0:continue # numeric minute comparisons never extend into half-time
  if b['op']=='eq' and (f in ('minute','absline','abs_diff','total_goals','stoppage_added','stoppage_total','cross_age') or f.endswith('_from_current_min')) and b['value']<0:continue
  if b['op']=='eq' and f.endswith('_from_current_max') and b['value']>0:continue
  if f in ('line_same','line_return_keep','line_return_reset'):continue
  from .rules import label
  b['label']=label(b,scale);out.append(b)
 return out

def select(jobdir,events,labels,scale,config,update,should_pause):
 if config.get('profile')=='standard':
  from .standard_review import select_standard
  return select_standard(jobdir,events,labels,scale,config,update,should_pause)
 dest=Path(jobdir)/'results';dest.mkdir(exist_ok=True);years=sorted({v['year'] for v in labels.values() if v['year'] and v.get('eligible',False)})
 audit=read_json(Path(jobdir)/'data_audit.json',{})
 atomic_json(dest/'数据证据分层.json',audit.get('result_evidence',{'status':'LEGACY_LABEL_EVIDENCE_NOT_RECORDED'}))
 quotes=Quotes(events,labels,scale,config['stale_minutes'],config.get('quote_cache_entries',100000),minute_close=historical_objective(config));discount=scaled('0.05',scale)
 candidates=[];qualified=[];stresslog=[];seen_sig={};direction_summary=[];single_stresslog=[]
 # All profitable expressions are reviewed, not merely top K. Only exact-equivalent baseline stats cache.
 csvpath=dest/'全部候选审查.csv'
 fieldnames=list(row_report({'id':'','direction':'PRE_OVER','conditions':[],'metrics':metrics([],scale,years),'status':''}))
 with csvpath.open('w',encoding='utf-8-sig',newline='') as fo:
  writer=csv.DictWriter(fo,fieldnames=fieldnames);writer.writeheader()
  for direction in config['directions']:
   if should_pause():raise Paused()
   root=Path(jobdir)/'mining'/direction;spec=read_json(root/'dictionary.json');state=read_json(root/'state.json');atoms=spec['atoms']
   if not atoms:direction_summary.append({'方向':DIRECTIONS[direction],'direction':direction,'候选数':0,'资格数':0,'已选':0});continue
   checkpoint=Path(jobdir)/'review_checkpoints'/direction
   binding=digest({'version':VERSION,'code':source_fingerprint(),'config':config,'state':state,
                   'prepared':read_json(Path(jobdir)/'prepared_manifest.json'),'dictionary':digest(spec)})
   cached=load_review(checkpoint,binding)
   if cached is not None:
    writer.writerows(cached['rows']);qualified.extend(cached['qualified'])
    stresslog.extend(cached['neighbors']);single_stresslog.extend(cached['stress'])
    candidates.extend({'id':x['候选ID'],'direction':direction,'status':x['状态'],'net':x['净胜']} for x in cached['rows'])
    direction_summary.append(cached['summary'])
    update(message='已核验并恢复该方向完整筛选断点',direction=direction,reviewed=len(cached['rows']))
    continue
   direction_rows=[];qstart=len(qualified);nstart=len(stresslog);sstart=len(single_stresslog)
   with np.load(root/'arrays.npz',allow_pickle=False) as z:arr={k:z[k] for k in z.files}
   count=qualified_count=scanned=0
   profit_threshold=money_threshold(config['min_profit'],2*scale)
   availability=direction_availability(events,labels,[direction],{})[direction];segments=list(availability)
   for part in state['parts']:
    with np.load(root/part['file'],allow_pickle=False) as z:comb=z['conditions'];ms=z['metrics']
    for ids,stat in zip(comb,ms):
     scanned+=1
     if scanned%128==1:
      if should_pause():raise Paused()
      update(message='全池逐条登记与压力检查',direction=direction,reviewed=count,review_scanned=scanned)
     if stat[1]<=profit_threshold:continue
     ids=[int(x) for x in ids if x>=0];ix=first_indices(ids,arr,atoms);base=quotes.trades(arr['eid'][ix],arr['side'][ix])
     if (len(ix) if historical_objective(config) else len(base))!=int(stat[0]) or sum(t['pnl'] for t in base)!=int(stat[1]):raise ValueError('候选账与首次触发复算不一致')
     conditions=[atoms[i] for i in ids];rid=rule_id(direction,conditions)
     signature=digest([(t['eid'],t['side']) for t in base]);m=metrics(base,scale,years,segments)
     r={'id':rid,'direction':direction,'conditions':conditions,'metrics':m,'signature':signature,'quality':sum(t['quality'] for t in base),'status':'观察','reason':''}
     count+=1
     tr=[quotes.trades(arr['eid'][ix],arr['side'][ix],d,discount) for d in (0,1,2)]
     sm=[metrics(t,scale,years,segments) for t in tr];r['stress']=sm
     # The same scale-aware sample, segment, return/drawdown, concentration and stress gates as the standard review.
     reasons,_=research_gate_reasons(base,m,sm,config,scale,availability)
     for scen,ss in enumerate([m,*sm]):
      single_stresslog.append({'候选ID':rid,'情景':scen,'场次':ss['n'],'净胜':ss['net'],'回撤':ss['drawdown'],'收益回撤比':ss['net']/ss['drawdown'] if ss['drawdown'] else None,'最大连亏':ss['streak'],'保留率':ss['n']/max(1,m['n']),'年份场次':canonical(ss['year_counts']),'研究段场次':canonical(ss.get('segment_counts',{})),'最小年度场次':min(ss['year_counts'].values(),default=0),'已知质量旗标':sum(t['quality'] for t in [base,*tr][scen])})
     if not reasons:
      # Share the same single/joint/semantic diagnostics with standard review.
      from .standard_review import diagnostic_variants
      failed=False;ntest=0
      diagnostics=not historical_objective(config) or config.get('historical_diagnostics',False)
      for mode,ai,cs in (diagnostic_variants(conditions,scale) if diagnostics else ()):
        if should_pause():raise Paused()
        bm=np.ones(len(arr['eid']),dtype=bool)
        for aa in cs:bm &= mask(aa,arr)
        hit=np.flatnonzero(bm)
        if len(hit):hit=hit[np.r_[True,arr['mid'][hit[1:]]!=arr['mid'][hit[:-1]]]]
        ts=quotes.trades(arr['eid'][hit],arr['side'][hit],0,discount);nm=metrics(ts,scale,years);ntest+=1
        stresslog.append({'候选ID':rid,'类型':mode,'变动原子':ai,'变体':' AND '.join(a['label'] for a in cs),'场次':nm['n'],'减水净胜':nm['net'],'状态':'PASS' if nm['n'] and nm['net']>0 else 'NO_SAMPLE' if not nm['n'] else 'FAIL'})
        if not nm['n'] or nm['net']<=0:failed=True
      if diagnostics and not ntest:
       bm=np.ones(len(arr['eid']),dtype=bool)
       for a in conditions:bm &= mask(a,arr)
       chosen=second_quote_indices(bm,arr['mid'])
       nm=metrics(quotes.trades(arr['eid'][chosen],arr['side'][chosen],0,discount),scale,years)
       status='PASS' if nm['n'] and nm['net_i']>0 else 'NO_SAMPLE' if not nm['n'] else 'FAIL'
       stresslog.append({'候选ID':rid,'类型':'first_quote_missing','变动原子':-1,'变体':'首次满足报价丢失，使用第二次满足报价；减水0.05','场次':nm['n'],'减水净胜':nm['net'],'状态':status});failed=status!='PASS'
      if failed and not historical_objective(config):reasons.append('已登记单项/联合/语义扰动存在非正或无样本')
     if not reasons:
      r['status']='模拟比较资格';qualified_count+=1;r['_trades']=[base,*tr]
      # Same history can share selection economics only; each condition retains direct tests and identity.
      if (direction,signature) not in seen_sig:
       qualified.append(r);seen_sig[(direction,signature)]=rid
      else:r['status']='同历史备选';r['reason']='同方向同逐笔代表 '+seen_sig[(direction,signature)]
     else:r['reason']='；'.join(reasons)
     report=row_report(r);writer.writerow(report);direction_rows.append(report);candidates.append({'id':rid,'direction':direction,'status':r['status'],'net':m['net']})
   ds={'方向':DIRECTIONS[direction],'direction':direction,'候选数':count,'资格数':qualified_count,'已选':0,'方向可用场次':sum(availability.values()),'所需场次':required_matches(config,sum(availability.values()))}
   direction_summary.append(ds)
   save_review(checkpoint,binding,ds,rows=direction_rows,qualified=qualified[qstart:],neighbors=stresslog[nstart:],stress=single_stresslog[sstart:])
 csv_write(dest/'直接邻域测试.csv',stresslog)
 csv_write(dest/'单条报价压力.csv',single_stresslog)
 return finish_selection(jobdir,events,labels,scale,config,update,should_pause,qualified,len(candidates),direction_summary,stresslog,single_stresslog)

def describe_alternative(current, alternative, criterion, current_sids, alternative_sids, scale):
 """Separate best-in-alternative-pool ranking from improvement over the incumbent."""
 added=len(alternative_sids-current_sids);removed=len(current_sids-alternative_sids)
 if criterion=='覆盖不同':
  return {'对照类型':'覆盖不同' if added or removed else '覆盖相同','比较指标':'覆盖比赛数',
   '当前值':len(current_sids),'备选值':len(alternative_sids),'差值_备选减当前':len(alternative_sids)-len(current_sids),
   '相对当前':'仅描述覆盖，不判定优劣','是否优于当前':None,
   '备选池排名依据':'同方向同样本门槛备选中新增覆盖最多；同分优先场次较多',
   '新增覆盖比赛':added,'减少覆盖比赛':removed}
 # Compare the prices that decide qualification: minute-close execution and every priced scenario.
 head=lambda r:r.get('execution_metrics') or r['metrics']
 worst=lambda r:min([m['net_i'] for m in r['stress']]+([r['pregoal_rejection']['metrics']['net_i']] if r.get('pregoal_rejection') else []))
 if criterion in ('压力净胜较高','历史净胜较高'):
  historical=criterion=='历史净胜较高'
  metric='历史净胜' if historical else '最差压力净胜';a=head(current)['net_i'] if historical else worst(current);b=head(alternative)['net_i'] if historical else worst(alternative);den=2*scale;better=b>a
  pool_rank='同方向同样本门槛备选中历史净胜最高' if historical else '同方向同样本门槛备选中最差压力净胜最高'
 elif criterion=='历史连亏较低':
  metric='原价最大连亏';a=head(current)['streak'];b=head(alternative)['streak'];den=1;better=b<a
  pool_rank='同方向同样本门槛备选中连亏最少；同分优先场次较多'
 elif criterion=='历史回撤较低':
  metric='原价最大回撤';a=head(current)['drawdown_i'];b=head(alternative)['drawdown_i'];den=2*scale;better=b<a
  pool_rank='同方向同样本门槛备选中回撤最低；同分优先场次较多'
 else:raise ValueError('未知替代对照维度: '+criterion)
 return {'对照类型':criterion if better else metric+'未优于当前','比较指标':metric,
  '当前值':a if den==1 else a/den,'备选值':b if den==1 else b/den,'差值_备选减当前':b-a if den==1 else (b-a)/den,
  '相对当前':'较优' if better else '持平' if b==a else '较差','是否优于当前':better,
  '备选池排名依据':pool_rank,'新增覆盖比赛':added,'减少覆盖比赛':removed}


def finish_selection(jobdir,events,labels,scale,config,update,should_pause,qualified,candidate_count,direction_summary,stresslog,single_stresslog,comparison_pool=None,review_complete=True):
 dest=Path(jobdir)/'results';dest.mkdir(exist_ok=True);years=sorted({v['year'] for v in labels.values() if v['year'] and v.get('eligible',False)})
 fieldnames=list(row_report({'id':'','direction':'PRE_OVER','conditions':[],'metrics':metrics([],scale,years),'status':''}))
 priority_mode='frozen_rule_priority' if config.get('profile')=='standard' else 'strategy_id_lexical'
 update(stage='S5',message='执行方向内及全局加入/替换/移除比较',review_complete=review_complete)
 # All directly qualified historical representatives remain eligible in global local-search.
 # Direction winners are additional starts, NOT a top-K filter on the global pool.
 scenario_total=scenario_count(config)
 from .history_policy import portfolio_availability,portfolio_qualifies,segment_evidence,compare_roster_risk
 portfolio_scope=portfolio_availability(events,labels,config)
 def score_port(rs):
  # Every priced scenario, including pre-goal rejection for standard, shares the portfolio risk budget.
  oo=[dispatch(rs,j,config['match_cap'],priority_mode=priority_mode)[0] for j in range(scenario_total)]
  mm=[portfolio_metrics(x,scale,years) for x in oo]
  for m in mm:m['segment_availability']=portfolio_scope
  return {'raw':mm[0],'stress':mm[1:],'historical':mm[4] if len(mm)>4 else mm[0]}
 historical=historical_objective(config) and scenario_total>4
 def score_search(rs):
  # Historical decisions read only the pre-goal-rejected basis, so the search replays that scenario alone;
  # exported tables keep using score_port with every priced scenario.
  if not historical:return score_port(rs)
  m=portfolio_metrics(dispatch(rs,4,config['match_cap'],priority_mode=priority_mode)[0],scale,years)
  m['segment_availability']=portfolio_scope
  return {'raw':None,'stress':[],'historical':m}

 replay=None
 if historical and priority_mode=='frozen_rule_priority':
  # Exact match-by-match incremental replay of the decision scenario; pools it cannot represent keep full replays.
  try:replay=MatchReplayScorer(qualified,4,int(config['match_cap']),max_offers=int(config['max_mask_mb'])*1024*1024//256,segment_availability=portfolio_scope,scale=scale)
  except ReplayUnsupported:replay=None
 def search_scorer(availability=None):
  scope=portfolio_scope if availability is None else availability
  if replay is not None:
   scorer=replay.fresh();scorer.segment_availability=scope;return scorer
  def scorer(rs):
   value=score_search(rs)
   if value.get('historical') is not None:value['historical']['segment_availability']=scope
   return value
  return scorer
 checkpoint_root=Path(jobdir)/'selection_checkpoints'
 local_results=[];union=[]
 for ds in direction_summary:
  d=ds['direction'];pool=[r for r in qualified if r['direction']==d]
  local_scope=direction_availability(events,labels,[d],config)[d]
  solver=PortfolioSearch(pool,search_scorer(local_scope),config,scale,checkpoint_root/d,d,config['portfolio_min_return_drawdown_ratio'],update,should_pause)
  rr=solver.run();local_results.append(rr);union.extend(rr['chosen'])
  ds['已选']=len(rr['chosen']);ds['停止原因']=rr['status']
 main_search=PortfolioSearch(qualified,search_scorer(),config,scale,checkpoint_root/'GLOBAL','GLOBAL',config['portfolio_min_return_drawdown_ratio'],update,should_pause)
 main_result=main_search.run([tuple(sorted(set(union)))])
 backup_search=PortfolioSearch(qualified,search_scorer(),config,scale,checkpoint_root/'LOWER_RISK','LOWER_RISK',config['conservative_portfolio_min_return_drawdown_ratio'],update,should_pause)
 backup_result=backup_search.run([tuple(main_result['chosen'])])
 byid={r['id']:r for r in qualified}
 selected=[byid[i] for i in main_result['chosen']];backup=[byid[i] for i in backup_result['chosen']]
 for ds in direction_summary:
  ds['全局保留']=sum(r['direction']==ds['direction'] for r in selected)
  ds['低风险备选']=sum(r['direction']==ds['direction'] for r in backup)
 csv_write(dest/'方向汇总.csv',direction_summary)
 atomic_json(dest/'组合搜索状态.json',{'directions':local_results,'main':main_result,'backup':backup_result})
 # Every accepted set is risk-feasible. Missing comparisons remain explicitly partial.
 for x in (main_result,backup_result):
  if not x['score']['feasible']:raise RuntimeError('组合收益回撤比低于冻结研究要求')
 comparisons=[];contributions=[];contract=event_contract(events[0])
 policy={'id':'FSL_paper_first_direction_v03','match_cap':config['match_cap'],'slot_per_direction':1,
         'opposite':'reject_current_minute_market','priority':priority_mode,
         'quote_mapping':'minute_end_last_state_same_line_score','stale_minutes':config['stale_minutes'],
         'live_enabled':False,'second_slot_enabled':False}
 if config.get('profile')=='standard':policy.update(quote_mapping='minute_close_latest_v3',pregoal_rejection=PREGOAL_REJECTION_POLICY,scenario_names=list(SCENARIO_NAMES),research_config_hash=digest(config),research_objective=config.get('research_objective','validation'),feature_version='v3',cross_stale_minutes=config.get('cross_stale_minutes'),cross_stale_minutes_prematch=config.get('cross_stale_minutes_prematch'),cross_stale_basis=config.get('cross_stale_basis'),prematch_trigger_status=config.get('prematch_trigger_status'),search_grammar=config.get('search_grammar'),warmup='complete_match_history_required_before_any_first_signal',slot_scope='core_directions_with_fixed_side_aliases',contract_deduplication='quote_time_line_price_score_not_row_id')
 if config.get('profile')=='standard':
  policy['execution_code_hash']=execution_fingerprint()
  policy['historical_evidence_policy']={k:config[k] for k in HISTORY_POLICY_DEFAULTS if k in config}

 def export_variant(chosen,folder,risk_ratio):
  folder.mkdir(exist_ok=True);roster=[];goldens=[];blocked=[]
  for pri,r in enumerate(chosen):
   rule={k:v for k,v in r.items() if not k.startswith('_')}
   rule.update(priority=r.get('_execution_priority',pri),version=VERSION,semantics=SEMANTICS,water_scale=scale,
               stage='PRE' if r['direction'].startswith('PRE') else 'LIVE',
               live_enabled=False,second_slot_enabled=False)
   roster.append(bind_rule(rule,contract))
  name='默认' if folder==dest else '较低风险'
  selected_orders=[]
  caps={1,2,int(config['match_cap']),6,24}
  for cap in sorted(caps):
   for scen in range(scenario_total):
    orders,reject=dispatch(chosen,scen,cap,True,priority_mode);mm=portfolio_metrics(orders,scale,years)
    comparisons.append({'名单':name,'整场上限':cap,'情景':scen,'政策':('每核心方向首单，无额外整场约束' if priority_mode=='frozen_rule_priority' else '每命名方向首单，无额外整场约束') if cap==24 else f'整场最多{cap}单位',**mm})
    if cap==config['match_cap']:
     write_jsonl(folder/f'orders_s{scen}.jsonl.gz',orders);write_jsonl(folder/f'rejected_s{scen}.jsonl.gz',reject)
     selected_orders.append(orders)
     # Per-strategy blocking summary under the frozen execution policy (details stay in rejected_s*).
     for rule in chosen:
      reasons={}
      for x in reject:
       if x['strategy_id']==rule['id']:reasons[x['reason']]=reasons.get(x['reason'],0)+1
      blocked.append({'名单':name,'情景':scen,'候选ID':rule['id'],'方向':rule['direction'],'成交订单':sum(o['strategy_id']==rule['id'] for o in orders),'被拦截信号':sum(reasons.values()),'拦截原因':canonical(reasons)})
  from .research_validation import block_bootstrap
  uncertainty=block_bootstrap(selected_orders,labels,scale,config.get('bootstrap_repetitions',200),should_pause=should_pause)
  atomic_json(folder/'uncertainty.json',uncertainty)
  for scope,subset in (('只赛前',[r for r in chosen if r['direction'].startswith('PRE')]),('只滚球',[r for r in chosen if r['direction'].startswith('LIVE')]),('赛前与滚球共同执行',chosen)):
   for scen in range(scenario_total):
    orders,_=dispatch(subset,scen,config['match_cap'],priority_mode=priority_mode)
    comparisons.append({'名单':name,'整场上限':config['match_cap'],'情景':scen,'政策':scope,**portfolio_metrics(orders,scale,years)})
  packet={'version':VERSION,'scope':'FSL_STANDARD_V3_2' if config.get('profile')=='standard' else 'FSL_LOCAL_FINITE_V03','rules':roster,'contract':contract,
          'execution_policy':policy,'water_scale':scale,'portfolio_research_return_drawdown_ratio':str(risk_ratio),
          'live_enabled':False,'second_slot_enabled':False}
  packet['roster_hash']=digest({'rules':roster,'contract':contract,'execution_policy':policy})
  # Export every trigger's evaluated conditions, not just one example per rule.
  from .features import SignalEngine
  engine=SignalEngine(roster,contract=contract,execution_policy=policy);goldens=[];previous=None
  for event_index,e in enumerate(events):
   if event_index%512==0 and should_pause():raise Paused()
   if previous!=e['sid']:
    if previous is not None:engine.close_match(previous)
    engine.mark_history_complete(e['sid']);previous=e['sid']
   signals=engine.feed(e)
   if labels[e['sid']]['eligible']:goldens.extend(signals)
  if previous is not None:engine.close_match(previous)
  packet['golden_evidence_complete']=True
  packet['priced_scenario_count']=scenario_total
  packet['research_quote_note']='metrics保留触发瞬间研究指标；standard的execution_metrics、stress、orders_s0–s4（含进球前拒单）及minute_close_orders_s0–s4使用完整分钟末最新报价；流式协调器核对s0–s3，s4由冻结订单、档案分钟末订单与模拟账对照，无实际成交认证。' if config.get('profile')=='standard' else '旧有限档orders_s为触发瞬间研究报价，不与分钟末模拟或外部成交混称。'
  atomic_json(folder/'rules.json',packet);write_jsonl(folder/'golden_signals.jsonl.gz',goldens)
  # Independent trigger reference: each roster rule's batch research first triggers, not the online engine's own output.
  if all(hasattr(r,'archive') for r in chosen):
   write_jsonl(folder/'research_first_signals.jsonl.gz',[{'strategy_id':r['id'],'sid':t['sid'],'eid':t['signal_eid'],'side':t['side'],'ts':t['ts']} for r in chosen for t in r.archive.get(r['_trade_ref'])['research_raw']])
  csv_write(folder/'当前模拟名单.csv',[row_report(r) for r in chosen],fieldnames)
  from .reporting import write_cards
  write_cards(folder,packet)
  base=score_port(chosen)
  if historical:
   atomic_json(folder/'组合分段稳定性.json',segment_evidence(base['historical'],config,portfolio_scope,portfolio=True))
  for r in chosen:
   removed=score_port([x for x in chosen if x['id']!=r['id']])
   if historical:
    contributions.append({'名单':name,'候选ID':r['id'],'方向':r['direction'],
       '原历史净胜':base['historical']['net_i']/(2*scale),'移除后历史净胜':removed['historical']['net_i']/(2*scale),
       '移除贡献':(base['historical']['net_i']-removed['historical']['net_i'])/(2*scale),
       '原历史最大回撤':base['historical']['drawdown_match_i']/(2*scale),'移除后历史最大回撤':removed['historical']['drawdown_match_i']/(2*scale)})
    continue
   contributions.append({'名单':name,'候选ID':r['id'],'方向':r['direction'],
      '原最差压力净胜':min(m['net_i'] for m in base['stress'])/(2*scale),
      '移除后最差压力净胜':min(m['net_i'] for m in removed['stress'])/(2*scale),
      '移除贡献':(min(m['net_i'] for m in base['stress'])-min(m['net_i'] for m in removed['stress']))/(2*scale),
      '原含原价最大回撤':max(m['drawdown_match_i'] for m in [base['raw'],*base['stress']])/(2*scale),
      '移除后最大回撤':max(m['drawdown_match_i'] for m in [removed['raw'],*removed['stress']])/(2*scale)})
  # Addition contribution of every qualified strategy outside this roster: P(S+j)-P(S), fully replayed.
  additions=[];chosen_ids={x['id'] for x in chosen};adder=search_scorer() if historical else score_port
  if hasattr(adder,'rebase'):adder.rebase(chosen_ids)
  base_worst=min(m['net_i'] for m in decision_metrics(config,base)) if historical else min((m['net_i'] for m in base['stress']),default=base['raw']['net_i'])
  net_column='加入后历史净胜' if historical else '加入后最差压力净胜'
  for index,candidate in enumerate(x for x in qualified if x['id'] not in chosen_ids):
   if index%64==0 and should_pause():raise Paused()
   added=adder([*chosen,candidate])
   priced=decision_metrics(config,added);dd_i=max(m['drawdown_match_i'] for m in priced)
   worst_i=min(m['net_i'] for m in priced) if historical else min((m['net_i'] for m in added['stress']),default=added['raw']['net_i'])
   feasible=portfolio_feasible(min(m['net_i'] for m in priced),dd_i,risk_ratio) and all(portfolio_qualifies(m,config,risk_ratio,empty=not [*chosen,candidate]) for m in priced)
   additions.append({'名单':name,'候选ID':candidate['id'],'方向':candidate['direction'],net_column:worst_i/(2*scale),'加入贡献':(worst_i-base_worst)/(2*scale),'加入后最大回撤':dd_i/(2*scale),'风险约束内':feasible})
  csv_write(folder/'加入贡献.csv',additions,['名单','候选ID','方向',net_column,'加入贡献','加入后最大回撤','风险约束内'])
  csv_write(folder/'各策略被拦截数.csv',blocked,['名单','情景','候选ID','方向','成交订单','被拦截信号','拦截原因'])
  return roster

 main_roster=export_variant(selected,dest,config['portfolio_min_return_drawdown_ratio'])
 backup_roster=export_variant(backup,dest/'lower_risk',config['conservative_portfolio_min_return_drawdown_ratio'])
 if historical:
  risk_comparison=compare_roster_risk(score_port(selected)['historical'],score_port(backup)['historical'])
  atomic_json(dest/'主备实际风险比较.json',risk_comparison)
 csv_write(dest/'最终移除贡献.csv',contributions)
 csv_write(dest/'组合政策对照.csv',[{**x,'year_counts':canonical(x['year_counts']),'year_net':canonical(x['year_net'])} for x in comparisons])
 keepids={r['id'] for r in [*selected,*backup]}
 from .diagnostics import export_selected_diagnostics
 export_selected_diagnostics(dest,selected,backup,scale,years,events,labels,4 if historical else 0,config if config.get('profile')=='standard' else {})
 csv_write(dest/'入围直接邻域测试.csv',[x for x in stresslog if x['候选ID'] in keepids],['候选ID','变动原子','变体','场次','减水净胜','状态'])
 csv_write(dest/'入围单条报价压力.csv',[x for x in single_stresslog if x['候选ID'] in keepids],['候选ID','情景','场次','净胜','回撤','收益回撤比','最大连亏','保留率','年份场次','研究段场次','最小年度场次','已知质量旗标'])
 # Condensed, explicit alternatives for every chosen strategy. Full trials stay in checkpoints.
 alternative_pool_scope='达到样本门槛的全部盈利家族代表，可含风险失败者' if comparison_pool is not None else '仅通过旧规格筛选的代表，不含失败候选'
 availability=direction_availability(events,labels,config['directions'],config if config.get('profile')=='standard' else {})
 requirements={d:{'availability':a,'matches':required_matches(config,sum(a.values()))} for d,a in availability.items()}
 alternatives=build_alternative_comparisons([('默认',selected),('较低风险',backup)],comparison_pool if comparison_pool is not None else qualified,config,scale,score_port,pool_scope=alternative_pool_scope,requirements=requirements)
 csv_write(dest/'入围替代对照.csv',alternatives,['名单','候选ID','备选池范围','对照类型','比较指标','当前值','备选值','差值_备选减当前','相对当前','是否优于当前','备选池排名依据','替代ID','替代场次','替代最大连亏','替代原价回撤','新增覆盖比赛','减少覆盖比赛','替换后组合历史净胜' if historical else '替换后组合最差压力净胜','替换后组合最大回撤','风险约束内','备注'])
 summary={'alternative_pool_scope':alternative_pool_scope,'candidates':candidate_count,'qualified_historical_groups':len(qualified),'selected':len(selected),
  'backup_selected':len(backup),'directions':direction_summary,'comparisons':comparisons,
  'portfolio_default_return_drawdown_ratio':config['portfolio_min_return_drawdown_ratio'],'portfolio_backup_return_drawdown_ratio':config['conservative_portfolio_min_return_drawdown_ratio'],
  'selection_status':'FINITE_LOCAL_SEARCH_COMPLETE' if all(x['status']=='LOCAL_SEARCH_COMPLETE' for x in [*local_results,main_result,backup_result]) else 'PARTIAL_SELECTION_BUDGET',
  'selection_limitations':['固定起点下完整加入/移除/一换一局部邻域；不是数学全局最优',
   '全池覆盖与扰动执行状态以全池审查范围.json为准；条件重采样未校正大规模选择偏差',
   '全部名单来自全部选定历史内的选择，未做样本外评测；历史结果不代表未来或实盘表现' if historical else '全部名单来自样本内选择；名单冻结后才在留出的最近12个月上评测一次，见 样本外评测.json；仍不是未来或实盘验证'],
  'quality_status':'UPLOADED_LABEL_UNVERIFIED','live_enabled':False}
 summary['execution_code_hash']=execution_fingerprint()
 summary['historical_policy']={k:config[k] for k in HISTORY_POLICY_DEFAULTS if k in config}
 atomic_json(dest/'selection_summary.json',summary)
 if historical and config.get('complementarity_research',False):
  from .complementarity import study_pairs
  study=study_pairs(dest/'complementarity',comparison_pool if comparison_pool is not None else qualified,events,labels,config,scale,update,should_pause)
  summary['complementarity_study']={k:study[k] for k in ('status','planned_pairs','remaining_pairs','qualified_pairs','primary_roster_changed','execution_handoff_verified')}
  atomic_json(dest/'互补研究摘要.json',summary['complementarity_study'])
  atomic_json(dest/'selection_summary.json',summary)
 return summary


def build_alternative_comparisons(rosters,comparison_pool,config,scale,score_port,pool_scope='显式提供的可比代表池',requirements=None):
 """Rank the complete comparable pool before scoring at most four final alternatives."""
 alternatives=[];requirements=requirements or {}
 for roster_name,rs in rosters:
  # A same-history twin of any roster member trades exactly like that member, so it is never a replacement.
  # A pool without signatures stays comparable: an absent signature is unknown, not a match.
  roster_signatures={x['signature'] for x in rs if x.get('signature') is not None}
  for r in rs:
   # Rank and band on the executable prices that decide qualification (legacy rows fall back to research metrics).
   head=lambda x:x.get('execution_metrics') or x['metrics']
   worst=lambda x:head(x)['net_i'] if historical_objective(config) else min([m['net_i'] for m in x['stress']]+([x['pregoal_rejection']['metrics']['net_i']] if x.get('pregoal_rejection') else []))
   n0=head(r)['n']
   need=requirements.get(r['direction'])
   # Comparable alternatives meet the same direction-scaled sample and research-segment requirements.
   pool=(x for x in comparison_pool if x['direction']==r['direction'] and x['id']!=r['id'] and x.get('signature') not in roster_signatures and head(x)['n']>=(need['matches'] if need else config['min_matches']) and Decimal(config.get('alternative_sample_ratio_min','0.75'))*n0<=head(x)['n']<=Decimal(config.get('alternative_sample_ratio_max','2'))*n0 and not (need and segment_shortfalls(head(x),config,need['availability'])))
   opts={}
   basis=4 if historical_objective(config) and len(r['_trades'])>4 else 0
   current_sids={t['sid'] for t in r['_trades'][basis]}
   ranks={}
   for alternative in pool:
    alt_sids=set(alternative.get('coverage_sids',[])) if 'coverage_sids' in alternative else {t['sid'] for t in alternative['_trades'][basis if len(alternative['_trades'])>basis else 0]}
    h=head(alternative)
    candidates_ranks={('历史净胜较高' if historical_objective(config) else '压力净胜较高'):(worst(alternative),),
      '历史连亏较低':(-h['streak'],h['n']),
      '历史回撤较低':(-h['drawdown_i'],h['n']),
      '覆盖不同':(len(alt_sids-current_sids),h['n'])}
    for reason,rank in candidates_ranks.items():
     if reason not in ranks or rank>ranks[reason] or rank==ranks[reason] and alternative['id']<opts[reason]['id']:ranks[reason]=rank;opts[reason]=alternative
   for reason,alt in opts.items():
    mm=score_port([x for x in rs if x['id']!=r['id']]+[alt] if all(x['id']!=alt['id'] for x in rs) else [x for x in rs if x['id']!=r['id']])
    priced=decision_metrics(config,mm);dd_i=max(x['drawdown_match_i'] for x in priced);dd=dd_i/(2*scale)
    ratio=config['portfolio_min_return_drawdown_ratio'] if roster_name=='默认' else config['conservative_portfolio_min_return_drawdown_ratio']
    alt_sids=set(alt.get('coverage_sids',[])) if 'coverage_sids' in alt else {t['sid'] for t in alt['_trades'][basis if len(alt['_trades'])>basis else 0]}
    comparison=describe_alternative(r,alt,reason,current_sids,alt_sids,scale)
    alternatives.append({'名单':roster_name,'候选ID':r['id'],'备选池范围':pool_scope,**comparison,'替代ID':alt['id'],
     '替代场次':head(alt)['n'],'替代最大连亏':head(alt)['streak'],'替代原价回撤':head(alt)['drawdown'],
     **({'替换后组合历史净胜':min(x['net_i'] for x in priced)/(2*scale)} if historical_objective(config) else {'替换后组合最差压力净胜':min(x['net_i'] for x in mm['stress'])/(2*scale)}),'替换后组合最大回撤':dd,
     '风险约束内':portfolio_feasible(min(x['net_i'] for x in priced),dd_i,ratio) and all(portfolio_qualifies(x,config,ratio) for x in priced),'备注':'替代已在当前组合中，本行等同移除当前策略；不改名单' if any(x['id']==alt['id'] for x in rs) else '已实际重算替换后的组合；描述性对照不改名单'})
   if not opts:alternatives.append({'名单':roster_name,'候选ID':r['id'],'备选池范围':pool_scope,'备注':'当前可比池无其他同方向代表（受样本及比例带限制）；不是全空间没有替代'})
 return alternatives
