"""An exact whole-universe upper proof for necessarily unprofitable datasets."""
from itertools import combinations
from pathlib import Path
from .common import atomic_json,digest,money_threshold
from .standard_spec import make_standard_plan
from .standard_atoms import W,C,T,X,P


def prove_global_bound(root,base,atoms,config,scale,binding,blocked):
    root=Path(root)
    best={};first={}
    for mid,pnl in zip(base['mid'],base['pnl']):
        mid=int(mid);pnl=int(pnl);best[mid]=max(best.get(mid,0),pnl);first.setdefault(mid,pnl)
    upper=sum(best.values());threshold=money_threshold(config['min_profit'],2*scale)
    if upper>threshold:return None
    core=atoms.ids(C);core_set=set(core);paths=[i for i in core if atoms[i]['feature'].startswith(('path','linepath'))]
    from .standard_spec import max_conditions_for,spec_version_for,modules_for
    plan=make_standard_plan(len(atoms.ids(W)),core,atoms.ids(T),[i for i in atoms.ids(W) if i not in core_set],atoms.ids(X),atoms.ids(P),paths,blocked,max_conditions_for(config),spec_version_for(config),modules_for(config))
    unary=next((b for b in plan['blocks'] if b['id']=='M01_W'),None)
    if unary is None:raise ValueError('标准计划缺少M01_W，不能构造全域证明')
    unary['kind']='unary_ids';unary['pool']=atoms.ids(W)
    plan.update(binding=binding,representation='raw_atom_ids_without_computing_masks',proof='global_upper_proof.json')
    atomic_json(root/'global_bound_plan.json',plan)
    proof={'binding':binding,'per_match_best_positive':best,'all_event_payoffs_sha256':digest([[int(m),int(p)] for m,p in zip(base['mid'],base['pnl'])]),
        'upper_i':upper,'threshold_i':threshold,'denominator':2*scale,'reason':'Any descendant selects at most one of all available quotes per match; sum(max(0, every event payoff)) bounds every possible first-event rule.',
        'baseline':{'n':len(first),'net_i':sum(first.values())},'no_profit_parent_pruning':True}
    atomic_json(root/'global_upper_proof.json',proof)
    total=plan['total'];state={'binding':binding,'status':'COMPLETE','total':total,'raw_total':total,'next':total,'covered':total,'remaining':0,
        'representatives':1,'equivalent_occurrences':0,'proven_zero':0,'proven_nonprofitable':total-1,'candidates':0,'nodes':1,
        'search_backend':'standard_global_bound','standard_scope_complete':not blocked,'blocked':blocked,'global_upper_i':upper,
        'mapping':'global_bound_plan.json raw atom IDs; global_bound.raw_conditions expands every original expression'}
    atomic_json(root/'state.json',state)
    from .search_integrity import seal_search_evidence
    seal_search_evidence(root,'standard_global_bound',binding)
    return state


def raw_conditions(block):
    if block['kind']=='base':yield ();return
    if block['kind']=='unary_ids':
        for i in block['pool']:yield (i,)
    elif block['kind']=='comb':yield from combinations(block['pool'],block['k'])
    elif block['kind']=='anchored':
        for ids in combinations(block['pool'],block['k']):
            for a in block['anchors']:yield tuple(sorted((*ids,a)))
    else:raise ValueError('未知原始表达索引')
