"""Replay actual online signals through the durable, label-free paper ledger."""
from collections import Counter,defaultdict
from pathlib import Path
import tempfile
from .common import canonical,digest
from .paper_execution import PaperDispatcher

def verify_stream_orders(packet,signals,events,stale=5,scenario=0,should_pause=None):
    """Independent minute-close quote lookup vs the actual restartable coordinator.

    Trigger-instant research orders are a different scenario; this check does not
    relabel them as observed minute-close fills.
    """
    from .paper_runner import StreamingPaperRunner
    from .selection import dispatch
    from bisect import bisect_right
    latest=defaultdict(list);times=defaultdict(list)
    delay=max(0,scenario-1);reduction=0 if scenario==0 else packet['contract']['water_scale']//20
    ordered=sorted(events,key=lambda e:(e['sid'],e['ts'],e['row'],e['market'],e['phase']))
    for e in ordered:
        key=(e['sid'],e['market'],e['phase']);latest[key].append(e);times[key].append(e['ts'])
    by={r['id']:{**r,'_trades':[[]]} for r in packet['rules']}
    for s in signals:
        r=by[s['strategy_id']];market=0 if r['direction'].endswith(('OVER','UNDER')) else 1
        target=s['ts']+delay;phase=int(r['direction'].startswith('LIVE'));key=(s['sid'],market,phase)
        pos=bisect_right(times[key],target)-1
        if pos<0:continue
        q=latest[key][pos]
        if not q['valid'] or q['phase']!=phase or q['line']!=s['line'] or market==1 and q['score']!=s['score'] or target-q['ts']>packet['execution_policy'].get('stale_minutes',stale) or q['water'][s['side']]<=reduction:continue
        r['_trades'][0].append({'strategy_id':s['strategy_id'],'sid':s['sid'],'ts':target,'quote_ts':q['ts'],'eid':q['eid'],'market':market,'phase':phase,'side':s['side'],'line':q['line'],'water':q['water'][s['side']]-reduction,'score':q['score']})
    expected,_=dispatch(list(by.values()),0,packet['execution_policy']['match_cap'],priority_mode='frozen_rule_priority')
    actual=[];restarts=0
    with tempfile.TemporaryDirectory(prefix='fsl_stream_verify_') as td:
        path=Path(td)/'stream.sqlite3';runner=StreamingPaperRunner(path,packet,True,True,checkpoint_every=128,execution_delay=delay,water_reduction=reduction)
        try:
            for i,e in enumerate(ordered):
                if i%128==0 and should_pause and should_pause():
                    from .mining import Paused
                    raise Paused()
                runner.feed(e)
                if i==len(ordered)//2:
                    runner.close();runner=StreamingPaperRunner(path,packet,True,True,checkpoint_every=128,execution_delay=delay,water_reduction=reduction);restarts+=1
                    if runner.feed(e)['signals']:raise AssertionError('流式重启重复报价再次触发')
            if runner.last_sid is not None:runner.finish_match(runner.last_sid)
            for row in runner.dispatcher.conn.execute("SELECT body FROM intents WHERE filled>0 ORDER BY rowid"):
                b=__import__('json').loads(row['body']);q=b['quote']
                actual.append({'strategy_id':b['strategy_id'],'sid':q['sid'],'ts':b['execution_ts'],'eid':q['eid'],'side':b['side'],'line':q['line'],'water':b['water']})
        finally:runner.close()
    left=Counter(map(order_key,actual));right=Counter(map(order_key,expected));differences=sum((left-right).values())+sum((right-left).values())
    return {'status':'PASS' if differences==0 else 'FAIL','scope':'archive minute-close latest quote, actual StreamingPaperRunner feed/flush/reopen',
            'orders':len(actual),'differences':differences,'durable_restarts':restarts,'real_orders_sent':0,'delay_minutes':delay,'water_reduction_i':reduction,'_orders':actual}


def order_key(t):
    return canonical({k:t[k] for k in ('strategy_id','sid','ts','eid','side','line','water')})


def verify_paper_orders(packet,signals,events,quotes,scenario,reference,should_pause=None):
    if packet['execution_policy'].get('priority')!='frozen_rule_priority':
        return {'status':'NOT_APPLICABLE','reason':'legacy lexical dispatcher; standard ledger requires frozen priorities'}
    if scenario==4:delay,reduction,reject_pregoal=0,0,True
    elif 0<=scenario<4:delay,reduction,reject_pregoal=max(0,scenario-1),0 if scenario==0 else packet['water_scale']//20,False
    else:raise ValueError('模拟账对照只接受情景 0—4')
    batches=defaultdict(list)
    for signal in signals:
        trade=quotes.trade(signal['eid'],signal['side'],delay,reduction,reject_pregoal)
        if trade is not None:
            batches[(trade['ts'],trade['sid'])].append({'strategy_id':signal['strategy_id'],'signal':signal,
                'quote':events[trade['eid']],'execution_ts':trade['ts'],'water':trade['water']})
    actual=[];retries=0
    with tempfile.TemporaryDirectory(prefix='fsl_paper_verify_') as td:
        path=Path(td)/'ledger.sqlite3';ledger=PaperDispatcher(path,packet)
        try:
            for index,key in enumerate(sorted(batches)):
                if index%64==0 and should_pause and should_pause():
                    from .mining import Paused
                    raise Paused()
                result=ledger.submit_batch(batches[key])
                for row in result:
                    if row['status']!='RESERVED':continue
                    intent=ledger.receipt(row['intent_id'],'fill_'+row['intent_id'],'FILLED','1')
                    body=intent['body'];q=body['quote']
                    actual.append({'strategy_id':body['strategy_id'],'sid':q['sid'],'ts':body['execution_ts'],
                                   'eid':q['eid'],'side':body['side'],'line':q['line'],'water':body['water']})
                # Each persisted batch must remain idempotent across a real DB reopen.
                if index%64==0 or index==len(batches)-1:
                    before=ledger.usage(key[1]);ledger.close();ledger=PaperDispatcher(path,packet)
                    ledger.submit_batch(list(reversed(batches[key])));retries+=1
                    if ledger.usage(key[1])!=before:raise AssertionError('重启重试改变模拟本金占额')
        finally:ledger.close()
    left=Counter(map(order_key,actual));right=Counter(map(order_key,reference))
    differences=sum((left-right).values())+sum((right-left).values())
    return {'status':'PASS' if differences==0 else 'FAIL','orders':len(actual),'differences':differences,
            'durable_restart_retries':retries,'actual_orders_sha256':digest(sorted(left.elements())),
            'terminal_labels_entered_dispatcher':False,'real_orders_sent':0}
