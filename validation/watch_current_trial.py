"""Read-only worker observation; writes only validation metrics, never controls jobs."""
from pathlib import Path
import sys,sqlite3,json,time,ctypes
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lab.common import read_json,atomic_json,canonical,source_fingerprint

def memory(pid):
    if not pid:return {}
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('faults',wintypes.DWORD)]+[(n,ctypes.c_size_t) for n in ('peak','rss','paged_peak','paged','nonpaged_peak','nonpaged','pagefile','pagefile_peak')]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True);psapi=ctypes.WinDLL('psapi',use_last_error=True)
    kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD);kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
    psapi.GetProcessMemoryInfo.argtypes=(wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD)
    handle=kernel.OpenProcess(0x410,False,pid)
    if not handle:return {'memory_unavailable':ctypes.get_last_error()}
    try:
        value=Counters();value.cb=ctypes.sizeof(value)
        if psapi.GetProcessMemoryInfo(handle,ctypes.byref(value),value.cb):return {'rss_bytes':value.rss,'peak_rss_bytes':value.peak}
        return {'memory_unavailable':ctypes.get_last_error()}
    finally:kernel.CloseHandle(handle)

def main():
    run=read_json(ROOT/'validation/current_india_run.json');jobdir=Path(run['jobdir']);workspace=Path(run['workspace'])
    output=ROOT/'validation/current_trial_metrics.json';old=read_json(output,{})
    peak=old.get('peak_worker_rss_bytes',0) if old.get('job')==run['job'] else 0
    with sqlite3.connect((workspace/'registry.sqlite3').resolve().as_uri()+'?mode=ro',uri=True,timeout=30) as conn:
        conn.row_factory=sqlite3.Row
        while True:
            row=dict(conn.execute('SELECT status,pid,progress,created,error FROM jobs WHERE id=?',(run['job'],)).fetchone())
            progress=json.loads(row['progress']);mem=memory(row['pid']);peak=max(peak,mem.get('peak_rss_bytes',0))
            directions={}
            for folder in (jobdir/'mining').glob('*'):
                if not folder.is_dir():continue
                state=read_json(folder/'state.json',{})
                directions[folder.name]={k:state.get(k) for k in ('status','nodes','representatives','proven_nonprofitable','proven_zero','remaining','candidates')}
                directions[folder.name]['sqlite_bytes']=sum(p.stat().st_size for p in folder.glob('*.sqlite3') if p.is_file())
            value={'job':run['job'],'engine_hash':run['engine_hash'],'engine_unchanged':source_fingerprint()==run['engine_hash'],
                   'sample_time':time.time(),'wall_seconds':time.time()-row['created'],'status':row['status'],'worker_pid':row['pid'],
                   'progress':progress,'memory':mem,'peak_worker_rss_bytes':peak,'directions':directions,
                   'scope':'Worker RSS only; package/test child peaks excluded. Counts are committed checkpoints, not estimated full coverage.',
                   'worker_error':row['error']}
            atomic_json(output,value)
            with (ROOT/'validation/current_trial_samples.jsonl').open('a',encoding='utf-8') as f:f.write(canonical(value)+'\n')
            if row['status'] in ('DONE','PARTIAL_RESULT','ERROR','INTERRUPTED','PAUSED') or not value['engine_unchanged']:break
            time.sleep(10)

if __name__=='__main__':main()
