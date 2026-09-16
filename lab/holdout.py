"""Pre-registered out-of-sample holdout.

S0 splits whole matches by label date before any feature domain, candidate or roster exists: the
latest 12 months never enter discovery, review or selection. After the roster is frozen and its
trigger replay verified, evaluate_holdout replays the frozen packets once on the held-out matches
with the same minute-close scenarios and match cap. Nothing measured here can change a roster.
"""
from pathlib import Path
from .common import *
from .research_standard import holdout_plan,holdout_verdict,assign_segments,segment_anchor,bonferroni

PLAN_FILE='holdout_plan.json'
HOLDOUT_FILES=('holdout/events.jsonl.gz','holdout/labels.json')

def split_holdout(events,labels,config):
    """Assign research segments and split off the holdout. Returns
    (discovery_events, discovery_labels, holdout_events, holdout_labels, plan); each part is renumbered in place."""
    plan=holdout_plan(labels,config)
    assign_segments(labels,plan.get('anchor') or segment_anchor(labels),config)
    plan['segment_basis']=config.get('segment_basis','end_anchored_365')
    plan['segment_anchor_date']=config.get('segment_anchor_date') or None
    if plan['status']!='SPLIT':return events,labels,[],{},plan
    later={sid for sid,l in labels.items() if l.get('date','')>plan['cutoff']}
    discovery=[e for e in events if e['sid'] not in later];held=[e for e in events if e['sid'] in later]
    for part in (discovery,held):
        for i,e in enumerate(part):e['eid']=i
    return discovery,{s:l for s,l in labels.items() if s not in later},held,{s:l for s,l in labels.items() if s in later},plan

def evaluate_holdout(jobdir,config,coverage=None,update=None,should_pause=None):
    root=Path(jobdir);results=root/'results';results.mkdir(exist_ok=True)
    plan=read_json(root/PLAN_FILE) or {'status':'NOT_RECORDED'}
    out={'schema':'FSL_HOLDOUT_EVALUATION_V1','plan':plan,'status':plan['status'],'rosters':{},
         'scope':'The latest months were held out before discovery and evaluated once after the roster was frozen. Retrospective archive holdout: stronger than in-sample, still not future or live evidence.'}
    if historical_objective(config):out['scope']='All selected historical matches are used for historical discovery and stability comparison; no future-performance claim or holdout gate.'
    rows=[]
    if plan['status']=='SPLIT':
        prepared=read_json(root/'prepared_manifest.json',{})
        for name in HOLDOUT_FILES:
            if name not in prepared or not (root/name).is_file() or sha(root/name)!=prepared[name]:raise ValueError('样本外留出数据缺失或改变: '+name)
        events=list(read_jsonl(root/HOLDOUT_FILES[0]));labels=read_json(root/HOLDOUT_FILES[1]);scale=read_json(root/'data_audit.json')['water_scale']
        from .research_validation import evaluate_frozen
        for name,folder in (('main',results),('lower_risk',results/'lower_risk')):
            packet=read_json(folder/'rules.json')
            if packet is None:continue
            if should_pause and should_pause():
                from .mining import Paused
                raise Paused()
            if update:update(stage='S6',message='名单冻结后在留出的最近12个月比赛上评测一次（不重新选择）',holdout_roster=name)
            report=evaluate_frozen(packet,events,labels,scale,config)
            verdict=holdout_verdict(packet,report,config);head=report['scenarios'][0] if report['scenarios'] else {}
            out['rosters'][name]={'verdict':verdict,'rules':len(packet['rules']),'roster_hash':packet.get('roster_hash'),'report':report}
            for i,m in enumerate(report['scenarios']):
                rows.append({'名单':'默认' if name=='main' else '较低风险','层级':'组合','情景':report['scenario_names'][i],'候选ID':'','方向':'','笔数':m['n'],'净胜':m['net'],'ROI':m['roi'],'按场回撤':m.get('drawdown_match'),'z值':m.get('z'),'单侧p值':m.get('p_one_sided'),'结论':verdict if i==0 else ''})
            for r in report.get('rules_s0',[]):
                rows.append({'名单':'默认' if name=='main' else '较低风险','层级':'单条（未经整场上限）','情景':report['scenario_names'][0],'候选ID':r['strategy_id'],'方向':r['direction'],'笔数':r['n'],'净胜':r['net'],'ROI':r['roi'],'按场回撤':r['drawdown'],'z值':r['z'],'单侧p值':r['p_one_sided'],'结论':''})
        out['status']=out['rosters'].get('main',{}).get('verdict','NOT_RUN')
    # Honest multiple-testing disclosure: in-sample significance corrected by the number of distinct signal classes searched.
    packet=read_json(results/'rules.json',{'rules':[]});tests=int((coverage or {}).get('evaluated') or 0);selected=[]
    for r in packet.get('rules',[]):
        m=r.get('execution_metrics') or {};p=m.get('p_one_sided')
        selected.append({'strategy_id':r['id'],'direction':r['direction'],'in_sample_z':m.get('z'),'in_sample_p_one_sided':p,'bonferroni_p':bonferroni(p,tests)})
    out['multiple_testing']={'evaluated_signal_classes':tests,'candidate_occurrences':(coverage or {}).get('candidates'),'selected_rules':selected,
        'significant_after_correction':sum(1 for x in selected if x['bonferroni_p'] is not None and x['bonferroni_p']<0.05),
        'method':'Bonferroni over distinct historical signal classes actually evaluated (a lower bound on the effective number of tests); normal approximation of mean per-order P&L.'}
    atomic_json(results/'样本外评测.json',out)
    csv_write(results/'样本外逐条.csv',rows,['名单','层级','情景','候选ID','方向','笔数','净胜','ROI','按场回撤','z值','单侧p值','结论'])
    return holdout_summary(out)

def holdout_summary(out):
    def roster(name):
        x=out['rosters'].get(name)
        if not x:return None
        head=x['report']['scenarios'][0] if x['report']['scenarios'] else {}
        return {'verdict':x['verdict'],'rules':x['rules'],'orders':head.get('n'),'net':head.get('net'),'roi':head.get('roi'),'z':head.get('z'),'p_one_sided':head.get('p_one_sided'),
                'worst_scenario_net':min((m['net'] for m in x['report']['scenarios']),default=None)}
    plan=out['plan']
    return {'status':out['status'],'plan':{k:plan.get(k) for k in ('status','policy','months','anchor','cutoff','discovery_matches','holdout_matches')},
            'main':roster('main'),'lower_risk':roster('lower_risk'),'multiple_testing':{k:out['multiple_testing'][k] for k in ('evaluated_signal_classes','significant_after_correction')},
            'file':'results/样本外评测.json'}
