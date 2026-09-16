"""Portable A/B archives verified from their actual extracted contents."""
from pathlib import Path
from contextlib import contextmanager
import os,sys,shutil,subprocess,tempfile,zipfile,time,copy,uuid
from .common import *
from .workflow_gates import completion_state
from .release import freeze_release_tree,manifest_fingerprint,release_fingerprint


def check_pause(should_pause):
    if should_pause and should_pause():
        from .mining import Paused
        raise Paused()

@contextmanager
def disposable_directory(prefix, directory):
    """Remove private packaging files without hiding a pause or validation failure on Windows."""
    path=Path(tempfile.mkdtemp(prefix=prefix,dir=directory))
    try:
        yield path
    finally:
        failure=None;deadline=time.monotonic()+15
        while path.exists():
            try:
                shutil.rmtree(path)
                failure=None
                break
            except OSError as error:
                failure=error
                if os.name!='nt' or time.monotonic()>=deadline:
                    break
                time.sleep(.1)
        # A pause or the validation error is more actionable than a stale Windows handle.
        if failure is not None and sys.exc_info()[0] is None:
            raise failure

def zip_file(z,path,name,should_pause=None):
    with path.open('rb') as source,z.open(str(name).replace('\\','/'),'w',force_zip64=True) as dest:
        while True:
            check_pause(should_pause);chunk=source.read(1024*1024)
            if not chunk:break
            dest.write(chunk)

def checked_process(command,should_pause=None,idle_timeout=3600,**kwargs):
    """Run a validation child. A slow but progressing child is not killed by wall time; it
    stops on pause, or after idle_timeout seconds without any new output."""
    if should_pause is None:return subprocess.run(command,timeout=1800,**kwargs)
    process=subprocess.Popen(command,**kwargs);stream=kwargs.get('stdout')
    def output_size():
        try:return os.fstat(stream.fileno()).st_size
        except (AttributeError,OSError,ValueError):return None
    size=output_size();changed=time.monotonic()
    try:
        while process.poll() is None:
            check_pause(should_pause)
            current=output_size()
            if current!=size:size=current;changed=time.monotonic()
            elif time.monotonic()-changed>idle_timeout:raise subprocess.TimeoutExpired(command,idle_timeout)
            time.sleep(.2)
        return process
    finally:
        if process.poll() is None:
            # Also stop helpers the validation child started (for example a test UI server) on pause or timeout.
            if os.name=='nt':subprocess.run(['taskkill','/T','/F','/PID',str(process.pid)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            else:process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()

def zip_tree(source,target,should_pause=None):
    temporary=target.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED,compresslevel=3) as z:
        for p in sorted(source.rglob('*')):
            if p.is_file() and '__pycache__' not in p.parts:zip_file(z,p,p.relative_to(source),should_pause)
    atomic_replace(temporary,target)


def content_manifest(root):
    return {p.relative_to(root).as_posix():sha(p) for p in sorted(root.rglob('*')) if p.is_file() and p.name!='manifest.json' and '__pycache__' not in p.parts}


def package_run(jobdir,summary,coverage,trigger,engine,journal,update,should_pause=None,holdout=None):
    """Publish one complete A/B bundle; failed private builds never earn completion."""
    from .data import safe_extract
    root=Path(jobdir);out=root/'packages';out.mkdir(exist_ok=True)
    check_pause(should_pause)
    if read_json(root/'config.json').get('engine_hash')!=source_fingerprint():raise ValueError('打包时引擎已改变，拒绝交付混合版本')
    frozen_inputs=read_json(root/'input_manifest.json')['files'];verify_input_records(frozen_inputs)
    if read_json(root/'config.json').get('profile')=='standard':
        from .workflow_gates import module_coverage
        if not module_coverage(root,coverage,engine,trigger)['evidence_complete']:raise ValueError('搜索证据缺失或改变，拒绝交付')
    prospective=completion_state(coverage,summary,trigger,engine,True,read_json(root/'config.json'),holdout=holdout)
    bundle_name='verified_'+uuid.uuid4().hex
    # The temporary parent is on the job's volume, outside the advertised packages
    # directory. Only a fully checked child bundle is renamed into that directory.
    with disposable_directory('handoff_bundle_',root) as private:
        source_snapshot=private/'source_snapshot'
        source_manifest=freeze_release_tree(source_snapshot,ROOT)
        source_release={'schema':'FSL_SOURCE_RELEASE_V1','release_hash':manifest_fingerprint(source_manifest),'files':source_manifest}
        bundle=private/'bundle';bundle.mkdir()
        a=bundle/('A_历史研究触发交接包.zip' if prospective['state']=='HISTORICAL_RESEARCH_COMPLETE' and not prospective.get('empty_roster_chain_verified') else 'A_标准研究触发交接包.zip' if prospective['state']=='STANDARD_HANDOFF_COMPLETE' else 'A_空结果链路验收包.zip' if prospective.get('empty_roster_chain_verified') else 'A_有限范围观察开发包.zip')
        b=bundle/'B_本次研究审计包.zip'
        a_relative=(Path(bundle_name)/a.name).as_posix()
        b_relative=(Path(bundle_name)/b.name).as_posix()
        build=private/'developer_contents';build.mkdir()
        for name in ('events.jsonl.gz','labels.json','config.json','data_audit.json','coverage.json','workflow_status.json','workflow_stages.json','coverage_modules.json','engine_validation.json','engine_tests.log','search_spec.json','standard_feature_preparation.json','holdout_plan.json'):
            if (root/name).exists():shutil.copy2(root/name,build/name)
        (build/'results').mkdir()
        # Only selected-roster evidence belongs in A; full candidate databases remain in B.
        for folder in (root/'results',root/'results/lower_risk'):
            if not folder.exists():continue
            dest=build/'results'/folder.relative_to(root/'results');dest.mkdir(exist_ok=True)
            for p in folder.iterdir():
                if not p.is_file() or p.suffix in ('.sqlite3','.db') or p.name.startswith(('全部','直接邻域','单条报价','高风险','低样本')):continue
                shutil.copy2(p,dest/p.name)
        # Both packages use one private snapshot and exactly its approved files.
        for name in source_manifest:
            dest=build/name;dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source_snapshot/name,dest)
        atomic_json(build/'source_release_manifest.json',source_release)
        # The heartbeat only prints when verification advanced, so a hung replay goes silent and
        # checked_process stops it on idle, while a slow but progressing replay is never killed.
        (build/'verify_handoff.py').write_text('from pathlib import Path\nfrom lab.verify import verify,verification_passed\nimport json,threading,time\nprogress={"checks":0}\ndef tick():\n    progress["checks"]+=1\n    return False\ndef heartbeat():\n    seen=None\n    while True:\n        time.sleep(60)\n        if progress["checks"]!=seen:\n            seen=progress["checks"];print("verification checkpoints",seen,flush=True)\nthreading.Thread(target=heartbeat,daemon=True).start()\nr=verify(Path(__file__).resolve().parent,should_pause=tick)\nprint(json.dumps(r,ensure_ascii=False,indent=2))\nraise SystemExit(0 if verification_passed(r) else 1)\n',encoding='utf-8')
        (build/'README_CN.md').write_text('''# 单联赛研究与触发交接包

本包的研究状态以 workflow_status.json 为准。名单位于 results/rules.json；较低风险备选位于 results/lower_risk/rules.json，二者不可叠加使用。原始终场标签仅供验收结算，绝不能进入未来报价。

使用64位Python 3.11—3.13，在本目录执行：

    python -B -m pip install --require-hashes --only-binary=:all: -r installer.lock
    python -B -m pip install --require-hashes --only-binary=:all: -r requirements.lock
    python -B -m lab.engine_selftest
    python -B verify_handoff.py

运行模拟流（标准JSON逐行输入；无行情订阅或真实下单）：

    python -B app.py paper-stream --rules results/rules.json --state paper_state.sqlite3 --input quotes.jsonl --output paper_results.jsonl

报价格式见 results/event.schema.json，所有字段和跨字段约束会校验。实时输入先发送 match_start 声明从开场完整观察，或 history 回补完整历史；按完整分钟发送 watermark，再形成同场同分钟的完整批次。历史回补只消费历史信号，不补下过去订单。进程重启复用同一state文件；未知/部分成交继续预占名额，确认取消后才释放未成交部分。成交回执也是模拟输入。只有明确离线完整历史，才加 --archive-history；--auto-fill 仅代表假定模拟成交。各控制消息的准确字段见 docs/标准流程与模拟接口.md。

固定名单评测：

    python -B app.py evaluate-frozen --rules results/rules.json 新数据目录 --output evaluation_v1

评测先冻结名单再读取输入，不重新选择。是否真正未见取决于数据此前用途；本包三年样本不能自行改称未来验证。

features.json、完整触发卡、rule_replay_cases.jsonl.gz提供特征解释、精确条件、真实正例与封盘负例，boundary_cases.json提供临界值。逐条历史信号和五种报价情景订单均会核对（s0–s3 经流式协调器，进球前拒单为档案反事实，由冻结订单与模拟账对照）；SQLite模拟账另做真实重启与幂等验收。测试使用包内合成数据和独立参考实现，不能替代外部赛果与成交证据。空名单会明确报EMPTY_ROSTER。

所有本包路径相对本目录。manifest.json覆盖最终内容。engine_validation是研究前引擎验收，handoff_validation是本包空目录实际验收；已安装的同版本依赖可复用，不声称进行了联网重装。真实下注、第二笔和跨联赛账户执行均关闭。
''',encoding='utf-8')
        atomic_json(build/'manifest.json',content_manifest(build));zip_tree(build,a,should_pause)
        # Test the actual ZIP with no source-project PYTHONPATH or implicit caches.
        with disposable_directory('fsl_handoff_check_',root) as check:
            safe_extract(a,check)
            if content_manifest(check)!=read_json(check/'manifest.json'):raise ValueError('A包首次解压内容不符')
            env=os.environ.copy();env.pop('PYTHONPATH',None);env['PYTHONUTF8']='1';env['PYTHONDONTWRITEBYTECODE']='1'
            commands=[[sys.executable,'-B','-m','lab.engine_selftest'],[sys.executable,'-B','verify_handoff.py']]
            logs=[]
            for index,command in enumerate(commands):
                update(stage='S7',message='在实际A包解压目录执行'+('完整引擎测试' if index==0 else '逐条历史触发和持久化模拟核对'))
                log=root/f'handoff_test_{index}.log';started=time.time()
                with log.open('w',encoding='utf-8') as stream:
                    process=checked_process(command,should_pause,cwd=check,env=env,stdout=stream,stderr=subprocess.STDOUT)
                logs.append({'command':command[1:],'exit_code':process.returncode,'seconds':time.time()-started,'log':log.name,'sha256':sha(log)})
                if process.returncode:raise RuntimeError('交接包空目录验收失败: '+str(log))
                shutil.copy2(log,build/log.name)
            report=read_json(check/'results/trigger_verification.json')
            from .verify import verification_passed
            if not verification_passed(report):raise RuntimeError('交接触发或实际执行经济资格未通过')
            # No network reinstall happens here; at least prove the reused runtime matches the locked versions.
            import importlib.metadata,re
            lock=source_snapshot/'requirements.lock';pins=dict(re.findall(r'^([A-Za-z0-9_.-]+)==([^\s\\]+)',lock.read_text(encoding='utf-8'),re.M)) if lock.is_file() else {}
            installed={}
            for name in pins:
                try:installed[name]=importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError:installed[name]=None
            if any(installed[name]!=version for name,version in pins.items()):raise RuntimeError('已安装依赖版本与requirements.lock不一致，空目录验收不能代替按README安装: '+canonical(installed))
            validation={'status':report['status'],'fresh_zip_extract':True,'installed_dependencies_reused':True,'installed_versions_match_lock':installed,'source_tree_not_on_pythonpath':True,
                'commands':logs,'signal_verification':report,'final_metadata_only_reseal':True}
        # Archives receive prospective metadata in the private build only. Neither
        # the durable root state nor the live journal is completed before publication.
        completed_stages=copy.deepcopy(journal.data)
        completed_stages['stages']['S7'].update(status='PASS',finished=time.time())
        completed_stages['stages']['S7']['evidence'].update(developer_package=a_relative,fresh_directory_verification=report['status'])
        atomic_json(build/'workflow_status.json',prospective)
        atomic_json(build/'workflow_stages.json',completed_stages)
        atomic_json(build/'handoff_validation.json',validation)
        # The conclusion and gap notes are written only after the actual fresh-directory verification.
        from .reporting import final_decision_markdown,gap_notes_markdown
        decision=final_decision_markdown(root,summary,coverage,trigger,prospective,validation);gaps=gap_notes_markdown(prospective,coverage)
        audit_text={'results/最终决定.md':decision,'results/缺口说明.md':gaps}
        for name,value in audit_text.items():
            target=build/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(value.encode('utf-8'))
        atomic_json(build/'manifest.json',content_manifest(build));zip_tree(build,a,should_pause)
        with disposable_directory('fsl_final_zip_integrity_',root) as final:
            safe_extract(a,final)
            if content_manifest(final)!=read_json(final/'manifest.json') or content_manifest(final)!=content_manifest(build):raise ValueError('最终封装A包与已验收代码/证据不符')
            if any(sha(final/name)!=expected for name,expected in source_manifest.items()):raise ValueError('A包源码与冻结发布清单不一致')
        validation.update(package_sha256=sha(a),final_zip_fresh_extract_integrity='PASS')
        packages={'developer':a_relative,'audit':b_relative,'developer_sha256':sha(a),'fresh_directory_verification':report['status']}
        # B cannot contain its own SHA. Its adjacent final certificate supplies both
        # hashes; B's metadata is serialized directly instead of mutating the root.
        audit_metadata={'workflow_status.json':prospective,'workflow_stages.json':completed_stages,
            'summary.json':{**summary,**prospective,'coverage':coverage,'verification':trigger,'packages':packages},
            'packaging_verification.json':validation,'source_release_manifest.json':source_release}
        required_handoff_logs={rec['log'] for rec in logs}
        omitted_logs=[];rebuildable=[];members=[]
        from .standard_mining import DERIVED_CACHES
        for p in sorted(root.rglob('*')):
            rel=p.relative_to(root)
            if not p.is_file() or rel.as_posix() in audit_metadata or rel.as_posix() in audit_text or any(x in rel.parts for x in ('packages','handoff_build','__pycache__')) or any(x.startswith(('handoff_build_','handoff_bundle_','fsl_handoff_check_','fsl_final_zip_integrity_','feature_preparation_')) for x in rel.parts) or p.suffix=='.tmp':continue
            # Direction caches rebuilt deterministically from arrays.npz + atoms.sqlite3 are listed, not archived.
            if len(rel.parts)>=3 and rel.parts[0]=='mining' and any(rel.parts[2]==n or rel.parts[2].startswith(n+'-') for n in DERIVED_CACHES):
                rebuildable.append({'path':rel.as_posix(),'bytes':p.stat().st_size});continue
            if p.suffix=='.log' and (p.name.startswith('worker') or p.name.startswith('handoff_test_') and rel.as_posix() not in required_handoff_logs):
                omitted_logs.append({'path':rel.as_posix(),'bytes':p.stat().st_size});continue
            members.append((p,rel))
        # Uncompressed upper bound; stop before a multi-GB archive fails on the last step.
        estimate=sum(p.stat().st_size for p,_ in members)+sum(Path(r['path']).stat().st_size for r in frozen_inputs)
        if shutil.disk_usage(bundle).free<estimate+int(read_json(root/'config.json').get('min_free_disk_mb',256))*1048576:
            from .mining import ResourcePaused
            raise ResourcePaused(f'B审计包压缩前约{estimate/1048576:.0f} MiB，工作盘剩余空间不足；已暂停，未发布不完整交接包')
        temporary=b.with_suffix('.zip.tmp')
        with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED,compresslevel=3) as z:
            for p,rel in members:zip_file(z,p,rel,should_pause)
            z.writestr('audit_log_policy.json',canonical({'retained_handoff_logs':sorted(required_handoff_logs),'omitted_runtime_or_obsolete_logs':omitted_logs,'rebuildable_caches_not_archived':rebuildable,'reason':'保留本次验收引用的日志；工作进程流水日志留在原任务，不重复交付；可由arrays.npz与atoms.sqlite3确定性重建的方向缓存只登记大小'}))
            for name,value in audit_metadata.items():z.writestr(name,canonical(value))
            for name,value in audit_text.items():z.writestr(name,value)
            for rec in read_json(root/'input_manifest.json')['files']:
                p=Path(rec['path']);zip_file(z,p,'original_inputs/'+rec['sha256'][:12]+'_'+p.name,should_pause)
            for name in source_manifest:zip_file(z,source_snapshot/name,'software/'+name,should_pause)
            z.writestr('00_审计复跑说明.md','# 全量研究审计\n\n所有原始表达以atoms.sqlite3 + standard_plan.json + search.sqlite3可逆保存；盈利家族按任务冻结的min_profit收录（历史默认净胜>10，0表示净胜>0全收；验证模式净胜>10）。software/lab/standard_cli.py提供export-candidates逐条展开。研究断点绑定原始输入和代码哈希；迁移后复核使用A包，重新研究使用新工作区和original_inputs。各联赛独立，不合并收益。B自身哈希见相邻交接验收证书。\n')
        atomic_replace(temporary,b)
        with zipfile.ZipFile(b) as z:
            if z.testzip() is not None:raise ValueError('最终B审计包CRC校验失败')
            import hashlib
            if len(z.namelist())!=len(set(z.namelist())):raise ValueError('B审计包存在重复条目')
            if {name[len('software/'):] for name in z.namelist() if name.startswith('software/')}!=set(source_manifest):raise ValueError('B包软件文件集合与发布白名单不一致')
            for name,expected in source_manifest.items():
                h=hashlib.sha256()
                with z.open('software/'+name) as member:
                    for chunk in iter(lambda:member.read(1024*1024),b''):h.update(chunk)
                if h.hexdigest()!=expected:raise ValueError('B包源码与冻结发布清单不一致: '+name)
            for rec in frozen_inputs:
                name='original_inputs/'+rec['sha256'][:12]+'_'+Path(rec['path']).name;h=hashlib.sha256()
                with z.open(name) as member:
                    for chunk in iter(lambda:member.read(1024*1024),b''):h.update(chunk)
                if h.hexdigest()!=rec['sha256']:raise ValueError('B包原始输入与冻结哈希不一致: '+name)
            for name in z.namelist():
                if name.startswith('mining/') and name.endswith('/search_evidence.json'):
                    seal=json.loads(z.read(name));parent=name.rsplit('/',1)[0]
                    for filename,expected in seal['files'].items():
                        h=hashlib.sha256()
                        with z.open(parent+'/'+filename) as member:
                            for chunk in iter(lambda:member.read(1024*1024),b''):h.update(chunk)
                        if h.hexdigest()!=expected:raise ValueError('B包搜索证据与封存清单不一致: '+name)
            for name,value in audit_metadata.items():
                if z.read(name)!=canonical(value).encode('utf-8'):raise ValueError('最终B审计包元数据校验失败: '+name)
            for name,value in audit_text.items():
                if z.read(name)!=value.encode('utf-8'):raise ValueError('最终B审计包结论校验失败: '+name)
        packages['audit_sha256']=sha(b)
        validation['audit_zip_crc']='PASS'
        certificate={'workflow':prospective,'packages':packages,'final_zip_integrity':'PASS',
            'audit_zip_crc':'PASS','engine_tests_in_fresh_directory':True,'publication':'atomic_directory_rename',
            'source_release_hash':source_release['release_hash'],'source_file_count':len(source_manifest),'same_whitelisted_source_snapshot':True}
        atomic_json(bundle/'交接验收证书.json',certificate)
        if read_json(root/'config.json').get('engine_hash')!=source_fingerprint():raise ValueError('打包期间引擎已改变，拒绝发布混合版本')
        if release_fingerprint(ROOT)!=source_release['release_hash']:raise ValueError('打包期间发布源码集合已改变，拒绝发布混合版本')
        verify_input_records(frozen_inputs)
        # No completion rollback is needed on any earlier exception, including
        # out-of-space failures. Both archives and their certificate become visible
        # together before any root completion marker is written.
        check_pause(should_pause)
        atomic_replace(bundle,out/bundle_name)
        for name in audit_text:
            target=root/name;target.parent.mkdir(parents=True,exist_ok=True);atomic_replace(build/name,target)
        atomic_json(root/'packaging_verification.json',validation)
        atomic_json(journal.path,completed_stages);journal.data=completed_stages
        # Summary first: the completion marker is the last root file written.
        atomic_json(root/'summary.json',{**summary,**prospective,'coverage':coverage,'verification':trigger,'packages':packages})
        atomic_json(root/'workflow_status.json',prospective)
    return packages,prospective
