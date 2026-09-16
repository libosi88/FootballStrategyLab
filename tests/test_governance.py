import json,re,tempfile,unittest,csv,copy
from pathlib import Path
from unittest.mock import patch
from lab.common import ROOT,VERSION,DEFAULT,DIRECTIONS,atomic_json,sha,source_fingerprint,digest,money_threshold,check_config
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.standard_feature_cache import direction_identity,check_direction_cache
from lab.store import Store,resumability
from lab.server import job_view
from lab.workflow_gates import update_league_registry
from lab.maintenance import cleanup_workspace
from lab.standard_mining import release_direction_caches
from lab.migration import rebuild_job
from lab.data import inspect
from lab.selection import metrics,Quotes
from lab.pool_diagnostics import all_pool_diagnostics
from test_cache_release import make_direction
from test_v031 import quote,write_csv

class Governance(unittest.TestCase):
    def test_sbom_matches_application_and_all_locked_distribution_hashes(self):
        sbom=json.loads((ROOT/'sbom.cdx.json').read_text(encoding='utf-8'))
        app=sbom['metadata']['component'];self.assertEqual(app['version'],VERSION)
        self.assertEqual(app['hashes'][0]['content'],source_fingerprint())
        expected=set()
        for name in ('requirements.lock','installer.lock'):expected.update(re.findall(r'--hash=sha256:([0-9a-f]{64})',(ROOT/name).read_text(encoding='utf-8')))
        actual={h['content'] for c in sbom['components'] for h in c['hashes']}
        self.assertEqual(actual,expected);self.assertEqual(len(sbom['components']),4)

    def test_cache_diagnostics_distinguish_source_config_input_scope_and_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path=root/'arrays.npz';path.write_bytes(b'cache')
            args=(root,[],{},100,'LIVE_GIVE',DEFAULT)
            binding,details=direction_identity(*args);metadata={'binding':binding,'binding_details':details,'sha256':sha(path)}
            check_direction_cache(metadata,binding,details,path)
            with patch('lab.standard_feature_cache.source_fingerprint',return_value='changed'):
                b,d=direction_identity(*args)
            with self.assertRaisesRegex(ValueError,'SOURCE_REVISION_MISMATCH'):check_direction_cache(metadata,b,d,path)
            for args2,code in (((root,[],{},100,'LIVE_GIVE',{**DEFAULT,'min_matches':81}),'CONFIGURATION_MISMATCH'),((root,[],{'changed':1},100,'LIVE_GIVE',DEFAULT),'INPUT_DATA_MISMATCH'),((root,[],{},1000,'LIVE_GIVE',DEFAULT),'SCOPE_OR_UNIT_MISMATCH')):
                b,d=direction_identity(*args2)
                with self.assertRaisesRegex(ValueError,code):check_direction_cache(metadata,b,d,path)
            path.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'CACHE_CONTENT_CHANGED'):check_direction_cache(metadata,binding,details,path)
            path.unlink()
            with self.assertRaisesRegex(ValueError,'CACHE_FILE_MISSING'):check_direction_cache(metadata,binding,details,path)

    def test_legacy_cache_does_not_guess_cause_without_details(self):
        with self.assertRaisesRegex(ValueError,'CACHE_BINDING_MISMATCH'):
            check_direction_cache({'binding':'old'},'new',{},Path('unused'))

    def test_old_task_resumability_is_explicit_in_ui_and_registry(self):
        with tempfile.TemporaryDirectory() as td:
            store=Store(td)
            with patch('lab.store.source_fingerprint',return_value='old'):jid=store.create('league',DEFAULT,{'files':[]},True)
            job=store.get(jid);self.assertFalse(resumability(job)['resumable'])
            self.assertFalse(job_view(store,job)['resumable'])
            row=update_league_registry(store)['experiments'][0]
            self.assertFalse(row['resumable']);self.assertEqual(row['resume_block_reason'],'SOURCE_REVISION_MISMATCH')

    def test_rebuild_is_paused_idempotent_and_keeps_old_task_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'input.csv';write_csv(source,[quote('match')]);store=Store(root/'workspace');manifest=inspect([str(source)],root/'cache')
            with patch('lab.store.source_fingerprint',return_value='old'):jid=store.create('真实甲组',DEFAULT,manifest,True)
            before=store.get(jid)
            result=rebuild_job(store.root,jid);again=rebuild_job(store.root,jid)
            self.assertEqual(result['job'],again['job']);self.assertEqual(result['status'],'PAUSED');self.assertFalse(result['automatic_research_started'])
            self.assertEqual(store.get(jid),before);self.assertEqual(sha(source),manifest['files'][0]['sha256'])

    def test_rebuild_standard_profile_writes_only_legal_config(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'input.csv';write_csv(source,[quote('match')]);store=Store(root/'workspace');manifest=inspect([str(source)],root/'cache')
            jid=store.create('真实甲组',DEFAULT,manifest,True)
            result=rebuild_job(store.root,jid,profile='standard');cfg=store.get(result['job'])['config']
            self.assertEqual(cfg['profile'],'standard');self.assertNotIn('include_supplement',cfg)
            self.assertEqual(set(cfg['directions']),set(DIRECTIONS))

    def test_job_view_tolerates_old_summary_without_v3_status(self):
        with tempfile.TemporaryDirectory() as td:
            store=Store(td);jid=store.create('A',DEFAULT,{'files':[]})
            atomic_json(store.root/jid/'summary.json',{'selected':0})
            view=job_view(store,store.get(jid))
            self.assertEqual(view['summary']['v3_status'],'未知');self.assertEqual(view['summary']['selected'],0)

    def test_settings_merge_drops_unknown_keys(self):
        dirty={**DEFAULT,'include_supplement':True,'paths':'C:\\data'}
        settings=check_config({k:v for k,v in dirty.items() if k in DEFAULT})
        self.assertNotIn('include_supplement',settings);self.assertEqual(settings['profile'],DEFAULT['profile'])

    def test_cleanup_releases_only_completed_verified_cache_and_keeps_similar_names(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);Store(root)
            complete=root/'job/mining/LIVE_GIVE';complete.mkdir(parents=True);make_direction(complete)
            atomic_json(complete/'state.json',{'status':'COMPLETE'});atomic_json(complete/'standard_features.json',{'sha256':sha(complete/'arrays.npz')});atomic_json(complete/'dictionary_complete.json',{'database_sha256':sha(complete/'atoms.sqlite3')})
            backup=complete/'masks.sqlite3.private_backup';backup.write_bytes(b'keep')
            paused=root/'job/mining/LIVE_RECEIVE';paused.mkdir();make_direction(paused);atomic_json(paused/'state.json',{'status':'PAUSED'})
            before=sha(complete/'arrays.npz');preview=cleanup_workspace(root)
            self.assertGreater(preview['eligible_bytes'],0);self.assertTrue((complete/'masks.sqlite3').exists())
            result=cleanup_workspace(root,True)
            self.assertEqual(result['freed_bytes'],preview['eligible_bytes']);self.assertEqual(sha(complete/'arrays.npz'),before)
            self.assertEqual(backup.read_bytes(),b'keep');self.assertTrue((paused/'masks.sqlite3').exists())
            self.assertEqual(cleanup_workspace(root,True)['freed_bytes'],0)

    def test_cleanup_rejects_unregistered_names_and_retains_active_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):release_direction_caches(td,{'status':'COMPLETE'},DEFAULT,names=('../',))
            root=Path(td);store=Store(root);store.create('league',DEFAULT,{'files':[]})
            folder=root/'job/mining/LIVE_GIVE';folder.mkdir(parents=True);make_direction(folder);atomic_json(folder/'state.json',{'status':'COMPLETE'})
            self.assertEqual(cleanup_workspace(root,True)['freed_bytes'],0);self.assertTrue((folder/'masks.sqlite3').exists())

    def test_zero_divisor_boundaries_and_zero_overlap_are_explicit(self):
        self.assertIsNone(metrics([],100,[])['roi'])
        for scale in (0,-1,False):
            with self.assertRaises(ValueError):metrics([],scale,[])
            with self.assertRaises(ValueError):Quotes([],{},scale,5)
            with self.assertRaises(ValueError):money_threshold('10',scale)
        population=[{'id':str(i),'direction':'LIVE_OVER','signature':str(i),'_trade_ref':{'sha256':digest(i)},'_trades':[[]]} for i in range(2)]
        with tempfile.TemporaryDirectory() as td:
            result=all_pool_diagnostics(td,population,[],{'one':{'eligible':True}},DEFAULT,lambda **kw:None,lambda:False)
            # Zero-overlap pairs are counted exactly (also per direction pair) instead of listed row by row.
            self.assertEqual((result['pairs_evaluated'],result['pairs_with_shared_matches'],result['zero_overlap_pairs']),(1,0,1))
            self.assertEqual(result['zero_overlap_by_direction'],{'LIVE_OVER|LIVE_OVER':1});self.assertTrue(result['all_pool_pairwise_done'])
            with (Path(td)/'全池重叠与共同亏损.csv').open(encoding='utf-8-sig',newline='') as f:self.assertEqual(list(csv.DictReader(f)),[])
