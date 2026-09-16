"""Minute-close historical discovery payoffs. Unfillable first signals remain in masks at zero payoff.

A live fill that the pre-goal rejection rule rejects (the same match market closes and shows a different
score within one full minute) could not have been executed either, so it also keeps a zero payoff and is
never replaced by a later signal."""
import numpy as np
from .common import MISSING,settlement

def pregoal_rejected_quotes(events):
    """Quote eids whose fill the pre-goal rule rejects; the same test as selection.Quotes.pregoal_rejected."""
    groups={}
    for e in events:
        if e['phase']==1:groups.setdefault((e['sid'],e['market']),[]).append(e)
    rejected=set()
    for quotes in groups.values():
        quotes.sort(key=lambda q:(q['ts'],q['row']))
        known=None
        for at,v in enumerate(quotes):
            # A blank live score is not evidence of a goal: compare with the last known score up to this quote.
            if v['score'][0]!=MISSING:known=v['score']
            if known is None:continue
            closed=changed=False
            for j in range(at+1,len(quotes)):
                u=quotes[j]
                if u['ts']>v['ts']+1:break
                # Missing or quality-invalid fields do not prove bookmaker closure.
                closed=closed or bool(u.get('closed',False))
                changed=changed or (u['score'][0]!=MISSING and u['score']!=known)
                if closed and changed:
                    rejected.add(v['eid']);break
    return rejected

def minute_close_payoffs(events,labels,scale,reject_pregoal=True):
    payoff=np.zeros((len(events),2),dtype=np.int64);latest={}
    rejected=pregoal_rejected_quotes(events) if reject_pregoal else frozenset()
    for e in reversed(events):
        key=(e['sid'],e['market'],e['phase']);v=latest.get(key)
        if v is None or v['ts']!=e['ts']:
            if v is not None and v['ts']<e['ts']:raise ValueError('历史收益数组要求每场每市场报价按时间排序')
            latest[key]=v=e
        label=labels[e['sid']]
        if not label['eligible'] or not v['valid'] or v['line']!=e['line']:continue
        if e['market']==1 and v['score']!=e['score']:continue
        if v['market']==1 and v['phase'] and v['score'][0]==MISSING:continue
        if v['eid'] in rejected:continue
        margin=sum(label['final']) if v['market']==0 else label['final'][0]-label['final'][1]-(v['score'][0]-v['score'][1] if v['phase'] else 0)
        for side in (0,1):
            if v['water'][side]>0:payoff[e['eid'],side]=settlement(v['line'],v['water'][side],margin,side,scale)
    return payoff
