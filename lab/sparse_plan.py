"""Exact no-hit pruning on FULL event masks, never on payoff or parent firsts.

An expression is elided only if one of its pairwise condition intersections is
empty on all eligible events. Missing expressions are reconstructible from the
frozen raw TaskPlan plus this graph; their n and pnl are exactly zero. Surviving
triangles may still have empty three-way intersection and are evaluated normally.
No performance claim is implied outside the recorded test scope.
"""
from pathlib import Path
import numpy as np
from .common import atomic_json,atomic_replace,read_json,digest,sha
from .mining import njit,Paused,ResourcePaused
from .search_plan import TaskPlan

SPARSE_VERSION='FSL_full_event_nohit_graph_v03a'

def close_graph(graph):
    """Release the file mapping before replacing files or removing a workspace."""
    mapping=getattr(graph,'_mmap',None)
    if mapping is not None:mapping.close()

@njit(cache=True)
def overlap_graph(masks,nz,offsets,rows,columns):
    out=np.zeros((len(rows),len(columns)),dtype=np.bool_)
    for ai in range(len(rows)):
        a=rows[ai]
        for bj in range(len(columns)):
            b=columns[bj];rare=a
            if offsets[b+1]-offsets[b]<offsets[a+1]-offsets[a]:rare=b
            for q in range(offsets[rare],offsets[rare+1]):
                w=nz[q]
                if masks[a,w]&masks[b,w]:out[ai,bj]=True;break
    return out

@njit(cache=True)
def prefix_groups(graph,anchors,pool,core_position,k,anchored):
    # Return nonempty 2-atom prefixes and the size of their allowed tail.
    aa=[];bb=[];cc=[]
    if k==2:
        for ai in range(len(anchors)):
            a=anchors[ai]
            start=0 if anchored else ai+1
            for bi in range(start,len(pool)):
                b=pool[bi];bp=core_position[b]
                if not graph[a,bp]:continue
                n=0
                for ci in range(bi+1,len(pool)):
                    c=pool[ci];cp=core_position[c]
                    if graph[a,cp] and graph[b,cp]:n+=1
                if n:
                    aa.append(a);bb.append(bi);cc.append(n)
    else:
        for ai in range(len(anchors)):
            a=anchors[ai];start=0 if anchored else ai+1;n=0
            for bi in range(start,len(pool)):
                if graph[a,core_position[pool[bi]]]:n+=1
            if n:aa.append(a);bb.append(start);cc.append(n)
    return np.asarray(aa,np.int64),np.asarray(bb,np.int64),np.asarray(cc,np.int64)

class SparseTaskPlan:
    """Seekable surviving-expression plan plus a complete compressed zero ledger."""
    def __init__(self,spec,graph,groups):
        self.spec=spec;self.graph=graph;self.groups=groups
        self.raw=TaskPlan(spec['raw_plan']);self.blocks=spec['blocks'];self.total=spec['total']
        self.core=np.array(spec['graph_columns'],dtype=np.int64)
        self.pos=np.full(spec['atom_count'],-1,dtype=np.int64)
        self.pos[self.core]=np.arange(len(self.core))
        self.ends=np.cumsum([b['size'] for b in self.blocks],dtype=np.int64)
    def close(self):
        graph=getattr(self,'graph',None);self.graph=None
        close_graph(graph)
    def __enter__(self):
        if self.graph is None:raise ValueError('稀疏计划已关闭')
        return self
    def __exit__(self,*exc):self.close()
    def __del__(self):self.close()
    def locate(self,i):
        if not 0<=i<=self.total:raise IndexError('稀疏计划位置越界')
        if i==self.total:return len(self.blocks),0
        b=int(np.searchsorted(self.ends,i,side='right'))
        return b,int(i-(self.ends[b-1] if b else 0))
    def iter_from(self,start=0):
        if self.graph is None:raise ValueError('稀疏计划已关闭')
        bi,local=self.locate(start)
        for b in self.blocks[bi:]:
            if b['kind']=='base':
                if local<1:yield ()
            elif b['kind']=='unary':
                for a in range(local,b['size']):yield (a,)
            else:
                pool=np.array(b['pool'],dtype=np.int64)
                g=self.groups[b['id']];end=np.cumsum(g['n'],dtype=np.int64)
                gi=int(np.searchsorted(end,local,side='right'))
                offset=local-int(end[gi-1] if gi else 0)
                for row in range(gi,len(g['a'])):
                    a=int(g['a'][row]);bx=int(g['b'][row])
                    if b['k']==1:
                        tail=pool[bx:];tail=tail[self.graph[a,self.pos[tail]]]
                        for c in tail[offset:]:yield tuple(sorted((a,int(c))))
                    else:
                        bb=int(pool[bx]);tail=pool[bx+1:]
                        tail=tail[self.graph[a,self.pos[tail]]&self.graph[bb,self.pos[tail]]]
                        for c in tail[offset:]:yield tuple(sorted((a,bb,int(c))))
                    offset=0
            local=0
    def coverage(self,completed):
        if not 0<=completed<=self.total:raise ValueError('已完成数越界')
        out=[];start=0
        for b in self.blocks:
            done=max(0,min(b['size'],completed-start));zero=b['raw_size']-b['size']
            out.append({'block':b['id'],'total':b['raw_size'],'evaluated':done,'proven_zero':zero,
                        'remaining':b['size']-done,
                        'status':'COMPLETE' if done==b['size'] else 'PARTIAL' if done or zero else 'NOT_RUN',
                        'v3_relation':b['v3_relation']})
            start+=b['size']
        return out
    def is_proven_zero(self,ids):
        if self.graph is None:raise ValueError('稀疏计划已关闭')
        # All unary expressions are intentionally evaluated, even when empty.
        for i,a in enumerate(ids):
            for b in ids[i+1:]:
                if self.pos[b]>=0 and not self.graph[a,self.pos[b]]:return True
                if self.pos[a]>=0 and not self.graph[b,self.pos[a]]:return True
        return False


def prepare_sparse_plan(root,raw_spec,masks,nz,offsets,config,update,should_pause):
    root=Path(root);root.mkdir(exist_ok=True)
    nf=root/'nohit_graph.npy';sf=root/'sparse_plan.json';gf=root/'sparse_groups.npz'
    import hashlib
    mask_hash=hashlib.sha256()
    for row in masks:mask_hash.update(memoryview(np.ascontiguousarray(row)).cast('B'))
    binding=digest({'mask_content_sha256':mask_hash.hexdigest(),'version':SPARSE_VERSION,'raw':raw_spec,'masks_shape':list(masks.shape),
                    'dictionary_hash':raw_spec['atom_hash']})
    if sf.exists():
        saved=read_json(sf)
        if saved['binding']!=binding or sha(nf)!=saved['graph_sha256'] or sha(gf)!=saved['groups_sha256']:
            raise ValueError('零命中证明图/游标已变化，不能复用')
        with np.load(gf,allow_pickle=False) as z:
            groups={b['id']:{n:z[b['id']+'_'+n] for n in ('a','b','n')} for b in saved['blocks'] if b['kind'] not in ('base','unary')}
        return SparseTaskPlan(saved,np.load(nf,mmap_mode='r'),groups)
    core=sorted({i for b in raw_spec['blocks'] for i in b.get('pool',[])})
    na=masks.shape[0]-1;estimated=na*len(core)
    # Reserve half for the bool compatibility graph; the rest covers indexes and masks.
    if estimated>int(config.get('max_mask_mb',1024))*1048576//2:
        raise ResourcePaused('零命中图预计超出预算；请提高预算或分方向执行，未缩减搜索空间')
    graph=np.lib.format.open_memmap(nf.with_suffix('.tmp.npy'),mode='w+',dtype=np.bool_,shape=(na,len(core)))
    columns=np.array(core,dtype=np.int64)
    try:
        for start in range(0,na,64):
            if should_pause():raise Paused()
            rows=np.arange(start,min(na,start+64),dtype=np.int64)
            graph[start:start+len(rows)]=overlap_graph(masks,nz,offsets,rows,columns)
            update(message='建立完整事件零交集证明图（不读取策略收益）',graph_rows=min(na,start+64),graph_total=na)
        graph.flush()
    finally:close_graph(graph)
    atomic_replace(nf.with_suffix('.tmp.npy'),nf)
    graph=np.load(nf,mmap_mode='r');pos=np.full(na,-1,dtype=np.int64);pos[columns]=np.arange(len(columns))
    try:
        blocks=[];groups={};flat={}
        for orig in raw_spec['blocks']:
            if should_pause():raise Paused()
            b=dict(orig);b['raw_size']=orig['size']
            if orig['kind'] in ('base','unary'):blocks.append(b);continue
            anchored=orig['kind']=='anchored';pool=np.array(orig['pool'],dtype=np.int64)
            anchors=np.array(orig.get('anchors',orig['pool']),dtype=np.int64)
            k=orig['k'] if anchored else orig['k']-1
            a,bb,n=prefix_groups(graph,anchors,pool,pos,k,anchored)
            groups[orig['id']]={'a':a,'b':bb,'n':n}
            for key,val in groups[orig['id']].items():flat[orig['id']+'_'+key]=val
            b.update(kind='sparse',k=k,size=int(n.sum()));blocks.append(b)
        with gf.open('wb') as f:np.savez_compressed(f,**flat)
        saved={'version':SPARSE_VERSION,'binding':binding,'raw_plan':raw_spec,'blocks':blocks,
               'raw_total':raw_spec['total'],'total':sum(b['size'] for b in blocks),
               'graph_columns':core,'atom_count':na,'graph_sha256':sha(nf),'groups_sha256':sha(gf),
               'proof':'A AND B has empty FULL eligible event mask => every expression containing A and B has n=0,pnl=0.',
               'mask_content_sha256':mask_hash.hexdigest(),
               'proof_scope':'this immutable input only; not logical equivalence on future events; uses no pnl for pruning'}
        saved['proven_zero']=saved['raw_total']-saved['total'];atomic_json(sf,saved)
        return SparseTaskPlan(saved,graph,groups)
    except BaseException:
        close_graph(graph);raise
