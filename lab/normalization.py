"""Provable syntactic/numeric AND normalization; never empirical equivalence."""
from .common import canonical,digest
from .standard_atoms import normalize_atom
from .rules import label

def normalize_conditions(conditions,scale):
    scalar={};paths={}
    for original in conditions:
        a=normalize_atom(original);f=a['feature']
        if f.startswith(('path','linepath')):
            paths[canonical(a)]=a;continue
        low,high=scalar.get(f,(None,None));op=a['op'];v=a['value']
        # Numeric minute comparisons exclude text half-time (-2). Preserve
        # that domain restriction before collapsing an interval to equality.
        if f=='minute' and op!='eq':low=0 if low is None else max(low,0)
        if op in ('ge','range','eq'):low=v if low is None else max(low,v)
        if op in ('le','eq','range'):
            upper=a['upper'] if op=='range' else v+1
            high=upper if high is None else min(high,upper)
        if low is not None and high is not None and low>=high:return None
        scalar[f]=(low,high)
    out=list(paths.values())
    for f,(low,high) in sorted(scalar.items()):
        if low is None:a={'feature':f,'op':'le','value':high-1}
        elif high is None:a={'feature':f,'op':'ge','value':low}
        elif high==low+1:a={'feature':f,'op':'eq','value':low}
        else:a={'feature':f,'op':'range','value':low,'upper':high}
        out.append(a)
    out.sort(key=canonical)
    return [{**a,'label':label(a,scale)} for a in out]

def logic_key(conditions):return digest([{k:v for k,v in a.items() if k!='label'} for a in conditions])
