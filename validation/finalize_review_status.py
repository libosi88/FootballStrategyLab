"""Finalize only the supplied current evidence; never rewrite frozen source/docs."""
import argparse,datetime,json,shutil,sqlite3,sys
from pathlib import Path
from contextlib import closing
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab.common import ROOT,VERSION,atomic_json,source_fingerprint,read_json,sha
from lab.release import release_files,release_fingerprint

def require(value,reason):
    if not value:raise ValueError(reason)

def executable_manifest_matches(proof,current):
    summaries={'README_先读.md','本版实测与未完成项.md','docs/当前交付范围.md','docs/v3_实现与验收进度.md'}
    protected=lambda name:name not in summaries
    saved=proof.get('source_manifest',{})
    return {k:v for k,v in current.items() if protected(k)}=={k:v for k,v in saved.items() if protected(k)}

def finalize(evidence):
    evidence=Path(evidence).resolve();proof=read_json(evidence/'frozen_regression.json',{})
    require(proof.get('status')=='PASS' and proof.get('version')==VERSION,'缺少、未通过或过期的隔离回归证据')
    engine=source_fingerprint();release=release_fingerprint();current={p.relative_to(ROOT).as_posix():sha(p) for p in release_files()}
    require(proof.get('status')=='PASS' and proof.get('engine_hash')==engine and proof.get('release_hash')==release,'最终隔离回归或当前源码/发布指纹不匹配')
    require(proof.get('source_unchanged') and proof.get('frozen_copy_unchanged') and proof.get('source_manifest')==current,'最终回归未证明完整冻结源码一致')
    sbom=read_json(ROOT/'sbom.cdx.json',{}).get('metadata',{}).get('component',{})
    require(sbom.get('version')==VERSION and sbom.get('hashes',[{}])[0].get('content')==engine,'SBOM不匹配')
    # The default historical research objective has its own whole-chain acceptance; validation-mode chains alone do not certify it.
    reports={'engine':'frozen_regression.json','empty_pipeline':'standard_empty_acceptance.json','nonempty_pipeline':'standard_nonempty_acceptance.json',
             'historical_empty':'historical_empty/acceptance.json','historical_full':'historical_full/acceptance.json','historical_limited':'historical_limited/acceptance.json',
             'dependencies':'dependencies.json','ui':'ui_validation.json'}
    entries={}
    for key,name in reports.items():
        path=evidence/name;report=read_json(path,{})
        require(report.get('status')=='PASS','缺失或未通过本轮证据: '+name)
        if key.endswith('pipeline'):
            require(report.get('engine_hash')==engine and report.get('source_unchanged') and report.get('frozen_copy_unchanged') and executable_manifest_matches(report,current),'管线验收执行源码、测试或配置不同: '+name)
        if key.startswith('historical_'):
            before=read_json(path.parent/'source_before.json',{})
            require(report.get('engine_hash')==engine and report.get('source_unchanged') and executable_manifest_matches({'source_manifest':before.get('files',{})},current),'历史模式整链验收执行源码、测试或配置不同: '+name)
        if key=='ui':require(report.get('engine_hash')==engine and report.get('version')==VERSION,'UI不是当前引擎验收')
        if key=='dependencies':
            require(report.get('locks')=={n:sha(ROOT/n) for n in ('requirements.lock','installer.lock')},'依赖核验锁文件不匹配')
        entries[key]={'report':str(path.relative_to(ROOT)) if ROOT in path.parents else str(path),'sha256':sha(path),'scope':report.get('scope')}
    jobs=[];registry=ROOT/'workspace/registry.sqlite3'
    if registry.is_file():
        with closing(sqlite3.connect(registry.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
            jobs=[dict(zip(('id','status','pid'),row)) for row in conn.execute('SELECT id,status,pid FROM jobs')]
    require(not any(r['status'] in ('QUEUED','RUNNING','PAUSING') for r in jobs),'原研究工作区有活动任务；不自动停止或混用其状态')
    state={'version':VERSION,'status':'SOFTWARE_REPAIR_VERIFIED_WITH_OPEN_RESEARCH_SCOPE','engine_hash':engine,'release_hash':release,
        'unit_tests':{'status':'PASS','count':proof['tests'],'source_unchanged':True,'frozen_copy_unchanged':True},
        'evidence':entries,'frozen_directory_relative':'validation/frozen_releases/'+VERSION+'_'+engine[:12],
        'real_league_jobs_started':False,'full_standard_league_search_certified':False,'live_enabled':False,'second_slot_enabled':False,
        'research_jobs':jobs,'open_items':['Physical directory name retained for compatibility','Real nonempty complete standard league search/handoff','Hundreds-of-leagues capacity','Real rolling/unseen data and upstream results','E01-E04, real money and second slot not enabled'],
        'note':'Software and explicitly synthetic layered acceptance only; pipeline metadata may precede final documentation summary, executable source/tests/config must be identical.'}
    for name in ('本版实际验收状态.json','最终交付状态.json'):
        path=ROOT/'validation'/name
        if path.is_file() and read_json(path,{}).get('engine_hash')!=engine:
            previous=ROOT/'validation/history'/('before_'+VERSION+'_'+name)
            previous.parent.mkdir(parents=True,exist_ok=True)
            if not previous.exists():shutil.copy2(path,previous)
        atomic_json(path,state)
    atomic_json(evidence/'working_status.json',state)
    require(source_fingerprint()==engine and release_fingerprint()==release,'发布状态时不得改写已验收源码或摘要')
    print(json.dumps({k:state[k] for k in ('version','status','engine_hash','release_hash','unit_tests')},ensure_ascii=False))
    return state

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--evidence-dir',type=Path,required=True);args=parser.parse_args()
    finalize(args.evidence_dir)
if __name__=='__main__':main()
