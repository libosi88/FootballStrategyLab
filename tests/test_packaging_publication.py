"""Small real-ZIP publication tests; the expensive subprocess checks are stubbed."""
from pathlib import Path
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
import os,tempfile,unittest,zipfile,json
from lab import packaging
from lab.common import atomic_json,read_json,sha
from lab.workflow_journal import StageJournal


class PackagingPublication(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='fsl_publication_test_')
        self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name);self.root=base/'job';self.root.mkdir()
        source=base/'source';source.mkdir()
        for name in ('lab','tests','docs','demo'):
            (source/name).mkdir();(source/name/'fixture.txt').write_text('fixture',encoding='utf-8')
        for name in ('app.py','requirements.txt'):(source/name).write_text('',encoding='utf-8')
        self.coverage={'standard_search_complete':True,'remaining':0,'local_search_complete':True,'local_profile':'standard'}
        self.summary={'standard_review':{'complete':True},'selection_status':'FINITE_LOCAL_SEARCH_COMPLETE'}
        self.trigger={'status':'PASS','rules':1,'rule_cases':{'status':'PASS'},
            'execution_economics':{'status':'PASS'},'streaming_paper_execution':{'status':'PASS'},'persistent_paper_execution':{str(i):{'status':'PASS'} for i in range(4)}}
        self.engine={'status':'PASS'}
        (self.root/'results').mkdir()
        atomic_json(self.root/'results/trigger_verification.json',self.trigger)
        atomic_json(self.root/'config.json',{'engine_hash':'fixture-engine'})
        original=base/'input.csv';original.write_text('fixture',encoding='utf-8')
        atomic_json(self.root/'input_manifest.json',{'files':[{'path':str(original),'sha256':sha(original)}]})
        atomic_json(self.root/'workflow_status.json',{'state':'PARTIAL_RESULT'})
        atomic_json(self.root/'summary.json',{'state':'PARTIAL_RESULT'})
        self.journal=StageJournal(self.root,'fixture-binding')
        for i in range(7):self.journal.finish(f'S{i}')
        self.journal.start('S7')
        self.before={name:(self.root/name).read_bytes() for name in ('workflow_status.json','workflow_stages.json','summary.json')}
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(packaging,'ROOT',source))
        self.stack.enter_context(patch.object(packaging,'source_fingerprint',return_value='fixture-engine'))
        self.stack.enter_context(patch.object(packaging.subprocess,'run',side_effect=self.fake_check))
        # The staged patch uses the shared Windows replacement helper once applied.
        # This deterministic primitive also lets the test inspect the publish boundary.
        self.stack.enter_context(patch.object(packaging,'atomic_replace',os.replace,create=True))

    def fake_check(self,command,**kwargs):
        kwargs['stdout'].write('isolated publication fixture: subprocess success\n')
        return SimpleNamespace(returncode=0)

    def run_package(self):
        return packaging.package_run(self.root,self.summary,self.coverage,self.trigger,self.engine,self.journal,lambda **kwargs:None)

    def assert_unpublished(self):
        for name,value in self.before.items():self.assertEqual((self.root/name).read_bytes(),value,name)
        self.assertEqual(self.journal.data['stages']['S7']['status'],'RUNNING')
        self.assertEqual(list((self.root/'packages').iterdir()),[])
        self.assertFalse((self.root/'packaging_verification.json').exists())
        self.assertEqual(list(self.root.glob('handoff_bundle_*')),[])

    def test_B_write_failure_never_publishes_completion(self):
        original=zipfile.ZipFile
        def fail_B(file,mode='r',*args,**kwargs):
            if Path(file).name.startswith('B_') and mode=='w':raise OSError('injected audit write failure')
            return original(file,mode,*args,**kwargs)
        with patch.object(packaging.zipfile,'ZipFile',side_effect=fail_B):
            with self.assertRaisesRegex(OSError,'audit write failure'):self.run_package()
        self.assert_unpublished()

    def test_B_final_integrity_failure_never_publishes_completion(self):
        original=zipfile.ZipFile
        def corrupt_B(file,mode='r',*args,**kwargs):
            result=original(file,mode,*args,**kwargs)
            if Path(file).name.startswith('B_') and mode=='r':result.testzip=lambda:'injected_bad_member'
            return result
        with patch.object(packaging.zipfile,'ZipFile',side_effect=corrupt_B):
            with self.assertRaisesRegex(ValueError,'CRC'):self.run_package()
        self.assert_unpublished()

    def test_both_archives_and_certificate_are_published_before_root_completion(self):
        publications=[]
        def replace(source,target):
            source,target=Path(source),Path(target)
            if source.is_dir():
                for name,value in self.before.items():self.assertEqual((self.root/name).read_bytes(),value,name)
                self.assertEqual(target.parent,self.root/'packages')
                self.assertTrue(target.name.startswith('verified_'))
                self.assertEqual(len(list(source.glob('*.zip'))),2)
                self.assertTrue((source/'交接验收证书.json').is_file())
                for archive in source.glob('*.zip'):
                    with zipfile.ZipFile(archive) as z:self.assertIsNone(z.testzip())
                publications.append(target)
            return os.replace(source,target)
        with patch.object(packaging,'atomic_replace',side_effect=replace):packages,state=self.run_package()
        self.assertEqual(len(publications),1)
        self.assertEqual(state['state'],'STANDARD_HANDOFF_COMPLETE')
        self.assertEqual(read_json(self.root/'workflow_status.json'),state)
        self.assertEqual(read_json(self.root/'workflow_stages.json')['stages']['S7']['status'],'PASS')
        self.assertEqual(read_json(self.root/'summary.json')['packages'],packages)
        for kind in ('developer','audit'):
            path=self.root/'packages'/packages[kind]
            self.assertEqual(path.parent,publications[0]);self.assertEqual(sha(path),packages[kind+'_sha256'])
        with zipfile.ZipFile(self.root/'packages'/packages['audit']) as z:
            self.assertEqual(json.loads(z.read('workflow_status.json')),state)
            self.assertEqual(json.loads(z.read('workflow_stages.json')),self.journal.data)
            self.assertNotIn('audit_sha256',json.loads(z.read('summary.json'))['packages'])

    def test_empty_roster_only_certifies_empty_result_chain(self):
        trigger={**self.trigger,'status':'EMPTY_ROSTER','rules':0}
        result=packaging.completion_state(self.coverage,self.summary,trigger,self.engine,True)
        self.assertEqual(result['state'],'PARTIAL_RESULT')
        self.assertFalse(result['gates']['nonempty_roster'])
        self.assertTrue(result['empty_roster_chain_verified'])

    def test_portable_validation_test_dependencies_are_whitelisted_in_both_archives(self):
        source=packaging.ROOT;tools=source/'validation';tools.mkdir()
        (tools/'finalize_review_status.py').write_text('test dependency',encoding='utf-8')
        (tools/'private_research.log').write_text('not distributable',encoding='utf-8')
        packages,_=self.run_package()
        with zipfile.ZipFile(self.root/'packages'/packages['developer']) as z:
            self.assertIn('validation/finalize_review_status.py',z.namelist());self.assertNotIn('validation/private_research.log',z.namelist())
        with zipfile.ZipFile(self.root/'packages'/packages['audit']) as z:
            self.assertIn('software/validation/finalize_review_status.py',z.namelist());self.assertNotIn('software/validation/private_research.log',z.namelist())

    def test_audit_omits_worker_and_stale_logs_but_keeps_referenced_checks(self):
        for name in ('worker.log','worker_old.log','handoff_test_99.log'):(self.root/name).write_text('old noise')
        result,_=self.run_package()
        with zipfile.ZipFile(self.root/'packages'/result['audit']) as z:
            names=z.namelist()
            for name in ('worker.log','worker_old.log','handoff_test_99.log'):self.assertNotIn(name,names)
            for name in ('handoff_test_0.log','handoff_test_1.log'):self.assertIn(name,names)
            policy=json.loads(z.read('audit_log_policy.json'));self.assertEqual(len(policy['omitted_runtime_or_obsolete_logs']),3)

    def test_both_packages_include_default_template_required_by_tests(self):
        atomic_json(packaging.ROOT/'config/default_config.json',{'fixture':'default config'})
        result,_=self.run_package()
        with zipfile.ZipFile(self.root/'packages'/result['developer']) as z:
            self.assertEqual(json.loads(z.read('config/default_config.json')),{'fixture':'default config'})
        with zipfile.ZipFile(self.root/'packages'/result['audit']) as z:
            self.assertEqual(json.loads(z.read('software/config/default_config.json')),{'fixture':'default config'})


if __name__=='__main__':unittest.main()
