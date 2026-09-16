"""Resumable exact class search for the complete frozen v3 grammar.

Nodes cover recoverable families of original expressions. A prefix can be
skipped only when its full event mask is empty or its per-match best possible
payoff bound cannot exceed the frozen threshold. Parent first-trade profit is
never a pruning criterion.
"""
from pathlib import Path
from collections import Counter
from itertools import combinations
from math import comb
import sqlite3,json,shutil
from .common import canonical,atomic_json,read_json,money_threshold
from .standard_atoms import W,C,T,X,P
from .mining import Paused,ResourcePaused

SEARCH_VERSION='FSL_STANDARD_CLASS_DFS_V2'

def choose(n,k):return comb(n,k) if 0<=k<=n else 0

def extension_count(prefix,k,sizes):
    """Count raw distinct-atom subsets whose ordered mask classes start here."""
    if not prefix:return choose(sum(sizes),k)
    if len(prefix)>k:return 0
    multiplicity=Counter(prefix);last=prefix[-1];fixed=1
    for group,n in multiplicity.items():
        if group!=last:fixed*=choose(sizes[group],n)
    remaining=k-len(prefix);nlast=multiplicity[last];later=sum(sizes[last+1:])
    return fixed*sum(choose(sizes[last],nlast+j)*choose(later,remaining-j) for j in range(remaining+1))

def build_blocks(atoms,masks,max_conditions=3):
    core=atoms.ids(C);core_set=set(core);paths={i for i in core if atoms[i]['feature'].startswith(('path','linepath'))}
    context=[i for i in core if i not in paths];fine=[i for i in atoms.ids(W) if i not in core_set]
    blocks=[{'id':'M00_BASE','module':'M00','k':0,'anchors':[None],'pool':[]}]
    def unary(module,ids):
        blocks.append({'id':module+'_UNARY','module':module,'k':0,'anchors':masks.groups(ids),'pool':[]})
    unary('M01',atoms.ids(W))
    # Modules run in order (M00, M01, M02, ...), so any budget stop has finished every earlier module.
    for k in (2,3):
        if k<=max_conditions:blocks.append({'id':f'M0{k}_CORE{k}','module':f'M0{k}','k':k,'anchors':[None],'pool':masks.groups(core)})
    for module,ids,pool in (('M04',atoms.ids(T),context),('M05',fine,context),('M06',atoms.ids(X),core),('M07',atoms.ids(P),core)):
        if module!='M05':unary(module,ids)
        for k in (1,2):blocks.append({'id':f'{module}_CONTEXT{k}','module':module,'k':k,'anchors':masks.groups(ids),'pool':masks.groups(pool)})
    for b in blocks:
        b['sizes']=[len(g['members']) for g in b['pool']]
        anchors=sum(1 if a is None else len(a['members']) for a in b['anchors'])
        b['total']=anchors*choose(sum(b['sizes']),b['k'])
    return blocks

def family_conditions(block,anchor,prefix):
    """Expand every distinct original expression without inventing future equivalence."""
    if len(prefix)<block['k']:
        low=prefix[-1] if prefix else 0
        for group in range(low,len(block['pool'])):
            child=[*prefix,group]
            if extension_count(child,block['k'],block['sizes']):yield from family_conditions(block,anchor,child)
        return
    groups=Counter(prefix)
    pieces=sorted(groups.items())
    def tails(position,ids):
        if position==len(pieces):yield ids;return
        g,count=pieces[position]
        for chosen in combinations(block['pool'][g]['members'],count):yield from tails(position+1,(*ids,*chosen))
    anchor_ids=(None,) if anchor is None else block['anchors'][anchor]['members']
    for ids in tails(0,()):
        for a in anchor_ids:yield tuple(sorted(ids if a is None else (*ids,a)))

def run_class_search(root,atoms,masks,config,update,should_pause,binding,blocked=None):
    root=Path(root);planfile=root/'standard_plan.json';statefile=root/'state.json'
    if should_pause():raise Paused()
    from .search_integrity import verify_stopped_search,verify_search_evidence,PARTIAL_SEARCH_STATUSES
    previous_state=verify_stopped_search(root,binding)
    if hasattr(masks,'set_group_control'):masks.set_group_control(update,should_pause)
    from .standard_spec import max_conditions_for
    blocks=build_blocks(atoms,masks,max_conditions_for(config));plan={'version':SEARCH_VERSION,'binding':binding,'blocks':blocks,'total':sum(b['total'] for b in blocks),'blocked':blocked or {},
      'mapping':'All original atom IDs remain in their full-event mask classes. family_conditions expands original AND expressions. No future equivalence is assumed.'}
    if planfile.exists() and read_json(planfile)!=plan:raise ValueError('标准搜索计划或掩码归组改变')
    if not planfile.exists():atomic_json(planfile,plan,compact=True)
    conn=sqlite3.connect(root/'search.sqlite3')
    conn.execute('CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY,block INTEGER,anchor INTEGER,prefix TEXT,kind TEXT,weight TEXT,mask TEXT,n INTEGER,net INTEGER,upper INTEGER)')
    conn.execute('CREATE TABLE IF NOT EXISTS checkpoint(id INTEGER PRIMARY KEY,body TEXT)')
    row=conn.execute('SELECT body FROM checkpoint WHERE id=1').fetchone()
    state=json.loads(row[0]) if row else {'version':SEARCH_VERSION,'binding':binding,'block':0,'anchor':0,'stack':None,'covered':0,'representatives':0,'equivalent_occurrences':0,'proven_zero':0,'proven_nonprofitable':0,'candidates':0,'nodes':0,'status':'RUNNING'}
    try:
        if state['binding']!=binding:raise ValueError('标准搜索断点身份改变')
        # The SQLite checkpoint also guards a missing or stale public state.
        # Rejection occurs outside the recovery handler, which seals interrupts.
        if state['status'] in PARTIAL_SEARCH_STATUSES and (previous_state or {}).get('status') not in PARTIAL_SEARCH_STATUSES:
            verify_search_evidence(root,'standard_class_dfs_partial',binding)
    except BaseException:
        conn.close();raise
    loaded_complete=state['status']=='COMPLETE'
    threshold=money_threshold(config['min_profit'],2*config['_water_scale'])
    all_key=masks.store(masks.all_words);masks.conn.commit()
    def public():
        return {**{k:v for k,v in state.items() if k!='stack'},'raw_total':plan['total'],'total':plan['total'],'next':state['covered'],'remaining':plan['total']-state['covered'],'planned_expressions':plan['total'],
                'search_backend':'standard_class_dfs','blocked':plan['blocked'],'standard_scope_complete':state['status']=='COMPLETE' and not plan['blocked'],
                'count_kind':'normalized_atom_module_expression_occurrences; representative families and reversible historical cache mapping'}
    published=[None]
    def save(status=None):
        from .search_integrity import seal_search_evidence,PARTIAL_SEARCH_STATUSES
        if status:state['status']=status
        masks.conn.commit()
        conn.execute('INSERT INTO checkpoint VALUES(1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',(canonical(state),));conn.commit()
        # The DFS stack lives only in the SQLite checkpoint; the small public state file is
        # rewritten at every stop and at most every 30 seconds while running.
        now=__import__('time').monotonic()
        if state['status']!='RUNNING' or published[0] is None or now-published[0]>=30:atomic_json(statefile,public(),compact=True);published[0]=now
        if state['status'] in PARTIAL_SEARCH_STATUSES:seal_search_evidence(root,'standard_class_dfs_partial',binding)
    try:
        if state['status']=='COMPLETE':
            from .search_integrity import verify_search_evidence
            verify_search_evidence(root,'standard_class_dfs',binding)
            return public()
        save('RUNNING')
        while state['block']<len(blocks):
            if should_pause():save('PAUSED');raise Paused()
            b=blocks[state['block']]
            if state['anchor']>=len(b['anchors']):state.update(block=state['block']+1,anchor=0,stack=None);continue
            anchor=b['anchors'][state['anchor']];factor=1 if anchor is None else len(anchor['members'])
            base=all_key if anchor is None else anchor['hash']
            if state['stack'] is None:state['stack']=[([],base)]
            if not state['stack']:state.update(anchor=state['anchor']+1,stack=None);continue
            budget=int(config.get('standard_node_budget',0))
            if budget and state['nodes']>=budget:save('BUDGET_STOP');return public()
            prefix,key=state['stack'].pop();weight=factor*extension_count(prefix,b['k'],b['sizes'])
            if not weight:continue
            if prefix:key=masks.intersection(key,b['pool'][prefix[-1]]['hash'])
            state['nodes']+=1;n,net,upper=masks.statistics(key);kind=None
            if key=='ZERO':kind='PROVEN_ZERO';state['proven_zero']+=weight
            elif len(prefix)<b['k'] and upper<=threshold:kind='PROVEN_NONPROFITABLE_BOUND';state['proven_nonprofitable']+=weight
            elif len(prefix)==b['k']:
                kind='EVALUATED';state['representatives']+=1;state['equivalent_occurrences']+=weight-1
                if net>threshold:state['candidates']+=weight
            else:
                low=prefix[-1] if prefix else 0
                for group in range(len(b['pool'])-1,low-1,-1):
                    child=[*prefix,group]
                    if extension_count(child,b['k'],b['sizes']):state['stack'].append((child,key))
            if kind:
                state['covered']+=weight
                conn.execute('INSERT INTO ledger(block,anchor,prefix,kind,weight,mask,n,net,upper) VALUES(?,?,?,?,?,?,?,?,?)',
                             (state['block'],None if anchor is None else state['anchor'],canonical(prefix),kind,str(weight),key,n if kind=='EVALUATED' else 0 if kind=='PROVEN_ZERO' else None,net if kind=='EVALUATED' else 0 if kind=='PROVEN_ZERO' else None,upper))
            if state['nodes']%64==0:
                save('RUNNING');update(message='按v3冻结空间搜索，保留全部表达映射与上界证明',standard_module=b['module'],evaluated=state['covered'],total_rules=plan['total'],candidate_occurrences=state['candidates'],search_nodes=state['nodes'])
                if shutil.disk_usage(root).free<int(config.get('min_free_disk_mb',256))*1048576:save('RESOURCE_BLOCKED');raise ResourcePaused('磁盘空间低于阈值，标准搜索断点保留')
        if state['covered']!=plan['total']:raise RuntimeError('标准表达覆盖总数不一致')
        if state['covered']!=state['representatives']+state['equivalent_occurrences']+state['proven_zero']+state['proven_nonprofitable']:raise RuntimeError('标准评价/缓存/证明账不一致')
        save('COMPLETE')
        from .search_integrity import seal_search_evidence
        seal_search_evidence(root,'standard_class_dfs',binding)
        return public()
    except BaseException:
        if not loaded_complete and state['status'] not in ('PAUSED','RESOURCE_BLOCKED'):
            conn.rollback()
            committed=conn.execute('SELECT body FROM checkpoint WHERE id=1').fetchone()
            if committed:state=json.loads(committed[0])
            save('INTERRUPTED')
        raise
    finally:conn.close()

def candidate_families(root,threshold):
    from .search_integrity import verify_stopped_search
    root=Path(root);state=verify_stopped_search(root) or {}
    if state.get('status')=='COMPLETE':
        backend=state.get('search_backend','standard_class_dfs')
        # Global upper-bound and explicit empty-scope proofs contain no DFS
        # candidate ledger by definition. A completed proof therefore means an
        # empty candidate iterator, not a class-DFS evidence mismatch.
        if backend!='standard_class_dfs':return
    plan=read_json(root/'standard_plan.json')
    conn=sqlite3.connect((root/'search.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
    try:
        for row in conn.execute("SELECT id,block,anchor,prefix,weight,mask,n,net FROM ledger WHERE kind='EVALUATED' AND net>? ORDER BY id",(threshold,)):
            rid,block,anchor,prefix,weight,mask,n,net=row
            yield {'id':rid,'block':plan['blocks'][block],'anchor':anchor,'prefix':json.loads(prefix),'weight':int(weight),'mask':mask,'n':n,'net':net}
    finally:conn.close()

def family_conditions_from(block,anchor,prefix,start=0):
    """Direct mixed-radix seek within a profitable leaf; no replay of its prefix."""
    from .search_plan import unrank_combination
    if len(prefix)!=block['k']:raise ValueError('只有已评价叶家族可按表达游标恢复')
    pieces=sorted(Counter(prefix).items())
    sizes=[choose(len(block['pool'][g]['members']),n) for g,n in pieces]
    anchors=[None] if anchor is None else block['anchors'][anchor]['members']
    total=len(anchors)
    for n in sizes:total*=n
    if not 0<=start<=total:raise IndexError('候选家族游标越界')
    for index in range(start,total):
        rank,ai=divmod(index,len(anchors));ranks=[]
        for size in reversed(sizes):rank,local=divmod(rank,size);ranks.append(local)
        ids=[]
        for (group,count),local in zip(pieces,reversed(ranks)):
            members=block['pool'][group]['members'];ids.extend(members[i] for i in unrank_combination(len(members),count,local))
        if anchors[ai] is not None:ids.append(anchors[ai])
        yield tuple(sorted(ids))
