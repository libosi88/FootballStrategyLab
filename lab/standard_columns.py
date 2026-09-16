"""Lazy virtual feature columns and path masks; bounded resident cache."""
from pathlib import Path
from collections import OrderedDict
import numpy as np
from .common import MISSING,digest
from .standard_kernels import bound_window_columns,path_predicate
from .rules import mask as ordinary_mask

class StandardColumns(dict):
    def __init__(self,base,events,direction,root=None,cache_mb=128):
        super().__init__(base);self.root=Path(root) if root else None
        if self.root:self.root.mkdir(parents=True,exist_ok=True)
        self.limit=int(cache_mb)*1048576;self.cache=OrderedDict();self.resident=0
        phase=0 if direction.startswith('PRE') else 1;market=0 if direction.endswith(('OVER','UNDER')) else 1
        rows=[e for e in events if (e['phase'],e['market'])==(phase,market)]
        self.raw={k:np.array([e[k] for e in rows],dtype=np.bool_ if k=='valid' else np.int64) for k in ('eid','mid','valid','minute','line','ts')}
        self.raw['w0']=np.array([e['water'][0] for e in rows],np.int64);self.raw['w1']=np.array([e['water'][1] for e in rows],np.int64)
        self.positions=np.searchsorted(self.raw['eid'],self['eid']) if len(rows) else np.empty(0,np.int64)
        if len(self.positions) and not np.array_equal(self.raw['eid'][self.positions],self['eid']):raise ValueError('v3特征索引与原始事件不一致')
        self.possible_codes=set()
        previous={}
        for e in rows:
            if not e['valid']:continue
            p=previous.get(e['mid'])
            if p:
                dl=e['line']-p['line'];lc=1 if dl>0 else 2 if dl<0 else 0
                for side in (0,1):
                    dw=e['water'][side]-p['water'][side];wc=3 if dw>0 else 4 if dw<0 else 0
                    if lc or wc:self.possible_codes.add(lc or wc)
                    if lc and wc:self.possible_codes.add(lc*10+wc)
                    elif lc or wc:self.possible_codes.add(lc or wc)
            previous[e['mid']]=e

    def _remember(self,key,value):
        self.cache[key]=value;self.resident+=value.nbytes
        while self.resident>self.limit and len(self.cache)>1:
            _,old=self.cache.popitem(last=False);self.resident-=old.nbytes
        return value

    def column(self,name):
        if dict.__contains__(self,name):return dict.__getitem__(self,name)
        key='column:'+name
        if key in self.cache:self.cache.move_to_end(key);return self.cache[key]
        if not name.startswith('window_'):raise KeyError(name)
        _,lo,hi,kind=name.split('_',3);r=self.raw
        values=bound_window_columns(r['mid'],r['valid'],r['minute'],r['line'],r['w0'],r['w1'],int(lo),int(hi))
        # One causal window pass produces all three existing columns. Keep a
        # local result even when a tiny cache evicts it while storing siblings.
        answer=None
        for suffix,at in (('line_init',0),('water_init',1+self['side']),('otherwater_init',2-self['side'])):
            sibling='column:window_'+lo+'_'+hi+'_'+suffix
            column=self.cache[sibling] if sibling in self.cache else self._remember(sibling,values[self.positions,at])
            if sibling in self.cache:self.cache.move_to_end(sibling)
            if suffix==kind:answer=column
        return answer

    def __getitem__(self,key):
        return dict.__getitem__(self,key) if dict.__contains__(self,key) else self.column(key)

    def atom_mask(self,a):
        if 'sequence' not in a and a.get('event_model')!='composite':
            values=self.column(a['feature']);base={a['feature']:values}
            if 'span' in a or a.get('pulse') or 'min_line_step' in a or 'min_water_step' in a:
                k=a['feature'][4];mode=a['feature'].split('_')[-1]
                for name in (f'span{k}_{mode}',f'pulse_{mode}',f'pmin_line{k}_{mode}',f'pmin_water{k}_{mode}'):base[name]=self[name]
            return ordinary_mask(a,base)
        key='path:'+digest({k:v for k,v in a.items() if k!='label'})
        if key in self.cache:self.cache.move_to_end(key);return self.cache[key]
        composite=a.get('event_model')=='composite';pattern=a.get('pattern',[int(v) for v in str(a['value'])])
        if any(code not in self.possible_codes for code in pattern):return np.zeros(len(self['eid']),np.bool_)
        r=self.raw;answer=np.zeros(len(self['eid']),np.bool_)
        for side in (0,1):
            positions=self['side']==side
            if not np.any(positions):continue
            full=path_predicate(r['mid'],r['valid'],r['line'],r['w'+str(side)],r['ts'],np.array(pattern,np.int64),a['feature'].endswith('_reset'),a.get('pulse',False),a.get('sequence')=='subsequence',composite,a.get('min_line_step',1),a.get('min_water_step',1),a.get('span',-1))
            answer[positions]=full[self.positions[positions]]
        return self._remember(key,answer)

    def close(self):self.cache.clear();self.raw.clear();self.resident=0

    def __enter__(self):return self
    def __exit__(self,*exc):self.close()

    def domain(self,name):
        value=self.column(name);good=value[value!=MISSING]
        return None if not len(good) else (int(good.min()),int(good.max()))
