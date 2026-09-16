"""Read-only economics-kernel comparison on frozen real full-event masks."""
from pathlib import Path
import sys,time,json,sqlite3,zlib,statistics
import numpy as np
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from lab.common import source_fingerprint,atomic_json,sha
from lab.mining import njit,_ctz,pack_masks
from lab.standard_masks import mask_economics

@njit(cache=True,inline='always')
def binary_ctz(x):
    n=0
    if (x & np.uint64(0xffffffff))==0:n+=32;x=x>>np.uint64(32)
    if (x & np.uint64(0xffff))==0:n+=16;x=x>>np.uint64(16)
    if (x & np.uint64(0xff))==0:n+=8;x=x>>np.uint64(8)
    if (x & np.uint64(0xf))==0:n+=4;x=x>>np.uint64(4)
    if (x & np.uint64(0x3))==0:n+=2;x=x>>np.uint64(2)
    if (x & np.uint64(0x1))==0:n+=1
    return n

@njit(cache=True)
def binary_economics(words,ends,payoffs):
    count=0;net=0;upper=0;start=0
    while start<len(words):
        end=ends[start];seen=False;best=0
        for w in range(start,end):
            bits=words[w]
            while bits:
                bit=binary_ctz(bits);value=payoffs[w*64+bit]
                if not seen:count+=1;net+=value;seen=True
                if value>best:best=value
                bits=bits&(bits-np.uint64(1))
        upper+=best;start=end
    return count,net,upper

@njit(cache=True)
def old_batch(masks,ends,payoffs):
    out=np.empty((len(masks),3),np.int64)
    for i in range(len(masks)):out[i]=mask_economics(masks[i],ends,payoffs)
    return out

@njit(cache=True)
def new_batch(masks,ends,payoffs):
    out=np.empty((len(masks),3),np.int64)
    for i in range(len(masks)):out[i]=binary_economics(masks[i],ends,payoffs)
    return out

@njit(cache=True)
def compare_ctz(words):
    out=np.empty((len(words),2),np.int64)
    for i in range(len(words)):out[i,0]=_ctz(words[i]);out[i,1]=binary_ctz(words[i])
    return out

def independent(words,ends,payoffs):
    hit=np.unpackbits(words.view(np.uint8),bitorder='little').astype(bool)
    start=0;count=net=upper=0
    while start<len(words):
        end=int(ends[start]);ix=np.flatnonzero(hit[start*64:end*64])+start*64
        if len(ix):count+=1;net+=int(payoffs[ix[0]]);upper+=max(0,int(payoffs[ix].max()))
        start=end
    return count,net,upper

def main():
    before=source_fingerprint();rng=np.random.default_rng(415)
    words=np.r_[np.array([1<<n for n in range(64)],dtype=np.uint64),rng.integers(1,2**64,size=50000,dtype=np.uint64)]
    expected=np.array([(int(x)&-int(x)).bit_length()-1 for x in words])
    checked=compare_ctz(words);assert np.array_equal(checked[:,0],expected) and np.array_equal(checked[:,1],expected)
    source=Path(r'D:\FootballStrategyLab_workspace\v04_real_budget_cost\20260912_225900_362357\mining\LIVE_OVER')
    with np.load(source/'arrays.npz',allow_pickle=False) as z:base={k:z[k] for k in z.files}
    _,_,_,ends,payoffs=pack_masks(base,[])
    con=sqlite3.connect((source/'masks.sqlite3').as_uri()+'?mode=ro',uri=True)
    try:
        rows=con.execute("SELECT hash,payload,n,net,upper FROM blobs WHERE hash<>'ZERO' ORDER BY rowid LIMIT 256").fetchall()
    finally:con.close()
    masks=np.stack([np.frombuffer(zlib.decompress(x[1]),dtype='<u8') for x in rows])
    expected=np.array([x[2:] for x in rows],dtype=np.int64)
    assert np.array_equal(old_batch(masks,ends,payoffs),expected)
    assert np.array_equal(new_batch(masks,ends,payoffs),expected)
    for i in range(0,len(masks),8):assert independent(masks[i],ends,payoffs)==tuple(expected[i])
    timings={'original':[],'binary_ctz':[]}
    for order in (('original','binary_ctz'),('binary_ctz','original'),('original','binary_ctz')):
        for name in order:
            start=time.perf_counter();result=(old_batch if name=='original' else new_batch)(masks,ends,payoffs)
            timings[name].append(time.perf_counter()-start);assert np.array_equal(result,expected)
    report={'status':'PASS','scope':'Kernel-only fixed 256-mask prefix of frozen CSL full-event store; not India mining throughput',
        'engine_before':before,'engine_after':source_fingerprint(),'source_not_modified':source_fingerprint()==before,
        'scalar_ctz_cases':len(words),'independent_mask_cases':len(range(0,len(masks),8)),'actual_full_event_masks':len(rows),
        'source_feature_sha256':sha(source/'arrays.npz'),'source_events':len(base['eid']),
        'all_stored_count_net_upper_equal':True,'timings_seconds':timings,
        'speedup_median':statistics.median(timings['original'])/statistics.median(timings['binary_ctz']),
        'limits':['Compilation excluded; other validation jobs may run concurrently.','No production code changed; no end-to-end speed claim.']}
    atomic_json(ROOT/'validation/v041_india/ctz_microbenchmark.json',report)
    print(json.dumps(report,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
