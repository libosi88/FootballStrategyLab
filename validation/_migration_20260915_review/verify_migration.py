"""Real legacy input verification; all actual rebuilds use a disposable registry."""
import json,subprocess,sys,tempfile,time,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from lab.common import atomic_json,canonical,read_json,sha,source_fingerprint
from lab.release import release_fingerprint
from lab.migration import _read_rebuild_jobs,rebuild_stale_jobs,REBUILD_BUDGETS
from lab.store import Store
AUDIT=Path(__file__).resolve().parent
IDS=('20260914_013241_08d308','20260913_043346_f7b1a9','20260913_155530_8db033')

def file_hashes():
    paths=list((ROOT/'workspace').glob('registry.sqlite3*'))
    for jid in IDS:paths.extend(ROOT/'workspace'/jid/n for n in ('config.json','input_manifest.json','workflow_stages.json','coverage.json'))
    return {str(p.relative_to(ROOT)):sha(p) for p in paths if p.is_file()}

def main():
    out=AUDIT/'real_legacy_validation.json'
    if out.exists():raise FileExistsError('Evidence already exists; do not overwrite.')
    started=time.time();engine=source_fingerprint();release=release_fingerprint()
    report={'status':'RUNNING','engine_hash':engine,'release_hash':release,'real_jobs_created_or_queued':0,
            'scope':'Real input hashes and isolated metadata migration; research pipeline not executed.'}
    before=file_hashes();atomic_json(AUDIT/'real_metadata_before.json',before)
    try:
        old={j['id']:j for j in _read_rebuild_jobs(ROOT/'workspace') if j['id'] in IDS}
        assert set(old)==set(IDS),'Missing requested job.'
        inputs={r['path']:r['sha256'] for j in old.values() for r in j['manifest']['files']}
        assert all(sha(p)==h for p,h in inputs.items())
        config=read_json(ROOT/'config/rebuild_historical.json')
        preserve=rebuild_stale_jobs(ROOT/'workspace',job_ids=list(IDS),dry_run=True)
        assert len(preserve['planned'])==1 and len(preserve['failed'])==2,preserve['counts']
        standard=rebuild_stale_jobs(ROOT/'workspace',profile='standard',job_ids=list(IDS),dry_run=True)
        historical=rebuild_stale_jobs(ROOT/'workspace',config_overrides=config,job_ids=list(IDS),dry_run=True)
        for plan in (standard,historical):assert plan['status']=='PASS' and len(plan['planned'])==3 and not plan['failed'],plan['counts']
        for row in historical['planned']:
            c=row['config'];assert (c['research_objective'],c['holdout_months'],c['min_profit'],c['min_matches'])==('historical',0,'10',40)
            assert len(c['directions'])==16 and all(c[k]==0 for k in REBUILD_BUDGETS)
        for name,plan in (('preserved_policy',preserve),('current_standard',standard),('full_history',historical)):atomic_json(AUDIT/('preview_'+name+'.json'),plan)
        assert file_hashes()==before,'Read-only preview changed source metadata.'
        with tempfile.TemporaryDirectory(prefix='fsl_real_legacy_migration_') as td:
            work=Path(td)/'workspace';store=Store(work)
            columns=('id','created','league','status','pause','config','manifest','progress','error','pid')
            with store.conn() as db:
                for jid in IDS:
                    values=[canonical(old[jid][k]) if k in ('config','manifest','progress') else old[jid][k] for k in columns]
                    db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',values)
            applied=rebuild_stale_jobs(work,profile='standard',job_ids=list(IDS))
            assert applied['status']=='PASS' and len(applied['rebuilt'])==3,applied
            assert all(store.get(r['job'])['status']=='PAUSED' for r in applied['rebuilt'])
            repeat=rebuild_stale_jobs(work,profile='standard',job_ids=list(IDS))
            assert [(r['source_job'],r['job']) for r in repeat['rebuilt']]==[(r['source_job'],r['job']) for r in applied['rebuilt']]
            converted=rebuild_stale_jobs(work,config_overrides=config,job_ids=list(IDS))
            assert converted['status']=='PASS' and len(converted['rebuilt'])==3,converted
            for r in converted['rebuilt']:
                j=store.get(r['job']);assert j['status']=='PAUSED' and j['config']['research_objective']=='historical'
                assert not (work/r['job']/'events.jsonl.gz').exists()
                proof=read_json(work/r['job']/r['provenance_file'])
                assert proof['config_changes']==r['config_changes'] and proof['old_checkpoint_reused'] is False
            for jid in IDS:assert store.get(jid)==old[jid]
            cmd=[sys.executable,'-B',str(ROOT/'app.py'),'rebuild-stale','--workspace',str(work),'--job',IDS[1]]
            proc=subprocess.run(cmd,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=60)
            body=json.loads(proc.stdout);assert proc.returncode==1 and body['failed'] and not body['rebuilt']
            report.update(isolated_standard_successes=3,isolated_historical_successes=3,idempotence=True,
                          cli_failure_exit_code=proc.returncode,source_jobs_preserved=True,old_checkpoints_reused=False,
                          historical_settings=[r['settings'] for r in historical['planned']])
        assert file_hashes()==before and all(sha(p)==h for p,h in inputs.items())
        assert source_fingerprint()==engine and release_fingerprint()==release
        report.update(status='PASS',original_metadata_unchanged=True,original_inputs_unchanged=True,
                      temporary_workspace_removed=not Path(td).exists(),original_input_count=len(inputs))
    except BaseException as error:
        report.update(status='FAIL',error=str(error),traceback=traceback.format_exc());raise
    finally:
        report['seconds']=round(time.time()-started,3);atomic_json(out,report)
        print(json.dumps({k:report.get(k) for k in ('status','isolated_standard_successes','isolated_historical_successes','original_metadata_unchanged','real_jobs_created_or_queued')},ensure_ascii=False))

if __name__=='__main__':main()
