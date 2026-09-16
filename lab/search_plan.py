"""Seekable finite-expression plan. Local block coverage != complete v3 M00-M08.

Indices are lexicographic, matching itertools.combinations. A resume near the end
unranks that index instead of walking the completed prefix. Enumeration is never
conditioned on profitability. Contradictory expressions are still evaluated and
reported; counts are expressions, not unique viable strategies.
"""
from math import comb
from bisect import bisect_right
from .common import digest

PLAN_VERSION='FSL_seekable_finite_v02'

def choose(n,k):
    return comb(n,k) if 0<=k<=n else 0


def unrank_combination(n,k,rank):
    if not 0<=rank<choose(n,k):
        raise IndexError('组合位置超出范围')
    out=[];low=0
    for pos in range(k):
        rest=k-pos-1
        base=choose(n-low,rest+1)
        left,right=low,n-rest-1
        # Prefix skipped before candidate x: C(n-low,r+1)-C(n-x,r+1).
        while left<right:
            mid=(left+right+1)//2
            skipped=base-choose(n-mid,rest+1)
            if skipped<=rank:left=mid
            else:right=mid-1
        x=left;rank-=base-choose(n-x,rest+1)
        out.append(x);low=x+1
    return tuple(out)


def combinations_from(pool,k,start=0):
    n=len(pool);total=choose(n,k)
    if start<0 or start>total:raise IndexError('组合恢复位置无效')
    if start==total:return
    ix=list(unrank_combination(n,k,start))
    while True:
        yield tuple(pool[i] for i in ix)
        j=k-1
        while j>=0 and ix[j]==n-k+j:j-=1
        if j<0:return
        ix[j]+=1
        for a in range(j+1,k):ix[a]=ix[a-1]+1


def is_path(a):
    return a['feature'].startswith(('path','linepath'))


def make_plan_spec(atoms, core, triples, profile):
    # Smoke intentionally stays small and retains the old smoke traversal order.
    core=sorted(set(core));triples=sorted(set(triples))
    if profile!='smoke':triples=core[:]
    context=[i for i in core if not is_path(atoms[i])]
    timed=[i for i,a in enumerate(atoms) if i not in set(core) and 'span' in a]
    fine=[i for i,a in enumerate(atoms) if i not in set(core) and i not in set(timed) and not is_path(a)]
    blocks=[{'id':'L00_BASELINE','kind':'base','size':1,'v3_relation':'M00'},
            {'id':'L01_UNARY','kind':'unary','size':len(atoms),'v3_relation':'M01/M04 limited atoms'},
            {'id':'L02_CORE_PAIR','kind':'comb','pool':core,'k':2,'size':choose(len(core),2),'v3_relation':'M02 limited dictionary'},
            {'id':'L03_CORE_TRIPLE','kind':'comb','pool':triples,'k':3,'size':choose(len(triples),3),'v3_relation':'M03 limited dictionary'}]
    if profile!='smoke':
        for name,anchors,relation in [('TIMED',timed,'M04 limited time-projected paths'),('FINE',fine,'M05 limited fine atoms')]:
            for k in (1,2):
                blocks.append({'id':f'L04_{name}_CONTEXT{k}' if name=='TIMED' else f'L05_{name}_CONTEXT{k}',
                               'kind':'anchored','anchors':anchors,'pool':context,'k':k,
                               'size':len(anchors)*choose(len(context),k),'v3_relation':relation})
    return {'version':PLAN_VERSION,'profile':profile,'atom_hash':digest(atoms),'blocks':blocks,
            'total':sum(b['size'] for b in blocks),'core_atoms':len(core),'context_atoms':len(context),
            'timed_atoms':len(timed),'fine_context_atoms':len(fine),
            'count_kind':'raw_deterministic_expressions_in_local_finite_spec',
            'standard_v3_status':'PARTIAL_CORE',
            'unimplemented':['Full v3 W/C/T domain grids and all bound-window ranges',
              'Full T min-step/span grids and inserted-event subsequences',
              'cross-market and pre-to-live summaries', 'complete standard coverage audit']}


class TaskPlan:
    def __init__(self,spec):
        if spec.get('version')!=PLAN_VERSION:raise ValueError('任务计划版本不兼容')
        self.spec=spec;self.blocks=spec['blocks'];self.ends=[];n=0
        for b in self.blocks:
            if type(b['size']) is not int or b['size']<0:raise ValueError('模块规模无效')
            n+=b['size'];self.ends.append(n)
        self.total=n
        if n!=spec['total']:raise ValueError('任务计划总数不符')

    def locate(self,index):
        if not 0<=index<=self.total:raise IndexError('恢复位置不属于计划')
        if index==self.total:return len(self.blocks),0
        b=bisect_right(self.ends,index)
        return b,index-(self.ends[b-1] if b else 0)

    def iter_from(self,start=0):
        bi,local=self.locate(start)
        for b in self.blocks[bi:]:
            kind=b['kind']
            if kind=='base':
                if local<1:yield ()
            elif kind=='unary':
                for i in range(local,b['size']):yield (i,)
            elif kind=='comb':
                yield from combinations_from(b['pool'],b['k'],local)
            else:
                per=choose(len(b['pool']),b['k'])
                if per:
                    ai,ci=divmod(local,per)
                    for anchor in b['anchors'][ai:]:
                        for tail in combinations_from(b['pool'],b['k'],ci):
                            yield tuple(sorted((anchor,*tail)))
                        ci=0
            local=0

    def coverage(self,completed):
        if not 0<=completed<=self.total:raise ValueError('已完成数超出计划')
        start=0;out=[]
        for b in self.blocks:
            done=max(0,min(b['size'],completed-start))
            out.append({'block':b['id'],'total':b['size'],'evaluated':done,
                        'status':'COMPLETE' if done==b['size'] else 'PARTIAL' if done else 'NOT_RUN',
                        'v3_relation':b['v3_relation']})
            start+=b['size']
        return out
