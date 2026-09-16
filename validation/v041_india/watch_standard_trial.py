"""Observe the real production worker and exercise one safe pause/resume."""
from pathlib import Path
from datetime import datetime,timezone
import sys,json,time,sqlite3,ctypes,shutil,traceback,os
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from lab.common import read_json,atomic_json,source_fingerprint,sha,canonical
from lab.store import Store

def worker_memory(pid):
    if not pid:return {}
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(n,ctypes.c_size_t) for n in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True);psapi=ctypes.WinDLL('psapi',use_last_error=True)
    kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD);kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
    psapi.GetProcessMemoryInfo.argtypes=(wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD)
    handle=kernel.OpenProcess(0x410,False,pid)
    if not handle:return {'memory_unavailable':ctypes.get_last_error()}
    try:
        out=Counters();out.cb=ctypes.sizeof(out)
        if not psapi.GetProcessMemoryInfo(handle,ctypes.byref(out),out.cb):return {'memory_unavailable':ctypes.get_last_error()}
        return {'rss_bytes':int(out.WorkingSetSize),'peak_rss_bytes':int(out.PeakWorkingSetSize)}
    finally:kernel.CloseHandle(handle)

def main():
    evidence=ROOT/'validation/v041_india'; launch=read_json(evidence/'standard_run.json')
    workspace=Path(launch['workspace']);jobdir=Path(launch['jobdir']);jid=launch['job'];expected=launch['engine_hash']
    config=read_json(jobdir/'config.json');manifest=read_json(jobdir/'input_manifest.json')
    report={'status':'OBSERVING','job':jid,'jobdir':str(jobdir),'physical_jobdir':str(jobdir.resolve()),'engine_hash':expected,
        'scope':'Real India all-input, 16-direction standard fixed-budget end-to-end trial; not exhaustive standard completion',
        'started_utc':datetime.now(timezone.utc).isoformat(),'pause_test':{'requested':False,'completed':False},'peak_worker_rss_bytes':0,
        'resource_limit_note':'OS worker peak only; independent package/test subprocess peaks are not included. Storage includes all generated files.',
        'monitor_errors':[],'initial_source_hashes':{x['path']:x['sha256'] for x in manifest['files']}}
    last_check=0;last_print=0;last_phase=None;pause_at=None;resume_at=None;last_sample=0;generated_bytes=0;started=time.monotonic()
    terminal=None;phase_started=started;phase_times={}
    try:
        with sqlite3.connect((workspace/'registry.sqlite3').resolve().as_uri()+'?mode=ro',uri=True,timeout=30) as conn, (evidence/'standard_samples.jsonl').open('a',encoding='utf8') as samples:
            conn.row_factory=sqlite3.Row
            while True:
                row=conn.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone();assert row is not None
                job=dict(row);progress=json.loads(job['progress']);now=time.monotonic()
                phase=(progress.get('stage'),progress.get('direction'),job['status'])
                memory=worker_memory(job['pid']);report['peak_worker_rss_bytes']=max(report['peak_worker_rss_bytes'],memory.get('peak_rss_bytes',0))
                if now-last_check>=15:
                    if source_fingerprint()!=expected:
                        Store(workspace).control(jid,'pause');raise RuntimeError('Engine changed during trial; requested safe pause')
                    generated_bytes=0
                    for path in jobdir.rglob('*'):
                        try:
                            if path.is_file():generated_bytes+=path.stat().st_size
                        except FileNotFoundError:pass
                    if shutil.disk_usage(jobdir).free<config['min_free_disk_mb']*1048576:
                        Store(workspace).control(jid,'pause');raise RuntimeError('Free disk below frozen safeguard; requested safe pause')
                    last_check=now
                # Exercise production control while real S2 state is being built.
                # Do not change a single search budget or rule when resuming.
                if not report['pause_test']['requested'] and job['status']=='RUNNING' and progress.get('stage')=='S2' and progress.get('mask_atoms',0)>=1024:
                    pause_at=now;Store(workspace).control(jid,'pause')
                    report['pause_test'].update(requested=True,requested_progress=progress,requested_worker_pid=job['pid'])
                if report['pause_test']['requested'] and not report['pause_test']['completed'] and job['status']=='PAUSED':
                    if json.loads(job['config'])!=config or read_json(jobdir/'config.json')!=config:raise AssertionError('Config changed around pause')
                    report['pause_test'].update(pause_seconds=now-pause_at,paused_progress=progress,config_unchanged=True)
                    Store(workspace).control(jid,'resume');resume_at=now;report['pause_test']['completed']=True
                sample={'elapsed_seconds':now-started,'status':job['status'],'pid':job['pid'],'progress':progress,'generated_bytes':generated_bytes,**memory}
                if now-last_sample>=2 or phase!=last_phase:
                    samples.write(canonical(sample)+'\n');samples.flush();atomic_json(evidence/'standard_progress.json',sample);last_sample=now
                if phase!=last_phase:
                    if last_phase is not None:
                        key='|'.join(str(x) for x in last_phase);phase_times[key]=phase_times.get(key,0)+(now-phase_started)
                    phase_started=now;last_phase=phase
                if now-last_print>=45:
                    print(canonical({'elapsed_seconds':round(now-started,1),'status':job['status'],'stage':progress.get('stage'),'direction':progress.get('direction'),'message':progress.get('message'),'nodes':progress.get('search_nodes'),'mask_atoms':progress.get('mask_atoms'),'mask_total':progress.get('mask_atoms_total'),'rss_bytes':memory.get('rss_bytes')}),flush=True);last_print=now
                if job['status'] in ('DONE','PARTIAL_RESULT','ERROR','INTERRUPTED'):
                    terminal=job;break
                if job['status']=='PAUSED' and report['pause_test']['completed'] and resume_at is not None and now-resume_at>10:
                    terminal=job;break
                time.sleep(.5)
        report['worker_status']=terminal['status'];report['worker_error']=terminal['error']
        report['engine_unchanged']=source_fingerprint()==expected
        report['input_hashes_unchanged']=all(sha(Path(name))==value for name,value in report['initial_source_hashes'].items())
        report['config_unchanged']=read_json(jobdir/'config.json')==config
        report['summary']=read_json(jobdir/'summary.json')
        report['status']='WORKER_FINISHED_PENDING_INDEPENDENT_REVIEW' if terminal['status'] in ('DONE','PARTIAL_RESULT') else 'WORKER_NEEDS_ATTENTION'
    except Exception as error:
        report.update(status='OBSERVER_OR_WORKER_ERROR',error=repr(error),traceback=traceback.format_exc())
    finally:
        report.update(wall_seconds=time.monotonic()-started,phase_wall_seconds=phase_times,generated_bytes_last_sample=generated_bytes,finished_utc=datetime.now(timezone.utc).isoformat())
        atomic_json(evidence/'standard_observation.json',report)
        print(canonical({'status':report['status'],'report':str(evidence/'standard_observation.json'),'pause_test':report['pause_test'].get('completed')}),flush=True)
    if report['status']!='WORKER_FINISHED_PENDING_INDEPENDENT_REVIEW':raise SystemExit(1)

if __name__=='__main__':main()
