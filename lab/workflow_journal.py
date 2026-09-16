"""Durable S0-S7 stage evidence; completion is earned, never inferred from a ZIP."""
from pathlib import Path
import time,sys
from .common import atomic_json,read_json,digest,sha,ROOT,source_fingerprint

class StageJournal:
    def __init__(self,root,binding):
        self.path=Path(root)/'workflow_stages.json'
        self.data=read_json(self.path,{'schema':'FSL_S0_S7_V1','binding':binding,'command':sys.argv[:],'stages':{f'S{i}':{'status':'PENDING','started':None,'finished':None,'evidence':{}} for i in range(8)}})
        if self.data['binding']!=binding:raise ValueError('阶段账身份与研究版本不符')
    def start(self,stage):
        row=self.data['stages'][stage];now=time.time();row.update(status='RUNNING',started=row['started'] or now,attempt_started=now,finished=None);atomic_json(self.path,self.data)
    def finish(self,stage,status='PASS',**evidence):
        if status not in ('PASS','FAIL','BLOCKED','NOT_APPLICABLE','PARTIAL'):raise ValueError('阶段状态无效')
        row=self.data['stages'][stage];row.update(status=status,finished=time.time());row['evidence'].update(evidence);atomic_json(self.path,self.data)
    def current_failure(self,error):
        for row in self.data['stages'].values():
            if row['status']=='RUNNING':row.update(status='FAIL',finished=time.time(),error=str(error))
        atomic_json(self.path,self.data)

    def pause(self,reason):
        now=time.time()
        for stage,row in self.data['stages'].items():
            if row['status']=='RUNNING':
                row.update(status='PARTIAL',finished=now)
                row.setdefault('pause_events',[]).append({'at':now,'reason':str(reason),'attempt_started':row.get('attempt_started',row['started'])})
                row['evidence']['pause_reason']=str(reason)
        atomic_json(self.path,self.data)

def verify_engine(workspace,update,should_pause=None):
    """Actual independent reference/unit suite, reused only for identical code/tests."""
    import subprocess,os,platform,importlib.metadata
    tests=[p for p in sorted((ROOT/'tests').glob('test_*.py'))]
    frozen_engine=source_fingerprint();frozen_tests=[(p.name,sha(p)) for p in tests]
    runtime={'python':sys.version,'platform':platform.platform(),'machine':platform.machine(),'dependencies':{name:importlib.metadata.version(name) for name in ('numpy','numba','llvmlite')}}
    from .release import release_fingerprint
    frozen_release=release_fingerprint()
    binding=digest({'engine':frozen_engine,'release':frozen_release,'tests':frozen_tests,'runtime':runtime})
    root=Path(workspace)/'engine_validation'/binding;root.mkdir(parents=True,exist_ok=True)
    from .locking import WorkspaceLock,WorkspaceBusy
    # Parallel jobs of the same engine share one selftest: the others wait and reuse its report.
    while True:
        try:
            with WorkspaceLock(root,'.selftest.lock'):
                return _verify_engine_locked(root,binding,frozen_engine,frozen_tests,runtime,update,should_pause,frozen_release)
        except WorkspaceBusy:
            if should_pause and should_pause():
                from .mining import Paused
                raise Paused()
            update(stage='S1',message='另一任务正在运行相同的引擎自检，等待复用其结果')
            time.sleep(2)

def _verify_engine_locked(root,binding,frozen_engine,frozen_tests,runtime,update,should_pause,frozen_release=None):
    import subprocess,os
    report=root/'report.json';saved=read_json(report)
    if (isinstance(saved,dict) and saved.get('status')=='PASS' and saved.get('binding')==binding
        and saved.get('engine_hash')==frozen_engine and saved.get('runtime')==runtime
        and saved.get('source_and_tests_unchanged') is True and (root/'tests.log').is_file()
        and saved.get('log_sha256')==sha(root/'tests.log')):
        return {**saved,'log':str(root/'tests.log'),'reused':True}
    update(stage='S1',message='实际运行当前引擎的独立参考、边界、恢复与执行状态测试')
    env=os.environ.copy();env['PYTHONUTF8']='1';env['PYTHONDONTWRITEBYTECODE']='1'
    started=time.time()
    with (root/'tests.log').open('w',encoding='utf-8') as log:
        from .packaging import checked_process
        process=checked_process([sys.executable,'-B','-m','lab.engine_selftest'],should_pause,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
    unchanged=source_fingerprint()==frozen_engine and [(p.name,sha(p)) for p in sorted((ROOT/'tests').glob('test_*.py'))]==frozen_tests
    if frozen_release is not None:
        from .release import release_fingerprint
        unchanged=unchanged and release_fingerprint()==frozen_release
    result={'binding':binding,'engine_hash':frozen_engine,'runtime':runtime,'status':'PASS' if process.returncode==0 and unchanged else 'FAIL','source_and_tests_unchanged':unchanged,'seconds':time.time()-started,'log':str(root/'tests.log'),'log_sha256':sha(root/'tests.log'),'reused':False}
    atomic_json(report,result)
    if process.returncode or not unchanged:raise RuntimeError('标准引擎自检未通过或验收期间代码改变，见 '+str(root/'tests.log'))
    return result
