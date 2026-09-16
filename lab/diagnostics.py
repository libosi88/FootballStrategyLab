"""Final-roster collision/risk diagnostics, not statistical independence certification."""
from itertools import combinations
import numpy as np
from .common import csv_write,atomic_json

def observation_sets(events,labels=None,directions=(),policy=None):
    """Per direction, the matches where it could have triggered: the same evaluable-quote rule as the sample gate."""
    from .research_standard import direction_matches
    return direction_matches(events,labels if labels is not None else {e['sid']:{'eligible':True} for e in events},directions,policy)

def export_selected_diagnostics(folder,selected,backup,scale,years,events=None,labels=None,trade_index=0,policy=None):
    byid={r['id']:r for r in [*selected,*backup]}; rows=[]
    # trade_index 4 is the historical basis (pre-goal rejected fills removed); validation keeps scenario 0.
    universe=sorted({t['sid'] for r in byid.values() for t in r['_trades'][trade_index]})
    stats={}
    for rid,r in byid.items():
        trades=r['_trades'][trade_index]; by={t['sid']:t for t in trades}
        stats[rid]=(by,{(t['sid'],t['market'],t['phase'],t['eid'],t['side'],t['line'],t['water']) for t in trades})
    observed=observation_sets(events,labels,sorted({r['direction'] for r in byid.values()}),policy) if events is not None else None
    for a,b in combinations(sorted(byid),2):
        aa,ae=stats[a];bb,be=stats[b];sa=set(aa);sb=set(bb);both=sa&sb;union=sa|sb
        # Empty dates are not used to certify low dependence; union and common active are separate.
        def available(r):return observed[r['direction']]
        jointly=available(byid[a])&available(byid[b]) if observed is not None else both
        active=sorted(jointly&union)
        x=np.array([aa[k]['pnl'] if k in aa else 0 for k in active],dtype=float)
        y=np.array([bb[k]['pnl'] if k in bb else 0 for k in active],dtype=float)
        corr=float(np.corrcoef(x,y)[0,1]) if len(active)>1 and np.std(x)>0 and np.std(y)>0 else None
        rows.append({'策略A':a,'策略B':b,'A场数':len(sa),'B场数':len(sb),'共同比赛':len(both),
          'A被B覆盖率':len(both)/len(sa) if sa else None,'B被A覆盖率':len(both)/len(sb) if sb else None,
          '比赛Jaccard':len(both)/len(union) if union else None,'相同报价合约':len(ae&be),
          '共同比赛同市场同侧':sum(aa[k]['market']==bb[k]['market'] and aa[k]['side']==bb[k]['side'] for k in both),
          '共同亏损场数':sum(aa[k]['pnl']<0 and bb[k]['pnl']<0 for k in both),
          '共同可观察比赛':len(jointly),'共同可观察活跃并集':len(active),'资料不足并集场数':len(union-jointly),
          '活跃并集收益相关':corr,'说明':'仅共同可观察范围；确认可观察而未触发才记0；不是统计独立证明' if corr is not None else '共同可观察样本不足或零方差，不解释为独立/零风险'})
    csv_write(folder/'主备名单重叠与共同亏损.csv',rows,['策略A','策略B','A场数','B场数','共同比赛','A被B覆盖率','B被A覆盖率','比赛Jaccard','相同报价合约','共同比赛同市场同侧','共同亏损场数','共同可观察比赛','共同可观察活跃并集','资料不足并集场数','活跃并集收益相关','说明'])
    atomic_json(folder/'重叠诊断范围.json',{'rules':len(byid),'pairs_evaluated':len(rows),'match_union':len(universe),
       'scope':'default_and_low_risk_roster_union_only','all_pool_pairwise_done':False,
       'effect':'descriptive evidence; does not silently change roster','selection_bias_adjusted':False})
