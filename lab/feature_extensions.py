"""Fixed, versioned bound-window first-quote features; no label inputs.

A window's first quote is registered before any strategy state/score filter.
History is full market+phase history and persists over closures for this version.
Outside the bound window the value is MISSING, not forward-filled.
"""
from .common import MISSING
WINDOWS=((0,16),(16,31),(31,46),(46,61),(61,76),(76,91),(0,46),(46,91),(0,91))

def window_feature_names():
    return {f'window_{lo}_{hi}_{kind}' for lo,hi in WINDOWS for kind in ('line_init','water_init','otherwater_init')}

def bound_window_features(state,event,side):
    out={}; storage=state.setdefault('windows',{})
    for lo,hi in WINDOWS:
        prefix=f'window_{lo}_{hi}_'; active=event['phase']==1 and lo<=event['minute']<hi
        if active:
            key=f'{lo}_{hi}'
            if key not in storage:storage[key]=[event['line'],*event['water']]
            b=storage[key]
            out[prefix+'line_init']=event['line']-b[0]
            out[prefix+'water_init']=event['water'][side]-b[1+side]
            out[prefix+'otherwater_init']=event['water'][1-side]-b[2-side]
        else:
            for kind in ('line_init','water_init','otherwater_init'):out[prefix+kind]=MISSING
    return out
