"""Seal 0.7.0 only after actual targeted, frozen and five synthetic pipelines pass.

No real-league jobs, live orders, Git resets or old-evidence overwrites.
"""
import ast
from datetime import datetime,timezone
import hashlib,json,os,shutil,subprocess,sys,time,traceback,uuid,zipfile
from concurrent.futures import ThreadPoolExecutor,as_completed
from threading import RLock
from pathlib import Path

ROOT=next(p for p in Path(__file__).resolve().parents if (p/'lab/common.py').is_file())
BASE=ROOT/'validation/_rationality_finish_20260916_r1'
BASE.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT))
from lab.common import VERSION,source_fingerprint,execution_fingerprint,atomic_json,sha
from lab.release import release_files,release_fingerprint,build_snapshot

def identity():
    return {'research_hash':source_fingerprint(),'execution_hash':execution_fingerprint(),
            'release_hash':release_fingerprint(ROOT)}

def main():
    if VERSION!='0.7.0':raise RuntimeError('This acceptance is only for the reviewed 0.7.0 upgrade.')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8]
    out=BASE/('acceptance_'+stamp);out.mkdir(exist_ok=False)
    report={'status':'RUNNING','version':VERSION,'output_directory':str(out),'checks':{},
            'scope':'SCOPED_RATIONALITY_REPAIR_AND_SYNTHETIC_SOFTWARE_ACCEPTANCE',
            'real_jobs_started':False,'real_orders_sent':0,'git_commit_created':False,
            'limitations':['No real-league rerun','No v3_full exhaustive certificate',
                'No clean-machine installation','No new online vulnerability scan',
                'No full-browser visual acceptance','No external system deployment']}
    started=time.monotonic();frozen=None;sealed=False;report_lock=RLock()
    def save():
        with report_lock:
            report['seconds']=round(time.monotonic()-started,3)
            atomic_json(out/'finish_result.json',report)
    def check(name,command,evidence=None):
        print('RUN '+name,flush=True);log=out/(name+'.log');begin=time.monotonic()
        env=os.environ.copy();env.pop('PYTHONPATH',None)
        env.update(PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
        with log.open('x',encoding='utf-8') as stream:
            proc=subprocess.run(command,cwd=ROOT,env=env,stdout=stream,stderr=subprocess.STDOUT,
                timeout=3600,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        item={'status':'PASS' if proc.returncode==0 else 'FAIL','exit_code':proc.returncode,
              'command':command,'seconds':round(time.monotonic()-begin,3),'log':log.name,'log_sha256':sha(log)}
        if evidence:
            if not evidence.is_file():item.update(status='FAIL',reason='Missing acceptance JSON')
            else:
                value=json.loads(evidence.read_text(encoding='utf-8-sig'))
                item['evidence']=evidence.relative_to(out).as_posix();item['evidence_sha256']=sha(evidence)
                item['detail']={k:value.get(k) for k in ('status','tests','case','state','matches','selected','remaining','engine_hash','release_hash','source_unchanged','frozen_copy_unchanged')}
                if value.get('status')!='PASS':item['status']='FAIL'
                if frozen and (value.get('engine_hash')!=frozen['research_hash'] or value.get('release_hash')!=frozen['release_hash']):
                    item.update(status='FAIL',reason='Acceptance identity mismatch')
        if frozen and identity()!=frozen:item.update(status='FAIL',reason='Source changed during acceptance')
        with report_lock:
            report['checks'][name]=item;save()
        print(name+' '+item['status'],flush=True)
        if item['status']!='PASS':raise RuntimeError(name+' failed; '+str(log))
    try:
        report['before_snapshot']=build_snapshot(out/'before_acceptance_source.zip',ROOT);save()
        check('presentation_sync',[sys.executable,'-B','validation/_rationality_upgrade_20260916_r1/update_presentation.py'])
        # Old numbers are retained as history, never reassigned to this upgrade.
        marker='<!-- FSL_070_CURRENT_ENTRY -->'
        for name in ('README_先读.md','本版实测与未完成项.md','docs/当前交付范围.md','docs/v3_实现与验收进度.md'):
            path=ROOT/name;text=path.read_text(encoding='utf-8-sig')
            if marker not in text:
                header=marker+'\n> 当前源码为0.7.0；下方0.6.3与旧日期的测试数字仅为历史记录，不能认证当前源码。\n'
                header+='> 当前功能见 docs/0.7.0_使用与验收入口.md；实际状态以 validation/_rationality_finish_20260916_r1/LATEST.json 指向的报告为准。\n\n'
                path.write_text(header+text,encoding='utf-8')
        check('sbom_sync',[sys.executable,'-B','validation/update_sbom.py'])
        for path in release_files(ROOT):
            if path.suffix=='.py':ast.parse(path.read_text(encoding='utf-8-sig'),filename=str(path))
        frozen=identity();report['identity']=frozen
        report['source_manifest']={p.relative_to(ROOT).as_posix():sha(p) for p in release_files(ROOT)}
        report['checks']['python_syntax']={'status':'PASS'};save()
        check('pip_check',[sys.executable,'-B','-m','pip','check'])
        node=shutil.which('node')
        if not node:raise RuntimeError('Node is required for the declared JavaScript checks.')
        for path in sorted((ROOT/'web').glob('*.js')):check('javascript_'+path.stem,[node,'--check',str(path)])
        check('rationality_targeted',[sys.executable,'-B','-m','unittest','discover','-s','tests','-p','test_rationality_070.py','-v'])
        extended=out/'extended'
        check('balanced_and_http',[sys.executable,'-B',str(BASE/'verify_extended.py'),'--output-dir',str(extended)],extended/'extended_acceptance.json')
        regression=out/'regression'
        check('frozen_regression',[sys.executable,'-B','validation/frozen_regression.py','--output-dir',str(regression)],regression/'frozen_regression.json')
        pipeline_checks=[]
        for case in ('empty','full','limited'):
            directory=out/('historical_'+case)
            pipeline_checks.append(('historical_'+case,[sys.executable,'-B','validation/historical_acceptance.py','--case',case,'--output-dir',str(directory)],directory/'acceptance.json'))
        for case in ('empty','nonempty'):
            directory=out/('validation_'+case)
            pipeline_checks.append(('validation_'+case,[sys.executable,'-B','validation/frozen_regression.py','--standard-'+case,'--output-dir',str(directory)],directory/('standard_'+case+'_acceptance.json')))
        # Independent synthetic workspaces; at most two active acceptance children.
        # Report writes are locked and every actual child must finish successfully.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(check,*args) for args in pipeline_checks]
            failures=[]
            for future in as_completed(futures):
                try:future.result()
                except Exception as error:failures.append(str(error))
            if failures:raise RuntimeError('; '.join(failures))
        if identity()!=frozen:raise RuntimeError('Final identity mismatch')
        report['source_snapshot']=build_snapshot(out/'verified_source.zip',ROOT)
        if report['source_snapshot']['release_fingerprint']!=frozen['release_hash']:raise RuntimeError('Snapshot identity mismatch')
        report['status']='PASS_SCOPED_070';save()
        summary=['# 0.7.0 修复与软件验收','', '**PASS_SCOPED_070：下列范围通过，不是全部潜在问题或真实联赛收益保证。**','',
                 '| 检查 | 状态 |','|---|---|']
        summary += ['| '+name+' | '+value['status']+' |' for name,value in report['checks'].items()]
        summary += ['', '全部整链为合成数据；full仅认证紧凑语法，limited通过表示正确保持PARTIAL。',
                    '原项目已应用修改；verified_source.zip是同版备份，不是另一个试用分支。',
                    '原始数据与旧断点保留；真实下单、第二笔未开放。',
                    '关闭旧界面服务再启动才会加载新服务端；不可直接改绑旧研究断点。',
                    '当前研究、执行及发布指纹见finish_result.json。',
                    '未验收：真实联赛完整搜索、完整v3穷尽、干净机安装、浏览器视觉、在线漏洞扫描、复盘系统接入。']
        (out/'修复与验收报告.md').write_text('\n'.join(summary)+'\n',encoding='utf-8')
        members={'verified_source.zip':out/'verified_source.zip','finish_result.json':out/'finish_result.json',
                 '修复与验收报告.md':out/'修复与验收报告.md',
                 'validation/_rationality_finish_20260916_r1/run_acceptance.py':Path(__file__).resolve(),
                 'validation/_rationality_finish_20260916_r1/verify_extended.py':BASE/'verify_extended.py',
                 'validation/_rationality_upgrade_20260916_r1/update_presentation.py':ROOT/'validation/_rationality_upgrade_20260916_r1/update_presentation.py'}
        for path in out.glob('*.log'):members['logs/'+path.name]=path
        for value in report['checks'].values():
            if value.get('evidence'):members['evidence/'+value['evidence']]=out/value['evidence']
        inventory={name:sha(path) for name,path in members.items()};target=out/'FSL_070_已验收源码与证据.zip'
        with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            for name,path in members.items():z.write(path,name)
            z.writestr('bundle_manifest.json',json.dumps(inventory,ensure_ascii=False,indent=2))
        with zipfile.ZipFile(target) as z:
            if z.testzip() or any(hashlib.sha256(z.read(n)).hexdigest()!=h for n,h in inventory.items()):raise RuntimeError('Evidence archive integrity failed')
        if identity()!=frozen:raise RuntimeError('Source changed during publication')
        atomic_json(out/'delivery.json',{'status':'PASS_SCOPED_070','identity':frozen,'bundle':target.name,'bundle_sha256':sha(target)})
        sealed=True;print('PASS_SCOPED_070 '+str(target),flush=True)
        return 0
    except Exception as error:
        report.update(status='FAILED_NOT_RELEASED',error=str(error),traceback=traceback.format_exc());save()
        print(str(error),file=sys.stderr,flush=True);return 1
    finally:
        if not sealed:save()
        atomic_json(BASE/'LATEST.json',{'status':report['status'],'report':(out/'finish_result.json').relative_to(ROOT).as_posix()})
        print('REPORT '+str(out/'finish_result.json'),flush=True)

if __name__=='__main__':raise SystemExit(main())
