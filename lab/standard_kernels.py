"""Independent, bounded-state numerical implementations of v3 virtual features."""
import numpy as np
from .mining import njit
from .common import MISSING

@njit(cache=True)
def bound_window_columns(mids,valid,minutes,lines,w0,w1,lo,hi):
    out=np.full((len(mids),3),MISSING,dtype=np.int64)
    current=-1;ready=False;first_line=0;first0=0;first1=0
    for i in range(len(mids)):
        if mids[i]!=current:current=mids[i];ready=False
        if not valid[i] or not lo<=minutes[i]<hi:continue
        if not ready:first_line=lines[i];first0=w0[i];first1=w1[i];ready=True
        out[i,0]=lines[i]-first_line;out[i,1]=w0[i]-first0;out[i,2]=w1[i]-first1
    return out

@njit(cache=True)
def path_predicate(mids,valid,lines,waters,times,pattern,reset,pulse,subsequence,composite,min_line,min_water,span):
    n=len(mids);k=len(pattern);out=np.zeros(n,dtype=np.bool_)
    codes=np.zeros(3,dtype=np.int64);stamps=np.zeros(3,dtype=np.int64);dls=np.zeros(3,dtype=np.int64);dws=np.zeros(3,dtype=np.int64)
    starts=np.zeros(3,dtype=np.int64);has_start=np.zeros(3,dtype=np.bool_)
    current=-1;ready=False;last_line=0;last_water=0;steps=0;state=False
    for i in range(n):
        if mids[i]!=current:
            current=mids[i];ready=False;steps=0;state=False;has_start[:]=False
        if not valid[i]:
            if reset:ready=False;steps=0;state=False;has_start[:]=False
            continue
        if not ready:last_line=lines[i];last_water=waters[i];ready=True;continue
        dl=lines[i]-last_line;dw=waters[i]-last_water;last_line=lines[i];last_water=waters[i]
        lc=1 if dl>0 else 2 if dl<0 else 0;wc=3 if dw>0 else 4 if dw<0 else 0
        code=lc*10+wc if composite and lc and wc else lc if lc else wc
        if code==0:
            if not pulse:out[i]=state
            continue
        dl=abs(dl);dw=abs(dw)
        if subsequence:
            state=False
            for j in range(k-1,-1,-1):
                target=pattern[j]
                if code!=target:continue
                line_event=code in (1,2) or code>=10
                water_event=code in (3,4) or code>=10
                if (line_event and dl<min_line) or (water_event and dw<min_water):continue
                if j and not has_start[j-1]:continue
                start=times[i] if j==0 else starts[j-1]
                if span>=0 and times[i]-start>span:continue
                if not has_start[j] or start>starts[j]:starts[j]=start;has_start[j]=True
                if j==k-1:state=True
        else:
            for j in range(2):codes[j]=codes[j+1];stamps[j]=stamps[j+1];dls[j]=dls[j+1];dws[j]=dws[j+1]
            codes[2]=code;stamps[2]=times[i];dls[2]=dl;dws[2]=dw;steps+=1;state=steps>=k
            if state:
                for j in range(k):
                    at=3-k+j;c=codes[at]
                    if c!=pattern[j] or ((c in (1,2) or c>=10) and dls[at]<min_line) or ((c in (3,4) or c>=10) and dws[at]<min_water):state=False
                if span>=0 and stamps[2]-stamps[3-k]>span:state=False
        out[i]=state
    return out
