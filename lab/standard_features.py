"""Additional v3 causal semantics, shared by selected-rule streaming replay.

The search backend supplies independent column/mask implementations. Nothing in
this module reads terminal labels, future quotes or an unverified kickoff time.
"""
from copy import deepcopy
import re
from .common import MISSING,canonical,digest
from .features import FeatureStream

def predicate_key(atom):return 'predicate_'+digest({k:v for k,v in atom.items() if k!='label'})

def minute_parts(raw):
    m=re.fullmatch(r'(45|90)\+(\d+)',str(raw).strip())
    if m:return int(m[1]),int(m[2]),int(m[1])+int(m[2])
    return MISSING,MISSING,MISSING

def sign(value):return 1 if value>0 else -1 if value<0 else 0

def path_events(history,side,composite=False):
    """One quote is one event even when both price attributes changed."""
    out=[]
    for previous,current in zip(history,history[1:]):
        dl=current['line']-previous['line'];dw=current['water'][side]-previous['water'][side]
        lc=1 if dl>0 else 2 if dl<0 else 0;wc=3 if dw>0 else 4 if dw<0 else 0
        code=lc*10+wc if composite and lc and wc else lc or wc
        if code:out.append((code,current['ts'],abs(dl),abs(dw),current['event_key']))
    return out

def path_matches(history,side,atom,current_key):
    composite=atom.get('event_model')=='composite'
    pattern=tuple(atom.get('pattern',tuple(int(x) for x in str(atom['value']))))
    steps=path_events(history,side,composite)
    if len(steps)<len(pattern):return False
    def valid(step,code):
        actual,t,dl,dw,key=step
        if actual!=code:return False
        codes={actual} if actual<10 else {actual//10,actual%10}
        return (not (codes&{1,2}) or dl>=atom.get('min_line_step',1)) and (not (codes&{3,4}) or dw>=atom.get('min_water_step',1))
    if atom.get('sequence','contiguous')=='contiguous':
        tail=steps[-len(pattern):]
        return all(valid(step,code) for step,code in zip(tail,pattern)) and (not atom.get('pulse') or tail[-1][4]==current_key) and ('span' not in atom or tail[-1][1]-tail[0][1]<=atom['span'])
    # Latest possible start for each prefix dominates an earlier start under an
    # upper span bound. Reverse updates prevent a quote from satisfying two steps.
    starts=[None]*len(pattern);matched=False
    for position,step in enumerate(steps):
        for i in range(len(pattern)-1,-1,-1):
            if not valid(step,pattern[i]):continue
            start=step[1] if i==0 else starts[i-1]
            if start is None or ('span' in atom and step[1]-start>atom['span']):continue
            starts[i]=max(starts[i],start) if starts[i] is not None else start
            if i==len(pattern)-1 and position==len(steps)-1 and (not atom.get('pulse') or step[4]==current_key):matched=True
    return matched

def path_evidence(history,side,atom,current_key):
    """Return concrete distinct quote events witnessing the selected predicate."""
    steps=path_events(history,side,atom.get('event_model')=='composite')
    pattern=tuple(atom.get('pattern',tuple(int(x) for x in str(atom['value']))));k=len(pattern)
    if atom.get('sequence','contiguous')=='contiguous':
        witness=steps[-k:] if path_matches(history,side,atom,current_key) else []
    else:
        prefixes=[None]*k;witness=[]
        for at,step in enumerate(steps):
            for i in range(k-1,-1,-1):
                if step[0]!=pattern[i]:continue
                codes={step[0]} if step[0]<10 else {step[0]//10,step[0]%10}
                if codes&{1,2} and step[2]<atom.get('min_line_step',1) or codes&{3,4} and step[3]<atom.get('min_water_step',1):continue
                if i and prefixes[i-1] is None:continue
                candidate=[step] if i==0 else [*prefixes[i-1],step]
                if 'span' in atom and candidate[-1][1]-candidate[0][1]>atom['span']:continue
                if prefixes[i] is None or candidate[0][1]>=prefixes[i][0][1]:prefixes[i]=candidate
                if i==k-1 and at==len(steps)-1 and (not atom.get('pulse') or step[4]==current_key):witness=candidate
    return [{'code':s[0],'ts':s[1],'line_step':s[2],'water_step':s[3],'event_key':s[4]} for s in witness]

PRE_FIELDS=('line_open','line_close','line_min','line_max','line_change','line_changes','water_open','water_close','water_min','water_max','water_change','water_changes','last_path1','last_path2','last_path3','last_path1_reset','last_path2_reset','last_path3_reset','closed')
CROSS_FIELDS=('age','phase','line','water0','water1','line_init','water0_init','water1_init','line_prev','water0_prev','water1_prev','line_alignment','init_alignment')
EXTRA_FIELDS={'stoppage_base','stoppage_added','stoppage_total','line_from_current_min','line_from_current_max','other_from_current_min','other_from_current_max','water_from_current_max',
 'other_water_prev_keep','other_water_prev_reset','other_water_prev_any_keep','other_water_prev_any_reset','line_same','line_return_keep','line_return_reset'}
EXTRA_FIELDS.update('pre_'+f for f in PRE_FIELDS)
EXTRA_FIELDS.update('cross_'+f for f in CROSS_FIELDS)
ROLE_FIELDS=('init','prev_keep','prev_reset','same','return_keep','return_reset','from_min','from_max','from_current_min','from_current_max')
EXTRA_FIELDS.update('role_water_'+f for f in ROLE_FIELDS)

class StandardFeatureStream(FeatureStream):
    def __init__(self,state=None,contract=None,atoms=(),cross_stale_minutes=None,cross_stale_minutes_prematch=None):
        super().__init__(state,contract)
        self.windows=set()
        self.paths=[];seen_paths=set()
        for a in atoms:
            if a['feature'].startswith('window_'):
                _,lo,hi,_=a['feature'].split('_',3);self.windows.add((int(lo),int(hi)))
            if ('sequence' in a or a.get('event_model')=='composite') and predicate_key(a) not in seen_paths:self.paths.append(deepcopy(a));seen_paths.add(predicate_key(a))
        # The archive logs a row only when a price changes, so an unchanged prematch quote hours old is still
        # the current price; live markets move every minute. Prematch staleness is therefore judged separately.
        self.cross_stale=cross_stale_minutes
        self.cross_stale_prematch=cross_stale_minutes if cross_stale_minutes_prematch is None or cross_stale_minutes is None else cross_stale_minutes_prematch
        self.extra=self.state.setdefault('_standard_v3',{'clocks':{},'markets':{},'pregame':{},'history':{},'roles':{}})

    def feed(self,event):
        if self.preflight(event):return None
        e=event;sid=e['sid'];clock=self.extra['clocks'].get(sid)
        if clock is not None and e['ts']<clock:raise ValueError('v3跨市场流必须按同场墙钟时间输入，不接受回插历史')
        self.extra['clocks'][sid]=e['ts']
        # Commit all market states strictly before this minute. Same-minute rows
        # never become a source for the other market in this decision batch.
        for market in (0,1):
            slot=self.extra['markets'].get(canonical([sid,market]))
            if slot and slot.get('pending') and slot['pending']['ts']<e['ts']:slot['committed']=deepcopy(slot['pending'])
        group=canonical([sid,e['market'],e['phase']])
        market_key=canonical([sid,e['market']])
        history=self.extra['history'].setdefault(group,{'keep':[],'reset':[]})
        previous={mode:history[mode][-1] if history[mode] else None for mode in ('keep','reset')}
        fs=super().feed(e)
        # All duplicates are resolved by preflight before either state changes.
        slot=self.extra['markets'].setdefault(market_key,{'pending':None,'committed':None})
        prior=slot.get('pending');first=prior.get('first') if prior and prior['phase']==e['phase'] else None
        if first is None and e['valid']:first=[e['line'],*e['water']]
        slot['pending']={'ts':e['ts'],'phase':e['phase'],'valid':e['valid'],'line':e['line'],'water':e['water'][:],
            'first':first,
            'previous':None if previous['keep'] is None else [previous['keep']['line'],*previous['keep']['water']]}
        pg=self.extra['pregame'].setdefault(market_key,{'summaries':{},'frozen':None,'last_closed':False,'previous':None,'previous_reset':None,'paths':{'0':[],'1':[],'0_reset':[],'1_reset':[]}})
        self._update_pregame(pg,e)
        if not e['valid']:
            history['reset']=[]
            states=self.extra.setdefault('path_states',{}).get(group,{})
            for a in self.paths:
                if a['feature'].endswith('_reset'):
                    for side in (0,1):states.pop(predicate_key(a)+':'+str(side),None)
            for key,r in self.extra['roles'].items():
                if key.startswith(group+'|'):r['prev_reset']=None;r['return_reset']=None
            return None
        for mode in ('keep','reset'):history[mode].append(deepcopy(e))
        if fs is None:return None
        # Only history needed by the selected rule tree is retained indefinitely.
        # Timed paths require at most their span plus one predecessor quote.
        for side,f in enumerate(fs):
            other=1-side
            f.update({name:MISSING for name in EXTRA_FIELDS})
            f.update(line_same=0,line_return_keep=0,line_return_reset=0)
            f['stoppage_base'],f['stoppage_added'],f['stoppage_total']=minute_parts(e['minute_raw'])
            for mode in ('keep','reset'):
                p=previous[mode]
                if p:
                    delta=e['water'][other]-p['water'][other]
                    f['other_water_prev_any_'+mode]=delta
                    f['other_water_prev_'+mode]=delta if e['line']==p['line'] else MISSING
            f['line_from_current_min']=max(0,f['line_from_min']);f['line_from_current_max']=min(0,f['line_from_max'])
            f['water_from_current_max']=min(0,f['water_from_max'])
            f['other_from_current_min']=max(0,f['other_from_min']);f['other_from_current_max']=min(0,f['other_from_max'])
            self._windows(group,e,side,f)
            self._role(group,e,side,f)
            self._cross(sid,e,f)
            self._pregame(pg,e,side,f)
            for a in self.paths:
                mode=a['feature'].split('_')[-1]
                witness=self._path_step(group,e,previous[mode],side,a)
                f[predicate_key(a)]=bool(witness);f[predicate_key(a)+'_evidence']=deepcopy(witness)
        for mode in ('keep','reset'):history[mode]=history[mode][-1:]
        return fs

    def _path_step(self,group,e,previous,side,a):
        key=predicate_key(a)+':'+str(side);pattern=tuple(a.get('pattern',[int(v) for v in str(a['value'])]));k=len(pattern)
        states=self.extra.setdefault('path_states',{}).setdefault(group,{})
        state=states.setdefault(key,{'events':[],'prefixes':[None]*k,'match':[]})
        if previous is None:return []
        dl=e['line']-previous['line'];dw=e['water'][side]-previous['water'][side]
        lc=1 if dl>0 else 2 if dl<0 else 0;wc=3 if dw>0 else 4 if dw<0 else 0
        code=lc*10+wc if a.get('event_model')=='composite' and lc and wc else lc or wc
        if not code:return [] if a.get('pulse') else state['match']
        step={'code':code,'ts':e['ts'],'line_step':abs(dl),'water_step':abs(dw),'event_key':e['event_key']}
        def valid(x,target):
            attrs={x['code']} if x['code']<10 else {x['code']//10,x['code']%10}
            return x['code']==target and (not attrs&{1,2} or x['line_step']>=a.get('min_line_step',1)) and (not attrs&{3,4} or x['water_step']>=a.get('min_water_step',1))
        state['match']=[]
        if a.get('sequence','contiguous')=='contiguous':
            state['events'].append(step);state['events']=state['events'][-3:];tail=state['events'][-k:]
            if len(tail)==k and all(valid(x,target) for x,target in zip(tail,pattern)) and ('span' not in a or tail[-1]['ts']-tail[0]['ts']<=a['span']):state['match']=tail
        else:
            prefixes=state['prefixes']
            for i in range(k-1,-1,-1):
                if not valid(step,pattern[i]) or i and prefixes[i-1] is None:continue
                candidate=[step] if not i else [*prefixes[i-1],step]
                if 'span' in a and candidate[-1]['ts']-candidate[0]['ts']>a['span']:continue
                if prefixes[i] is None or candidate[0]['ts']>=prefixes[i][0]['ts']:prefixes[i]=candidate
                if i==k-1:state['match']=candidate
        return state['match']

    def _update_pregame(self,pg,e):
        if e['phase']==1:
            if pg['frozen'] is None:
                pg['frozen']=deepcopy(pg['summaries'])
                for side in (0,1):
                    summary=pg['frozen'].get(str(side))
                    if summary is None:continue
                    summary['pre_closed']=int(pg['last_closed'])
                    for suffix in ('','_reset'):
                        steps=pg['paths'][str(side)+suffix]
                        for k in (1,2,3):summary[f'pre_last_path{k}'+suffix]=int(''.join(str(x) for x in steps[-k:])) if len(steps)>=k else MISSING
                pg['summaries']={};pg['paths']={};pg['previous']=pg['previous_reset']=None
            return
        if pg['frozen'] is not None:return
        pg['last_closed']=e['closed']
        if not e['valid']:
            pg['previous_reset']=None;pg['paths']['0_reset']=[];pg['paths']['1_reset']=[];return
        for side in (0,1):
            key=str(side);w=e['water'][side];line=e['line'];summary=pg['summaries'].get(key)
            if summary is None:
                summary={'pre_line_open':line,'pre_line_close':line,'pre_line_min':line,'pre_line_max':line,'pre_line_change':0,'pre_line_changes':0,
                         'pre_water_open':w,'pre_water_close':w,'pre_water_min':w,'pre_water_max':w,'pre_water_change':0,'pre_water_changes':0};pg['summaries'][key]=summary
            else:
                summary['pre_line_changes']+=int(line!=summary['pre_line_close']);summary['pre_water_changes']+=int(w!=summary['pre_water_close'])
                summary.update(pre_line_close=line,pre_line_min=min(summary['pre_line_min'],line),pre_line_max=max(summary['pre_line_max'],line),pre_line_change=line-summary['pre_line_open'],
                               pre_water_close=w,pre_water_min=min(summary['pre_water_min'],w),pre_water_max=max(summary['pre_water_max'],w),pre_water_change=w-summary['pre_water_open'])
            for suffix in ('','_reset'):
                previous=pg['previous'+suffix]
                if previous:
                    dl=line-previous['line'];dw=w-previous['water'][side];code=1 if dl>0 else 2 if dl<0 else 3 if dw>0 else 4 if dw<0 else 0
                    if code:pg['paths'][key+suffix].append(code);pg['paths'][key+suffix]=pg['paths'][key+suffix][-3:]
        pg['previous']=pg['previous_reset']={'line':e['line'],'water':e['water'][:]}

    def _windows(self,group,e,side,f):
        store=self.extra.setdefault('windows',{}).setdefault(group,{})
        for lo,hi in self.windows:
            prefix=f'window_{lo}_{hi}_';names=('line_init','water_init','otherwater_init')
            if e['phase']!=1 or not lo<=e['minute']<hi:
                for name in names:f[prefix+name]=MISSING
                continue
            first=store.setdefault(f'{lo}_{hi}',[e['line'],*e['water']])
            f[prefix+'line_init']=e['line']-first[0];f[prefix+'water_init']=e['water'][side]-first[1+side];f[prefix+'otherwater_init']=e['water'][1-side]-first[2-side]

    def _role(self,group,e,side,f):
        if e['market']!=1 or not e['line']:return
        role='give' if side==(0 if e['line']>0 else 1) else 'receive'
        key=group+'|'+role;w=e['water'][side]
        r=self.extra['roles'].setdefault(key,{'first':w,'min':w,'max':w,'prev_keep':None,'prev_reset':None,'same':{},'return_keep':None,'return_reset':None})
        f['role_water_init']=w-r['first'];f['role_water_same']=w-r['same'].setdefault(str(e['line']),w)
        for mode in ('keep','reset'):
            prev=r['prev_'+mode];f['role_water_prev_'+mode]=w-prev[1] if prev else MISSING
            if prev is None or prev[0]!=e['line']:r['return_'+mode]=w
            f['role_water_return_'+mode]=w-r['return_'+mode];r['prev_'+mode]=[e['line'],w]
        f['role_water_from_min']=w-r['min'];f['role_water_from_max']=w-r['max']
        r['min']=min(r['min'],w);r['max']=max(r['max'],w)
        f['role_water_from_current_min']=w-r['min'];f['role_water_from_current_max']=w-r['max']

    def _cross(self,sid,e,f):
        slot=self.extra['markets'].get(canonical([sid,1-e['market']]),{});other=slot.get('committed')
        if other is None:return
        age=e['ts']-other['ts'];f['cross_age']=age;f['cross_phase']=other['phase']
        limit=self.cross_stale if e['phase'] else getattr(self,'cross_stale_prematch',self.cross_stale)
        if limit is None or age>limit or not other['valid'] or other['phase']!=e['phase']:return
        f['cross_line']=other['line'];f['cross_water0'],f['cross_water1']=other['water']
        first=other.get('first');prev=other.get('previous')
        for base,suffix in ((first,'init'),(prev,'prev')):
            if base:
                f['cross_line_'+suffix]=other['line']-base[0]
                f['cross_water0_'+suffix]=other['water'][0]-base[1];f['cross_water1_'+suffix]=other['water'][1]-base[2]
        for own,foreign,name in (('line_prev_keep','cross_line_prev','cross_line_alignment'),('line_init','cross_line_init','cross_init_alignment')):
            if f.get(own,MISSING)!=MISSING and f.get(foreign,MISSING)!=MISSING:f[name]=sign(f[own])*sign(f[foreign])

    def _pregame(self,pg,e,side,f):
        if e['phase'] and pg['frozen']:f.update(pg['frozen'].get(str(side),{}))
