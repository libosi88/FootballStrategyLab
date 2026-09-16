"""Export verifier: online row-at-a-time trigger vs batch conjunction references.
Feature semantics are shared: this is not independent external data verification.
"""
from pathlib import Path
import json, gc
from collections import Counter
from .common import *
from .features import SignalEngine
from .contracts import resolve_contract
from .selection import Quotes,dispatch,priced_scenario_count,PREGOAL_REJECTION_POLICY

KEYS=('strategy_id','direction','sid','eid','event_key','ts','side','line','water','score')
def signature(x):return canonical({k:x[k] for k in KEYS})

def verify_golden_evidence(expected,actual):
 # Legacy baseline rules have zero conditions and legitimately emit an empty evidence object.
 # Missing evidence and changed condition values still fail the exact replay comparison.
 if any(not isinstance(s.get('evidence'),dict) for s in expected):raise AssertionError('黄金信号缺少逐次条件证据')
 if sorted(canonical(s) for s in expected)!=sorted(canonical(s) for s in actual):raise AssertionError('逐次黄金信号条件证据与实际回放不一致')

def verification_passed(report):
 return report.get('status') in ('PASS','EMPTY_ROSTER') and (not report.get('execution_economics_required') or report.get('execution_economics',{}).get('status')=='PASS')

STANDARD_SCOPES=('FSL_STANDARD_V3_1','FSL_STANDARD_V3_2')

def verify_frozen_config(packet,config):
 expected=packet['execution_policy'].get('execution_code_hash')
 if expected is not None and expected!=execution_fingerprint():raise ValueError('触发包执行代码身份已改变，必须重新验收，不能沿用旧S6证据')
 if packet.get('scope') in STANDARD_SCOPES and packet['execution_policy'].get('pregoal_rejection')!=PREGOAL_REJECTION_POLICY:
  raise ValueError('标准冻结包必须声明进球前拒单政策，筛选与交接口径不能对齐')
 if packet['execution_policy'].get('quote_mapping')=='minute_close_latest_v3' and packet['execution_policy'].get('research_config_hash')!=digest(config):raise ValueError('冻结研究配置内容改变或缺少绑定，拒绝以新门槛复验旧名单')

def settle_execution_orders(orders,events,labels,scale):
 settled=[]
 for order in orders:
  q=events[order['eid']];lab=labels[q['sid']]
  margin=sum(lab['final']) if q['market']==0 else lab['final'][0]-lab['final'][1]-(q['score'][0]-q['score'][1] if q['phase'] else 0)
  settled.append({**order,'year':lab['year'],'stake':order.get('stake',1),'sort_time':lab['kickoff'] if lab['kickoff']!=MISSING else timestamp(lab['date']+' 12:00'),'pnl':settlement(q['line'],order['water'],margin,order['side'],scale)})
 return settled

def replay_scenario_orders(rules,by,quotes,scenario,cap,priority,scale):
 if scenario==4:
  roster=[{**r,'_trades':[[t for s in by[r['id']] if (t:=quotes.trade(s['eid'],s['side'],0,0,True)) is not None]]} for r in rules]
 else:
  delay=max(0,scenario-1);red=0 if scenario==0 else scaled('0.05',scale)
  roster=[{**r,'_trades':[[t for s in by[r['id']] if (t:=quotes.trade(s['eid'],s['side'],delay,red)) is not None]]} for r in rules]
 return dispatch(roster,0,cap,priority_mode=priority)[0]

def verify(root,events_path=None,variant=None,*,_shared_events=None,should_pause=None):
 def checkpoint():
  if should_pause and should_pause():
   from .mining import Paused
   raise Paused()
 checkpoint()
 root=Path(root);rulesfile=root/'results'/'rules.json' if (root/'results').exists() else root/'rules.json'
 if variant is not None:rulesfile=root/'results'/variant/'rules.json'
 data=read_json(rulesfile);rules=data['rules'];res=rulesfile.parent
 resolve_contract(rules,data.get('contract'))
 if data.get('roster_hash')!=digest({'rules':rules,'contract':data['contract'],'execution_policy':data['execution_policy']}):raise ValueError('名单与契约或执行政策内容哈希不符')
 # Reuse the same immutable input between default and backup verification; never nest full copies.
 events=_shared_events if _shared_events is not None else list(read_jsonl(events_path or root/'events.jsonl.gz'));labels=read_json(root/'labels.json')
 if not events and rules:raise ValueError('没有报价事件，不能验收非空名单')
 expected=list(read_jsonl(res/'golden_signals.jsonl.gz'))
 engine=SignalEngine(rules,contract=data['contract'],execution_policy=data['execution_policy']);got=[];mid=len(events)//2
 for sid in {e['sid'] for e in events}:engine.mark_history_complete(sid)
 first_samples={}
 for s in expected:first_samples.setdefault(s['strategy_id'],s['eid'])
 sample_eids=set(first_samples.values());replay_cases=[]
 prefix=[];previous_sid=None
 for i,e in enumerate(events):
  if i%512==0:checkpoint()
  if previous_sid is not None and e['sid']!=previous_sid:engine.close_match(previous_sid)
  previous_sid=e['sid']
  before=engine.snapshot() if e['eid'] in sample_eids else None
  sig=engine.feed(e)
  if before is not None:
   negative={**e,'valid':False,'closed':True}
   check=SignalEngine(rules,before,contract=data['contract'],execution_policy=data['execution_policy'])
   if check.feed(negative):raise AssertionError('封盘负样例产生信号')
   replay_cases.append({'eid':e['eid'],'state_before':before,'positive_event':e,'positive_signals':sig,'negative_event':negative,'negative_signals':[]})
  if labels[e['sid']]['eligible']:got+=sig
  if i==mid:
   state=json.loads(canonical(engine.snapshot()));engine=SignalEngine(rules,state,contract=data['contract'],execution_policy=data['execution_policy'])
   if engine.feed(e):raise AssertionError('重复推送不得发第二次信号')
   prefix=got[:]
 if previous_sid is not None:engine.close_match(previous_sid)
 a=sorted(map(signature,expected));b=sorted(map(signature,got))
 missing=list((Counter(a)-Counter(b)).elements());extra=list((Counter(b)-Counter(a)).elements())
 if data.get('golden_evidence_complete'):
  verify_golden_evidence(expected,got)
 # Golden signals come from the same online engine; the research first-trigger reference is the independent comparison.
 reference_path=res/'research_first_signals.jsonl.gz';research=None
 if reference_path.exists():
  identity=lambda s:(s['strategy_id'],s['sid'],s['eid'],s['side'])
  reference=Counter(map(identity,read_jsonl(reference_path)));online=Counter(map(identity,got))
  research={'scope':'batch research first triggers (TradeArchive research_raw) vs online row-at-a-time signals; shared feature definitions',
   'reference_signals':sum(reference.values()),'missing_signals':sum((reference-online).values()),'extra_signals':sum((online-reference).values())}
  research['status']='PASS' if not research['missing_signals'] and not research['extra_signals'] else 'FAIL'
 eng2=SignalEngine(rules,contract=data['contract'],execution_policy=data['execution_policy']);trunc=[]
 for sid in {e['sid'] for e in events}:eng2.mark_history_complete(sid)
 previous_sid=None
 for i,e in enumerate(events[:mid+1]):
  if i%512==0:checkpoint()
  if previous_sid is not None and e['sid']!=previous_sid:eng2.close_match(previous_sid)
  previous_sid=e['sid']
  sig=eng2.feed(e)
  if labels[e['sid']]['eligible']:trunc+=sig
 if previous_sid is not None:eng2.close_match(previous_sid)
 if sorted(map(signature,trunc))!=sorted(map(signature,prefix)):raise AssertionError('未来截断改变历史信号')
 # Calendar-time cutoff across all markets: future state cannot alter earlier signals.
 cutoff=sorted(e['ts'] for e in events)[mid] if events else 0
 pastengine=SignalEngine(rules,contract=data['contract'],execution_policy=data['execution_policy']);past=[]
 for sid in {e['sid'] for e in events}:pastengine.mark_history_complete(sid)
 previous_sid=None
 for i,e in enumerate(events):
  if i%512==0:checkpoint()
  if e['ts']<=cutoff:
   if previous_sid is not None and e['sid']!=previous_sid:pastengine.close_match(previous_sid)
   previous_sid=e['sid']
   ss=pastengine.feed(e)
   if labels[e['sid']]['eligible']:past+=ss
 if previous_sid is not None:pastengine.close_match(previous_sid)
 if Counter(map(signature,past))!=Counter(signature(x) for x in got if x['ts']<=cutoff):raise AssertionError('跨市场未来截断改变过去信号')
 # Build execution offers anew from online signals, not from saved research trades.
 cfg=read_json(root/'config.json');verify_frozen_config(data,cfg);quotes=Quotes(events,labels,data['water_scale'],cfg.get('stale_minutes',5),minute_close=data['execution_policy'].get('quote_mapping')=='minute_close_latest_v3');by={r['id']:[] for r in rules}
 for s in got:by[s['strategy_id']].append(s)
 order_diffs={};paper_reports={};priority=data['execution_policy'].get('priority','strategy_id_lexical');scale=data['water_scale']
 priced=priced_scenario_count(data['execution_policy'])
 from .paper_verification import verify_paper_orders
 from .selection import portfolio_metrics
 replayed={}
 for scenario in range(priced):
  orders=replay_scenario_orders(rules,by,quotes,scenario,cfg['match_cap'],priority,scale);replayed[scenario]=orders
  path=res/f'orders_s{scenario}.jsonl.gz';ref=list(read_jsonl(path)) if path.exists() else None
  left=Counter(map(canonical,orders));right=Counter(map(canonical,ref or []))
  order_diffs[str(scenario)]=sum((left-right).values())+sum((right-left).values()) if ref is not None else len(orders)+1
  if priority=='frozen_rule_priority' and (scenario<4 or ref is not None or data['execution_policy'].get('pregoal_rejection')==PREGOAL_REJECTION_POLICY):
   checkpoint();paper_reports[str(scenario)]=verify_paper_orders(data,got,events,quotes,scenario,ref or [],should_pause)
 stream_report={'status':'NOT_APPLICABLE','reason':'legacy execution policy'}
 if priority=='frozen_rule_priority':
  from .paper_verification import verify_stream_orders
  reports={};eligible=[e for e in events if labels[e['sid']]['eligible']];years=sorted({l['year'] for l in labels.values() if l['eligible']})
  for scenario in range(4):
   checkpoint();report=verify_stream_orders(data,got,eligible,scenario=scenario,should_pause=should_pause)
   settled=settle_execution_orders(report.pop('_orders'),events,labels,scale)
   report['metrics']=portfolio_metrics(settled,scale,years)
   write_jsonl(res/f'minute_close_orders_s{scenario}.jsonl.gz',settled)
   if scenario==0:write_jsonl(res/'minute_close_orders.jsonl.gz',settled)
   reports[str(scenario)]=report
  if data['execution_policy'].get('pregoal_rejection')==PREGOAL_REJECTION_POLICY:
   archived=replayed[4]
   write_jsonl(res/'minute_close_orders_s4.jsonl.gz',archived)
   reports['4']={'status':'PASS' if order_diffs.get('4')==0 and paper_reports.get('4',{}).get('status')!='FAIL' else 'FAIL',
    'scope':'archive pre-goal rejection; not replayed on StreamingPaperRunner','orders':len(archived),
    'metrics':portfolio_metrics(archived,scale,years),'source':'orders_s4.jsonl.gz + paper ledger','coordinator':'not_replayed',
    'differences':order_diffs.get('4',1),'durable_restarts':paper_reports.get('4',{}).get('durable_restart_retries',0),'real_orders_sent':0}
   stream_report_note='archive_priced_and_ledger_checked; coordinator_not_replayed'
  else:stream_report_note=None
  stream_report={**reports['0'],'scenarios':reports,'status':'PASS' if all(r['status']=='PASS' for r in reports.values()) else 'FAIL'}
  stream_report['screening_reference_orders']=len(list(read_jsonl(res/'orders_s0.jsonl.gz'))) if (res/'orders_s0.jsonl.gz').exists() else 0
  stream_report['screening_reference_quote_mapping']=data['execution_policy'].get('quote_mapping')
  stream_report['priced_scenario_count']=len(reports)
  if stream_report_note:stream_report['pregoal_rejection']=stream_report_note
 cases_path=res/'rule_replay_cases.jsonl.gz'
 if cases_path.exists() and digest(list(read_jsonl(cases_path)))!=digest(replay_cases):raise AssertionError('逐条规则样例与当前实际回放不符')
 write_jsonl(cases_path,replay_cases)
 covered_samples={s['strategy_id'] for case in replay_cases for s in case['positive_signals']}
 if not set(r['id'] for r in rules)<=covered_samples:raise AssertionError('入围规则缺少真实历史正样例')
 from .handoff_assets import verify_scalar_cases
 # The pipeline writes handoff assets before verification; a missing file must fail rather than be regenerated and checked against itself.
 if not (res/'boundary_cases.json').exists():raise ValueError('交接结果缺少逐条边界样例boundary_cases.json，不在验收时重新生成')
 boundaries=verify_scalar_cases(res)
 report={'engine_version':VERSION,'scope':'batch first-event masks vs row-at-a-time rules; shared feature implementation',
  'events':len(events),'rules':len(rules),'golden_signals':len(expected),'actual_signals':len(got),
  'missing_signals':len(missing),'extra_or_changed_signals':len(extra),'order_differences':order_diffs,
  'persistent_paper_execution':paper_reports,
  'streaming_paper_execution':stream_report,
  'research_first_trigger_reference':research or {'status':'NOT_AVAILABLE','reason':'旧交接或非标准档位没有导出批量研究首触发参考'},
  'rule_cases':{'status':'PASS','covered_rules':len(covered_samples),'positive_and_closed_negative_events':len(replay_cases),'scalar_boundaries':boundaries['scalar_boundaries'],'path_boundaries':boundaries.get('path_boundaries',0)},
  'restart_and_duplicate':'PASS' if events else 'NOT_APPLICABLE_EMPTY_INPUT','prefix_causality':'PASS' if events else 'NOT_APPLICABLE_EMPTY_INPUT','feature_input_has_no_terminal_labels':not any({'全场比分','半场比分','final','result_status'}&set(e) for e in events),
  'status':'PASS' if not missing and not extra and not any(order_diffs.values()) and all(x['status']!='FAIL' for x in paper_reports.values()) and stream_report['status']!='FAIL' and (research is None or research['status']=='PASS') else 'FAIL',
  'scope_limit':'未验证真实行情接口、成交、独立赛果、未见数据或完整v3覆盖。共享特征代码不是完全独立特征审计。'}
 if not rules:
  if report['status']=='PASS':report['status']='EMPTY_ROSTER'
  report['scope_limit']+=' 空名单不能证明非空策略触发验收。'
 from .execution_economics import verify_economics
 report['execution_economics']=verify_economics(data,by,quotes,labels,cfg,stream_report)
 report['execution_economics_required']=data.get('scope') in ('FSL_STANDARD_V3_1','FSL_STANDARD_V3_2')
 if variant is None and (root/'results'/'lower_risk'/'rules.json').exists():
  # Feature states and quotation caches are no longer needed; backup uses its own engine.
  del engine,eng2,pastengine,quotes
  if 'state' in locals():del state
  gc.collect()
  backup=verify(root,events_path,variant='lower_risk',_shared_events=events,should_pause=should_pause);report['lower_risk_verification']=backup
  if backup['status']=='FAIL':report['status']='FAIL'
  report['execution_economics']['lower_risk_status']=backup['execution_economics']['status']
  if report['execution_economics_required'] and backup['execution_economics']['status']!='PASS':
   report['execution_economics']['status']='FAIL'
   report['execution_economics']['reason']='较低风险备选未通过实际执行经济资格；主备均须通过后才可交接'
 atomic_json(res/'trigger_verification.json',report)
 return report
