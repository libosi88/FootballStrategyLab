"""Atomically preserve the supplied verified source and layered evidence."""
import argparse,datetime,json,os,shutil,stat,subprocess,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab.common import ROOT,VERSION,source_fingerprint,sha,read_json
from lab.release import release_files,release_fingerprint

def require(value,reason):
    if not value:raise ValueError(reason)

def preserve(evidence):
    evidence=Path(evidence).resolve();proof=read_json(evidence/'frozen_regression.json',{});state=read_json(evidence/'working_status.json',{})
    engine=source_fingerprint();release=release_fingerprint();expected={p.relative_to(ROOT).as_posix():sha(p) for p in release_files()}
    require(proof.get('status')=='PASS' and proof.get('engine_hash')==engine and proof.get('release_hash')==release and proof.get('source_manifest')==expected,'源码未通过本轮冻结回归')
    require(state.get('status')=='SOFTWARE_REPAIR_VERIFIED_WITH_OPEN_RESEARCH_SCOPE' and state.get('engine_hash')==engine and state.get('release_hash')==release,'先完成本轮finalize，不读取旧状态')
    for item in state['evidence'].values():require(sha(ROOT/item['report'])==item['sha256'],'认证证据文件改变')
    parent=ROOT/'validation/frozen_releases';parent.mkdir(parents=True,exist_ok=True);target=parent/(VERSION+'_'+engine[:12])
    if target.exists():
        previous=read_json(target/'frozen_source_manifest.json',{})
        require(previous.get('source_files')==expected,'既有冻结副本不同，拒绝覆盖')
        require(all(sha(target/name)==h for name,h in {**previous['source_files'],**previous['evidence_files']}.items()),'既有冻结副本损坏，拒绝覆盖')
        return {'directory':str(target),'status':'EXISTING_UNCHANGED'}
    with tempfile.TemporaryDirectory(prefix='new_frozen_',dir=parent) as temporary:
        fresh=Path(temporary)/'source';fresh.mkdir()
        for name,h in expected.items():
            p=fresh/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/name,p);require(sha(p)==h,'冻结复制源码不一致')
        dest=fresh/'evidence';dest.mkdir()
        for name in ('frozen_regression.json','frozen_regression.log','standard_empty_acceptance.json','standard_empty_acceptance.log','standard_nonempty_acceptance.json','standard_nonempty_acceptance.log','dependencies.json','ui_validation.json','working_status.json','修复交付报告.md'):
            path=evidence/name
            if path.is_file():shutil.copy2(path,dest/name)
        for scope in ('empty','nonempty'):
            source=evidence/('synthetic_full16_'+scope);packet=read_json(source/'acceptance.json',{})
            require(packet.get('status')=='PASS','合成验收摘要缺失或失败: '+scope)
            folder=dest/('synthetic_'+scope);folder.mkdir();shutil.copy2(source/'acceptance.json',folder/'acceptance.json')
            job=Path(packet.get('jobdir') or source/'workspace'/packet['job'])
            for kind in ('developer','audit'):
                archive=job/'packages'/packet['packages'][kind]
                require(sha(archive)==packet['packages'][kind+'_sha256'],'验收交接包改变')
                shutil.copy2(archive,folder/archive.name)
            for name in ('independent_final_A_replay.log',):
                if (source/name).is_file():shutil.copy2(source/name,folder/name)
        for case in ('empty','full','limited'):
            source=evidence/('historical_'+case);packet=read_json(source/'acceptance.json',{})
            require(packet.get('status')=='PASS' and packet.get('engine_hash')==engine,'历史模式整链验收摘要缺失或失败: '+case)
            folder=dest/('historical_'+case);folder.mkdir()
            for name in ('acceptance.json','source_before.json'):shutil.copy2(source/name,folder/name)
        hashes={p.relative_to(fresh).as_posix():sha(p) for p in dest.rglob('*') if p.is_file()}
        record={'version':VERSION,'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'scope':'VERIFIED_SOFTWARE_AND_SYNTHETIC_LAYERED_ACCEPTANCE_NOT_REAL_LEAGUE_CERTIFICATE','engine_hash':engine,'release_hash':release,'tests':proof['tests'],
            'source_files':expected,'evidence_files':hashes,'policy':'Immutable revision; later changes require another separate freeze. Read-only attributes are not signatures.'}
        (fresh/'frozen_source_manifest.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        (fresh/'冻结说明.md').write_text('# 固定复核对象 '+VERSION+'\n\n源码逐文件与最终隔离回归清单一致。证据含合成90场16分支每方向10节点非空链路、合成微型全字典空链路、真实A/B ZIP、UI及依赖核验；这些不是完整真实联赛/容量/滚动/未来成交证书。\n\nengine_hash: '+engine+'\n\nrelease_hash: '+release+'\n\n文件清单见frozen_source_manifest.json；只读属性与SHA仅防误改及核对一致性，不是来源签名。原日常入口不变，原真实任务不启动。\n',encoding='utf-8')
        require(release_fingerprint(fresh)==release and source_fingerprint()==engine and release_fingerprint()==release,'冻结前后指纹不同')
        os.replace(fresh,target)
    for p in target.rglob('*'):
        if p.is_file():p.chmod(stat.S_IREAD)
    env=os.environ.copy();env.pop('PYTHONPATH',None)
    command="import json; from lab.common import source_fingerprint; from lab.release import release_fingerprint; print(json.dumps({'engine_hash':source_fingerprint(),'release_hash':release_fingerprint()}))"
    run=subprocess.run([sys.executable,'-B','-c',command],cwd=target,env=env,text=True,capture_output=True,check=True)
    actual=json.loads(run.stdout);require(actual=={'engine_hash':engine,'release_hash':release},'冻结副本独立指纹不同')
    return {'directory':str(target),'status':'PRESERVED_VERIFIED','tests':proof['tests'],'source_files':len(expected),'engine_hash':engine,'release_hash':release}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--evidence-dir',type=Path,required=True);args=parser.parse_args()
    print(json.dumps(preserve(args.evidence_dir),ensure_ascii=False))
if __name__=='__main__':main()
