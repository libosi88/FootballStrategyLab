"""Exact incremental joint replay for the historical portfolio search.

dispatch() keeps all of its state per match: direction slot, quote contract, stake cap and the same-minute
batch. A roster's orders in one match therefore depend only on the offers of the rules trading in that match.
Neighbouring rosters of the local search (add, remove, one-for-one replacement) differ in at most two rules, so
only those rules' matches are replayed again and every other match keeps its result.

Once a match reaches the stake cap every later offer is rejected. So a rule whose first offer in that match comes
after the cap was reached cannot change it, whether that rule is added or removed. The base roster's offers are
kept merged per match, so a neighbour is replayed from one sorted list.

The search reads three numbers of portfolio_metrics(dispatch(...)): net_i, drawdown_match_i and matches.
Matches are ordered by kickoff (sort_time) and sid, so the match-ordered drawdown is a cumulative sum over a
fixed order in which matches without orders add zero.
"""
import numpy as np
from .common import DIRECTIONS

OPEN=1<<62
SLOT_IDS={name:i for i,name in enumerate(DIRECTIONS)}


class ReplayUnsupported(ValueError):pass


def _slot(rule,t):
    """The frozen_rule_priority slot of dispatch(): named direction for totals, handicap role otherwise."""
    if t['market']==0:return rule['direction']
    prefix='PRE' if t['phase']==0 else 'LIVE'
    if t['line']==0:return prefix+('_PK_HOME' if t['side']==0 else '_PK_AWAY')
    return prefix+('_GIVE' if t['side']==(0 if t['line']>0 else 1) else '_RECEIVE')


def replay_offers(merged,cap):
    """dispatch() for one match over offers sorted by (ts, priority, eid, rank, index):
    (net pnl, any order, ts of the batch that reached the cap or OPEN)."""
    used=set();contracts=set();stake=pnl=0;n=len(merged);i=0
    while i<n:
        ts=merged[i][0];j=i+1
        while j<n and merged[j][0]==ts:j+=1
        if j==i+1:
            o=merged[i]
            if o[5] not in used and o[6] not in contracts:
                used.add(o[5]);contracts.add(o[6]);stake+=1;pnl+=o[9]
        else:
            sides={}
            for k in range(i,j):
                o=merged[k]
                if o[5] not in used:sides.setdefault(o[7],set()).add(o[8])
            for k in range(i,j):
                if stake>=cap:break
                o=merged[k]
                if o[5] in used or o[6] in contracts or len(sides.get(o[7],()))>1:continue
                used.add(o[5]);contracts.add(o[6]);stake+=1;pnl+=o[9]
        if stake>=cap:return pnl,True,ts
        i=j
    return pnl,stake>0,OPEN


class MatchReplayScorer:
    """Score rosters like portfolio_metrics(dispatch(roster,scenario,cap,priority_mode='frozen_rule_priority')[0]).

    Returns {'raw':None,'stress':[],'historical':{'net_i','drawdown_match_i','matches'}} as PortfolioSearch
    expects in historical mode. rebase(ids) names the roster whose neighbours follow."""
    method='FSL_match_incremental_replay_v2_segments'

    def __init__(self,pool,scenario,cap,max_offers=None,segment_availability=None,scale=100):
        if type(cap) is not int or cap<1:raise ReplayUnsupported('整场上限无效')
        def priority(r):
            value=r.get('_execution_priority',r.get('priority'))
            if value is None:raise ReplayUnsupported('规则缺少冻结执行优先级，改用逐单完整回放')
            return value
        self.cap=cap;self.offers={};self.rank={};kickoff={};contracts={};total=0;segments={}
        self.segment_availability=segment_availability;self.scale=scale
        # dispatch() lists rules by (priority, id), then stably sorts offers by (ts, sid, priority, eid);
        # (ts, priority, eid, rank, index) reproduces that order inside one match without ties.
        for rank,r in enumerate(sorted(pool,key=lambda r:(priority(r),r['id']))):
            if r['id'] in self.offers:raise ReplayUnsupported('规则编号重复')
            p=priority(r);trades=r['_trades'][scenario];per={}
            total+=len(trades)
            if max_offers is not None and total>max_offers:raise ReplayUnsupported('候选订单超过增量回放内存预算，改用逐单完整回放')
            for index,t in enumerate(trades):
                sid=t['sid']
                segment=t.get('segment','')
                if segments.setdefault(sid,segment)!=segment:raise ReplayUnsupported('同场研究段不一致')
                if kickoff.setdefault(sid,t['sort_time'])!=t['sort_time']:raise ReplayUnsupported('同一场比赛的排序时间不一致，改用逐单完整回放')
                slot=SLOT_IDS.get(_slot(r,t))
                if slot is None:raise ReplayUnsupported('未知方向槽位，改用逐单完整回放')
                local=contracts.setdefault(sid,{})
                contract=local.setdefault((t['market'],t['phase'],t.get('quote_ts',t['ts']),t['side'],t['line'],t['water'],tuple(t['score']) if t['market']==1 else ()),len(local))
                per.setdefault(sid,[]).append((t['ts'],p,t['eid'],rank,index,slot,contract,t['market'],t['side'],t['pnl']))
            self.offers[r['id']]={sid:tuple(sorted(v)) for sid,v in per.items()};self.rank[r['id']]=rank
        order=sorted(kickoff,key=lambda sid:(kickoff[sid],sid))
        self.position={sid:k for k,sid in enumerate(order)};self.size=len(order);self.offer_count=total
        self.days=np.array([kickoff[sid]//1440 for sid in order],dtype=np.int64)
        groups={}
        for sid in order:groups.setdefault(segments[sid],[]).append(self.position[sid])
        self.segment_indices={key:np.array(indices,dtype=np.intp) for key,indices in groups.items()}
        self.indices={rid:np.fromiter((self.position[sid] for sid in sids),np.intp,len(sids)) for rid,sids in self.offers.items()}
        self.base_ids=None;self.rebase(())

    def fresh(self):
        """Another search over (part of) the same pool: shared offers, its own base roster."""
        other=object.__new__(MatchReplayScorer)
        for name in ('cap','offers','rank','position','size','offer_count','indices','days','segment_indices','segment_availability','scale'):setattr(other,name,getattr(self,name))
        other.base_ids=None;other.rebase(());return other

    def replay(self,sid,members):
        if len(members)==1:return replay_offers(self.offers[members[0]][sid],self.cap)
        merged=[]
        for rid in members:merged.extend(self.offers[rid][sid])
        merged.sort();return replay_offers(merged,self.cap)

    def _members(self,ids):
        members={}
        for rid in ids:
            for sid in self.offers[rid]:members.setdefault(sid,[]).append(rid)
        return members

    def rebase(self,ids):
        ids=tuple(sorted(ids))
        if ids==self.base_ids:return
        merged={};closed={}
        P=np.zeros(self.size,np.int64);has=np.zeros(self.size,np.bool_)
        for sid,rids in self._members(ids).items():
            offers=[]
            for rid in rids:offers.extend(self.offers[rid][sid])
            offers.sort();merged[sid]=offers
            k=self.position[sid];P[k],has[k],closed[sid]=replay_offers(offers,self.cap)
        self.base_ids=ids;self.base_set=frozenset(ids);self.base_offers=merged;self.base_closed=closed
        self.base_P=P;self.base_has=has;self.added={};self.removed={}

    def _added(self,rid):
        hit=self.added.get(rid)
        if hit is None:
            idx=self.indices[rid];pnl=self.base_P[idx];has=self.base_has[idx];closed={}
            for k,(sid,offers) in enumerate(self.offers[rid].items()):
                base=self.base_offers.get(sid)
                if base is None:
                    pnl[k],has[k],closed[sid]=replay_offers(offers,self.cap);continue
                closed[sid]=self.base_closed[sid]
                # A match already at the stake cap before this rule's first offer keeps its result.
                if offers[0][0]>closed[sid]:continue
                merged=[*base,*offers];merged.sort()
                pnl[k],has[k],closed[sid]=replay_offers(merged,self.cap)
            hit=self.added[rid]=(idx,pnl,has,closed)
        return hit

    def _removed(self,rid):
        hit=self.removed.get(rid)
        if hit is None:
            idx=self.indices[rid];pnl=self.base_P[idx];has=self.base_has[idx];closed={};at={};rank=self.rank[rid]
            for k,(sid,offers) in enumerate(self.offers[rid].items()):
                at[sid]=k;closed[sid]=self.base_closed[sid]
                if offers[0][0]>closed[sid]:continue
                rest=[o for o in self.base_offers[sid] if o[3]!=rank]
                pnl[k],has[k],closed[sid]=replay_offers(rest,self.cap) if rest else (0,False,OPEN)
            hit=self.removed[rid]=(idx,pnl,has,closed,at)
        return hit

    def _result(self,P,has):
        curve=np.cumsum(P);peak=np.maximum.accumulate(curve);np.maximum(peak,0,out=peak)
        counts={key:int(np.count_nonzero(has[ix])) for key,ix in self.segment_indices.items()}
        counts={key:value for key,value in counts.items() if value}
        nets={key:int(P[self.segment_indices[key]].sum()) for key in counts}
        days=self.days[has]
        m={'net_i':int(curve[-1]) if self.size else 0,'drawdown_match_i':int((peak-curve).max()) if self.size else 0,
           'matches':int(np.count_nonzero(has)),'segment_net_i':nets,'segment_match_counts':counts,
           'history_days':int(days.max()-days.min()) if len(days) else 0,'denominator':2*self.scale}
        if self.segment_availability is not None:m['segment_availability']=self.segment_availability
        return {'raw':None,'stress':[],'historical':m}

    def __call__(self,rules):
        return self.score_ids([r['id'] for r in rules])

    def score_ids(self,ids):
        target=frozenset(ids)
        if len(target)!=len(ids):raise ValueError('组合含重复规则')
        added=target-self.base_set;removed=self.base_set-target
        if len(added)>1 or len(removed)>1:
            P=np.zeros(self.size,np.int64);has=np.zeros(self.size,np.bool_)
            for sid,rids in self._members(sorted(target)).items():
                k=self.position[sid];P[k],has[k],_=self.replay(sid,rids)
            return self._result(P,has)
        if not added and not removed:return self._result(self.base_P,self.base_has)
        P=self.base_P.copy();has=self.base_has.copy()
        if removed:
            (r,)=removed;idx,rest_pnl,rest_has,rest_closed,at=self._removed(r);P[idx]=rest_pnl;has[idx]=rest_has
        if added:
            (a,)=added;idx,add_pnl,add_has,add_closed=self._added(a);P[idx]=add_pnl;has[idx]=add_has
        if added and removed:
            offers_a=self.offers[a];offers_r=self.offers[r];rank_r=self.rank[r]
            # Matches where both rules trade currently hold the roster-plus-a result.
            for sid in offers_a.keys()&offers_r.keys():
                k=self.position[sid]
                if offers_a[sid][0][0]>rest_closed[sid]:
                    # Without r the match is capped before a's first offer, so a changes nothing.
                    j=at[sid];P[k]=rest_pnl[j];has[k]=rest_has[j]
                elif offers_r[sid][0][0]>add_closed[sid]:
                    # With a the match is capped before r's first offer, so removing r changes nothing.
                    continue
                else:
                    merged=[o for o in self.base_offers[sid] if o[3]!=rank_r];merged.extend(offers_a[sid]);merged.sort()
                    P[k],has[k],_=replay_offers(merged,self.cap)
        return self._result(P,has)
