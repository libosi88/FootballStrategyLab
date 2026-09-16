"""Legacy migrations disclose policy changes and preserve original evidence."""
import copy,json,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from lab import migration
from lab.common import DEFAULT,VALIDATION_DEFAULT,ROOT,atomic_json,canonical,read_json,sha
from lab.data import inspect
from lab.store import Store
from test_v031 import quote,write_csv

class RebuildMigration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='fsl_migration_test_');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'input.csv'
        write_csv(self.source,[quote('legacy-match')])
        self.manifest=inspect([str(self.source)],self.root/'cache');self.store=Store(self.root/'work')
    def legacy(self,**values):
        jid=self.store.create('真实甲组',copy.deepcopy(VALIDATION_DEFAULT),self.manifest,True)
        cfg=self.store.get(jid)['config'];cfg.update(engine_hash='old-engine',created_version='0.4.8',**values);cfg.pop('research_objective',None)
        with self.store.conn() as db:db.execute('UPDATE jobs SET config=? WHERE id=?',(canonical(cfg),jid))
        atomic_json(self.store.root/jid/'config.json',cfg);return jid
    def test_old_conflict_is_explicit_without_new_job(self):
        jid=self.legacy(min_matches=80);before=self.store.get(jid)
        result=migration.rebuild_stale_jobs(self.store.root)
        self.assertEqual(result['rebuilt'],[]);self.assertEqual(result['status'],'FAIL')
        self.assertIn('--profile standard',result['failed'][0]['hint'])
        self.assertEqual(len(self.store.jobs()),1);self.assertEqual(self.store.get(jid),before)
    def test_batch_standard_migration_records_changes_and_keeps_old_job(self):
        jid=self.legacy(min_matches=80,max_streak=5);before=self.store.get(jid)
        result=migration.rebuild_stale_jobs(self.store.root,profile='standard',job_ids=[jid])
        self.assertEqual(result['status'],'PASS');row=result['rebuilt'][0];new=self.store.get(row['job'])
        self.assertEqual((new['config']['min_matches'],new['status']),(40,'PAUSED'))
        self.assertEqual(new['config']['research_objective'],'validation')
        self.assertEqual(row['config_changes']['min_matches']['before'],80)
        self.assertEqual(row['config_changes']['min_matches']['after'],40)
        self.assertFalse(row['config_changes']['max_streak']['after_present'])
        proof=read_json(self.store.root/row['job']/row['provenance_file'])
        self.assertEqual(proof['config_changes'],row['config_changes']);self.assertFalse(row['old_checkpoint_reused'])
        self.assertEqual(self.store.get(jid),before)
    def test_default_preserves_validation_and_budgets(self):
        limits={'standard_node_budget':8192,'select_budget':12,'selection_eval_budget':200000,'max_run_minutes':120}
        jid=self.legacy(**limits);result=migration.rebuild_job(self.store.root,jid);cfg=self.store.get(result['job'])['config']
        self.assertEqual((cfg['research_objective'],cfg['holdout_months']),('validation',12))
        for k,v in limits.items():self.assertEqual(cfg[k],v);self.assertEqual(result['settings'][k],v)
        self.assertTrue(result['warnings'])
    def test_explicit_historical_config_is_disclosed(self):
        jid=self.legacy(min_matches=80,standard_node_budget=8192,max_run_minutes=120)
        overrides={k:v for k,v in DEFAULT.items() if k!='company'};snapshot=copy.deepcopy(overrides)
        row=migration.rebuild_job(self.store.root,jid,config_overrides=overrides);cfg=self.store.get(row['job'])['config']
        self.assertEqual((cfg['research_objective'],cfg['holdout_months'],cfg['min_profit']),('historical',0,'10'))
        self.assertEqual(cfg['standard_node_budget'],0);self.assertEqual(cfg['company'],'皇冠')
        self.assertEqual(overrides,snapshot);self.assertIn('research_objective',row['config_changes'])
    def test_invalid_explicit_threshold_is_not_overwritten(self):
        jid=self.legacy(min_matches=80)
        with self.assertRaisesRegex(ValueError,'min_matches'):migration.rebuild_job(self.store.root,jid,profile='standard',config_overrides={'min_matches':80})
        self.assertEqual(len(self.store.jobs()),1)
    def test_identity_override_is_refused(self):
        jid=self.legacy()
        for key in ('engine_hash','created_version','research_partition','acceptance_scope'):
            with self.subTest(key=key),self.assertRaises(ValueError):migration.rebuild_job(self.store.root,jid,config_overrides={key:'changed'})
        self.assertEqual(len(self.store.jobs()),1)
    def test_dry_run_is_readonly_even_with_queue_requested(self):
        jid=self.legacy(min_matches=80)
        before={str(p.relative_to(self.store.root)):sha(p) for p in self.store.root.rglob('*') if p.is_file()}
        with patch.object(migration,'Store',side_effect=AssertionError('dry-run opened mutating Store')):
            result=migration.rebuild_stale_jobs(self.store.root,profile='standard',job_ids=[jid],dry_run=True,queue=True)
        self.assertEqual(result['status'],'PASS');self.assertEqual(result['rebuilt'],[])
        self.assertEqual(result['planned'][0]['status'],'READY');self.assertTrue(result['planned'][0]['would_queue'])
        self.assertEqual({str(p.relative_to(self.store.root)):sha(p) for p in self.store.root.rglob('*') if p.is_file()},before)
    def test_missing_workspace_is_not_created_by_preview(self):
        missing=self.root/'absent'
        with self.assertRaises(ValueError):migration.rebuild_stale_jobs(missing,dry_run=True)
        self.assertFalse(missing.exists())
    def test_filter_prevents_other_jobs_from_rebuilding(self):
        jid=self.legacy(min_matches=80);other=self.legacy(min_matches=80)
        result=migration.rebuild_stale_jobs(self.store.root,profile='standard',job_ids=[jid])
        self.assertEqual([r['source_job'] for r in result['rebuilt']],[jid])
        self.assertIn(other,[r['source_job'] for r in result['skipped']]);self.assertEqual(len(self.store.jobs()),3)
    def test_unknown_requested_job_is_failure(self):
        result=migration.rebuild_stale_jobs(self.store.root,job_ids=['absent'])
        self.assertEqual(result['status'],'FAIL');self.assertEqual(result['failed'][0]['source_job'],'absent')
    def test_repeated_rebuild_preserves_provenance_and_identity(self):
        jid=self.legacy(min_matches=80);first=migration.rebuild_job(self.store.root,jid,profile='standard')
        proof=self.store.root/first['job']/first['provenance_file'];before=sha(proof)
        second=migration.rebuild_job(self.store.root,jid,profile='standard')
        self.assertEqual(first['job'],second['job']);self.assertEqual(sha(proof),before);self.assertEqual(len(self.store.jobs()),2)
    def test_missing_input_does_not_hide_success_of_another_job(self):
        good=self.legacy();bad=self.legacy();manifest=copy.deepcopy(self.manifest)
        manifest['files'][0]['path']=str(self.root/'absent.csv')
        with self.store.conn() as db:db.execute('UPDATE jobs SET manifest=? WHERE id=?',(canonical(manifest),bad))
        result=migration.rebuild_stale_jobs(self.store.root)
        self.assertEqual(result['status'],'PARTIAL')
        self.assertEqual([r['source_job'] for r in result['rebuilt']],[good]);self.assertEqual([r['source_job'] for r in result['failed']],[bad])
    def test_queue_is_explicit_and_leaves_old_job_paused(self):
        jid=self.legacy(min_matches=80)
        row=migration.rebuild_stale_jobs(self.store.root,queue=True,profile='standard',job_ids=[jid])['rebuilt'][0]
        self.assertEqual(row['status'],'QUEUED');self.assertTrue(row['queued'])
        self.assertEqual(self.store.get(jid)['status'],'PAUSED');self.assertEqual(self.store.get(row['job'])['status'],'QUEUED')
    def test_cli_failed_json_means_nonzero_exit(self):
        self.legacy(min_matches=80)
        r=subprocess.run([sys.executable,'-B',str(ROOT/'app.py'),'rebuild-stale','--workspace',str(self.store.root)],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertTrue(json.loads(r.stdout)['failed']);self.assertEqual(r.returncode,1,r.stderr)
    def test_cli_profile_filter_config_preview(self):
        jid=self.legacy(min_matches=80);config=self.root/'override.json'
        atomic_json(config,{'research_objective':'historical','holdout_months':0,'min_profit':'10'})
        r=subprocess.run([sys.executable,'-B',str(ROOT/'app.py'),'rebuild-stale','--workspace',str(self.store.root),'--job',jid,'--profile','standard','--config',str(config),'--dry-run'],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(r.returncode,0,r.stderr);body=json.loads(r.stdout)
        self.assertEqual(body['planned'][0]['settings']['research_objective'],'historical');self.assertEqual(len(self.store.jobs()),1)

    def test_preview_reads_committed_wal_and_keeps_original_files_unchanged(self):
        jid=self.legacy(min_matches=80);conn=sqlite3.connect(self.store.path)
        try:
            cfg=self.store.get(jid)['config'];cfg['standard_node_budget']=4321
            conn.execute('UPDATE jobs SET config=? WHERE id=?',(canonical(cfg),jid));conn.commit()
            paths=[p for p in self.store.root.glob('registry.sqlite3*') if p.is_file()]
            self.assertTrue(any(p.name.endswith('-wal') and p.stat().st_size for p in paths))
            before={p.name:sha(p) for p in paths}
            r=migration.rebuild_stale_jobs(self.store.root,profile='standard',job_ids=[jid],dry_run=True)
            self.assertEqual(r['planned'][0]['settings']['standard_node_budget'],4321)
            self.assertEqual({p.name:sha(p) for p in self.store.root.glob('registry.sqlite3*') if p.is_file()},before)
        finally:conn.close()
    def test_historical_template_preserves_company_and_memory(self):
        jid=self.legacy(min_matches=80,standard_node_budget=8192,max_feature_cache_mb=64)
        r=migration.rebuild_job(self.store.root,jid,config_overrides=read_json(ROOT/'config/rebuild_historical.json'))
        cfg=self.store.get(r['job'])['config']
        self.assertEqual((cfg['research_objective'],cfg['min_matches'],cfg['min_profit']),('historical',40,'10'))
        self.assertEqual((cfg['company'],cfg['max_feature_cache_mb']),('皇冠',64));self.assertEqual(len(cfg['directions']),16)
        for name in migration.REBUILD_BUDGETS:self.assertEqual(cfg[name],0,name)
    def test_cli_mixed_success_failure_returns_nonzero(self):
        self.legacy(min_matches=40);self.legacy(min_matches=80)
        r=subprocess.run([sys.executable,'-B',str(ROOT/'app.py'),'rebuild-stale','--workspace',str(self.store.root)],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=30)
        body=json.loads(r.stdout);self.assertEqual(body['status'],'PARTIAL')
        self.assertEqual((len(body['rebuilt']),len(body['failed'])),(1,1));self.assertEqual(r.returncode,1)
    def test_single_cli_readonly_preview(self):
        jid=self.legacy(min_matches=80)
        r=subprocess.run([sys.executable,'-B',str(ROOT/'app.py'),'rebuild-job','--workspace',str(self.store.root),'--job',jid,'--profile','standard','--dry-run'],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(r.returncode,0,r.stderr);body=json.loads(r.stdout)
        self.assertEqual(body['status'],'READY');self.assertEqual(body['settings']['min_matches'],40);self.assertEqual(len(self.store.jobs()),1)

if __name__=='__main__':unittest.main()
