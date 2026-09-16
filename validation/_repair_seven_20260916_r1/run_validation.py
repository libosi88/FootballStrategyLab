"""Source-bound repair acceptance. Synthetic tests only; never opens real jobs."""
from __future__ import annotations
import argparse
import ast
import concurrent.futures
import difflib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback
import zipfile

EVIDENCE=Path(__file__).resolve().parent
PROJECT=EVIDENCE.parent.parent
sys.path.insert(0,str(PROJECT))
from lab.common import VERSION,atomic_json,source_fingerprint,sha
from lab.release import release_files,release_fingerprint,build_snapshot


def manifest():
    return {p.relative_to(PROJECT).as_posix():sha(p) for p in release_files(PROJECT)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--workers',type=int,default=3,choices=(1,2,3))
    args=parser.parse_args();output=args.output_dir.resolve()
    if EVIDENCE not in output.parents:raise ValueError('Use a new output directory under this repair evidence directory')
    output.mkdir(parents=True,exist_ok=False)
    sources=manifest();engine=source_fingerprint();release=release_fingerprint();started=time.time()
    report={'status':'RUNNING','scope':'SEVEN_REPAIRS_PLUS_ADJACENT_BATCH_AND_SYNTHETIC_SOFTWARE_ACCEPTANCE',
            'version':VERSION,'engine_hash':engine,'release_hash':release,'output_directory':str(output),
            'real_jobs_started':False,'real_orders_enabled':False,'checks':{},'source_manifest':sources,
            'limitations':['No real-league search rerun','No v3_full exhaustive-scope certification',
                           'UI checks are actual JavaScript rendering plus isolated HTTP, not full-browser visual acceptance',
                           'Dependencies checked offline; no new online vulnerability scan','No Git commit or tag was created']}
    atomic_json(output/'source_identity.json',{'engine_hash':engine,'release_hash':release,'files':sources})

    def save():
        report['seconds']=round(time.time()-started,3)
        atomic_json(EVIDENCE/'repair_result.json',report)
        atomic_json(output/'repair_result.json',report)

    def check(name,argv,inner=None,timeout=3600):
        log=output/(name+'.log');result={'status':'RUNNING','command':argv,'log':str(log)}
        print('CHECK_START '+name,flush=True);t=time.time()
        try:
            env=os.environ.copy();env['PYTHONUTF8']='1';env['PYTHONDONTWRITEBYTECODE']='1'
            env.pop('PYTHONPATH',None);env.pop('FSL_RUN_STANDARD_PIPELINE',None);env.pop('FSL_RUN_NONEMPTY_PIPELINE',None)
            with log.open('x',encoding='utf-8') as stream:
                run=subprocess.run(argv,cwd=PROJECT,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=timeout)
            text=log.read_text(encoding='utf-8',errors='replace')
            count=re.search(r'Ran (\d+) tests',text);skips=re.search(r'OK \(skipped=(\d+)\)',text)
            result.update(exit_code=run.returncode,tests=int(count[1]) if count else None,
                          skipped=int(skips[1]) if skips else 0,log_sha256=sha(log),source_unchanged=manifest()==sources)
            good=run.returncode==0 and result['source_unchanged']
            if inner is not None:
                detail=json.loads(inner.read_text(encoding='utf-8'));result['evidence']=str(inner)
                result['detail']={k:detail.get(k) for k in ('status','engine_hash','release_hash','tests','case','state','matches','selected','remaining','source_unchanged','frozen_copy_unchanged')}
                good=good and detail.get('status')=='PASS' and detail.get('engine_hash')==engine and detail.get('release_hash')==release
            result['status']='PASS' if good else 'FAIL'
            if not good:result['log_tail']=text[-10000:]
        except BaseException as error:
            result.update(status='FAIL',error=str(error),traceback=traceback.format_exc())
        result['seconds']=round(time.time()-t,3)
        atomic_json(output/(name+'.json'),result)
        print('CHECK_END '+name+' '+result['status'],flush=True)
        return result

    try:
        save();errors=[]
        for name in sources:
            if name.endswith('.py'):
                try:ast.parse((PROJECT/name).read_text(encoding='utf-8-sig'),filename=name)
                except Exception as error:errors.append({'file':name,'error':str(error)})
        pins={}
        for name in ('requirements.lock','installer.lock'):
            pins.update(dict(re.findall(r'^([A-Za-z0-9_.-]+)==([^\s\\]+)',(PROJECT/name).read_text(encoding='utf-8'),re.M)))
        installed={name:importlib.metadata.version(name) for name in pins}
        sbom=json.loads((PROJECT/'sbom.cdx.json').read_text(encoding='utf-8'))
        sbom_hash=sbom['metadata']['component']['hashes'][0]['content']
        consistency={'status':'PASS' if not errors and installed==pins and sbom_hash==engine else 'FAIL',
                     'syntax_errors':errors,'locked':pins,'installed':installed,'sbom_engine_hash':sbom_hash,
                     'network_used':False,'vulnerability_scan_performed':False}
        report['checks']['syntax_and_locked_dependencies']=consistency
        atomic_json(output/'syntax_and_locked_dependencies.json',consistency)
        report['checks']['pip_check']=check('pip_check',[sys.executable,'-B','-m','pip','check'],timeout=120)
        report['checks']['targeted']=check('targeted',[sys.executable,'-B','-m','unittest','discover','-s','tests','-p','test_seven_repair*.py','-v'],timeout=180)
        save()
        if any(c['status']!='PASS' for c in report['checks'].values()):raise RuntimeError('Preflight/targeted check failed; pipeline acceptance not started')
        destination=output/'regression'
        report['checks']['frozen_regression']=check('frozen_regression',[sys.executable,'-B','validation/frozen_regression.py','--output-dir',str(destination)],destination/'frozen_regression.json',timeout=2100)
        save()
        if report['checks']['frozen_regression']['status']!='PASS':raise RuntimeError('Frozen regression failed; pipeline acceptance not started')
        commands=[]
        for suffix,flag in [('empty','--standard-empty'),('nonempty','--standard-nonempty')]:
            name='validation_'+suffix;dest=output/name
            commands.append((name,[sys.executable,'-B','validation/frozen_regression.py','--output-dir',str(dest),flag],dest/('standard_'+suffix+'_acceptance.json')))
        for case in ('empty','full','limited'):
            name='historical_'+case;dest=output/name
            commands.append((name,[sys.executable,'-B','validation/historical_acceptance.py','--case',case,'--output-dir',str(dest)],dest/'acceptance.json'))
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures={executor.submit(check,*item,timeout=4200):item[0] for item in commands}
            for future in concurrent.futures.as_completed(futures):
                report['checks'][futures[future]]=future.result();save()
        report['source_unchanged']=manifest()==sources
        if not report['source_unchanged'] or any(c['status']!='PASS' for c in report['checks'].values()):raise RuntimeError('Acceptance failed or source identity changed')
        report['source_snapshot']=build_snapshot(output/'verified_source.zip')
        report['status']='PASS_SCOPED_REPAIR'
    except BaseException as error:
        report.update(status='PARTIAL',error=str(error),traceback=traceback.format_exc(),source_unchanged=manifest()==sources)
    finally:
        baseline=json.loads((EVIDENCE/'baseline_identity.json').read_text(encoding='utf-8'))['source_manifest']
        current=manifest();changed=[name for name in sorted(set(baseline)|set(current)) if baseline.get(name)!=current.get(name)]
        report['changes_since_repair_baseline']=changed;lines=[]
        with zipfile.ZipFile(EVIDENCE/'baseline_source.zip') as archive:
            for name in changed:
                try:
                    old=archive.read(name).decode('utf-8-sig') if name in baseline else ''
                    new=(PROJECT/name).read_text(encoding='utf-8-sig') if name in current else ''
                except UnicodeError:
                    lines.append('Binary file changed: '+name+'\n');continue
                lines.extend(difflib.unified_diff(old.splitlines(keepends=True),new.splitlines(keepends=True),fromfile='a/'+name if name in baseline else '/dev/null',tofile='b/'+name if name in current else '/dev/null'))
        (output/'changes_vs_repair_baseline.patch').write_text(''.join(lines),encoding='utf-8',newline='\n')
        save()
        print(json.dumps({k:report[k] for k in ('status','engine_hash','release_hash','output_directory','seconds')},ensure_ascii=False),flush=True)
    return 0 if report['status']=='PASS_SCOPED_REPAIR' else 1


if __name__=='__main__':raise SystemExit(main())
