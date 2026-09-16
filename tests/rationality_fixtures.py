"""Small legal quotes with explicit schedules; no external research inputs."""
from datetime import date,timedelta
from lab.common import timestamp
from lab.contracts import bind_rule
from lab.features import SignalEngine
from lab.research_standard import assign_segments
from lab.selection import Quotes
from test_core import event,TEST_CONTRACT


def scheduled_rules(schedules,directions,minutes,config):
    events=[];labels={}
    for year,schedule in sorted(schedules.items()):
        for offset,outcomes in enumerate(schedule):
            day=(date(year,1,1)+timedelta(days=offset)).isoformat()
            sid=f'synthetic-{year}-{offset:03d}';mid=len(labels);kickoff=timestamp(day+' 12:00')
            labels[sid]={'eligible':True,'final':[2,1],'year':year,'date':day,
                         'kickoff':kickoff,'result_status':'完','league':TEST_CONTRACT['league']}
            for index,win in sorted(outcomes.items(),key=lambda item:minutes[item[0]]):
                total=directions[index].endswith(('OVER','UNDER'))
                line=(10 if win else 14) if total else (2 if win else 6)
                e=event(len(events),line=line,ts=kickoff+minutes[index])
                e.update(sid=sid,mid=mid,market=0 if total else 1,
                         minute=minutes[index],minute_raw=str(minutes[index]))
                events.append(e)
    assign_segments(labels,config=config)
    rules=[bind_rule({'id':f'r{i}','direction':d,'priority':i,
                      'conditions':[{'feature':'minute','op':'eq','value':minutes[i]}]},TEST_CONTRACT)
           for i,d in enumerate(directions)]
    engine=SignalEngine(rules,contract=TEST_CONTRACT,
                        execution_policy={'feature_version':'v3','cross_stale_minutes':5})
    for sid in labels:engine.mark_history_complete(sid)
    signals=[]
    for e in events:signals.extend(engine.feed(e))
    quotes=Quotes(events,labels,100,5,minute_close=True)
    for i,r in enumerate(rules):
        trades=[t for s in signals if s['strategy_id']==r['id']
                if (t:=quotes.trade(s['eid'],s['side'],reject_pregoal=True)) is not None]
        r['_trades']=[trades]*5;r['_execution_priority']=i;r['tags']=[]
    return events,labels,rules


def losing_portfolio(config):
    first=[{0:False,1:True}]*10+[{0:True}]*12+[{1:False}]*6
    later=[v for _ in range(10) for v in ({0:True,1:False},{1:True})]
    later += [{1:True}]*8+[{0:True}]*12
    return scheduled_rules({2022:first,2023:later,2024:later},['LIVE_GIVE','LIVE_GIVE'],[5,20],config)


def complementary_portfolio(config):
    schedule=[{0:True,1:False}]*20+[{0:False,1:True}]*20+[{0:True,1:True}]*10
    return scheduled_rules({y:schedule for y in (2022,2023,2024)},['LIVE_GIVE','LIVE_OVER'],[5,5],config)
