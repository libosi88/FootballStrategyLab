import copy,json,os,sqlite3,tempfile,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import numpy as np
from lab.common import DEFAULT,atomic_json,sha,digest
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.features import SignalEngine
from lab.paper_runner import StreamingPaperRunner
from lab.paper_execution import PaperDispatcher
from lab.locking import WorkspaceBusy
from lab.standard_cli import paper_stream,validate_stream_paths,export_candidates
from lab.search_integrity import seal_search_evidence,verify_search_evidence
from lab.global_bound import prove_global_bound
from lab.standard_atoms import AtomCatalog,W,C
from lab.workflow_gates import module_coverage
from lab.standard_search import candidate_families
from test_paper_runner import standard_packet
from test_core import event

class ProactiveAudit(unittest.TestCase):
    def test_candidate_export_never_overwrites_evidence_and_cleans_failed_partial(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);config=root/'config.json';atomic_json(config,{'directions':[]});atomic_json(root/'data_audit.json',{'water_scale':100});before=sha(config)
            with self.assertRaises(FileExistsError):export_candidates(root,config)
            self.assertEqual(sha(config),before)
            target=root/'new.jsonl.gz'
            with patch('lab.standard_cli.write_jsonl',side_effect=OSError('injected export failure')):
                with self.assertRaises(OSError):export_candidates(root,target)
            self.assertFalse(target.exists());self.assertFalse(list(root.glob('*.partial_*')))
            export_candidates(root,target)
            import gzip
            with gzip.open(target,'rt') as f:self.assertEqual(f.read(),'')

    def test_foreign_or_unbound_database_is_unchanged_on_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);foreign=root/'other.db'
            with closing(sqlite3.connect(foreign)) as conn:conn.execute('CREATE TABLE unrelated(value TEXT)');conn.commit()
            before=sha(foreign)
            with self.assertRaisesRegex(ValueError,'不是完整模拟账'):PaperDispatcher(foreign,standard_packet())
            self.assertEqual(sha(foreign),before)
            path=root/'ledger.db'
            with PaperDispatcher(path,standard_packet()) as d:d.save_runner_state('test',{'value':1})
            with closing(sqlite3.connect(path)) as conn:conn.execute("DELETE FROM meta WHERE key='binding'");conn.commit()
            before=sha(path)
            with self.assertRaisesRegex(ValueError,'身份绑定'):PaperDispatcher(path,standard_packet())
            self.assertEqual(sha(path),before)

    def test_output_collisions_cannot_truncate_input_rules_or_state(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);rules=root/'rules.json';quotes=root/'quotes.jsonl';state=root/'ledger.db'
            atomic_json(rules,standard_packet());quotes.write_text(json.dumps(event(0))+'\n');state.write_bytes(b'existing state')
            before={p:sha(p) for p in (rules,quotes,state)}
            for output in (quotes,rules,state):
                with self.assertRaisesRegex(ValueError,'重合'):paper_stream(rules,state,str(quotes),str(output),True)
            with self.assertRaisesRegex(ValueError,'重合'):validate_stream_paths(rules,quotes,str(quotes),None)
            alias=root/'linked.jsonl';os.link(quotes,alias)
            with self.assertRaisesRegex(ValueError,'重合'):validate_stream_paths(rules,root/'new.db',str(quotes),alias)
            self.assertEqual({p:sha(p) for p in before},before)

    def test_failed_save_poison_blocks_every_mutator_and_close_write(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'ledger.db';r=StreamingPaperRunner(path,standard_packet(),True);r.declare_new_match('a')
            before=r.dispatcher.load_runner_state('coordinator')
            try:
                # A new observation now commits its state and time fence together.
                # Inject the failure at that actual durable boundary, not a redundant save.
                with patch.object(r.dispatcher,'save_observation',side_effect=OSError('injected disk failure')):
                    with self.assertRaises(OSError):r.feed({**event(0,line=8),'market':0})
                self.assertTrue(r._faulted)
                for action in (lambda:r.feed(event(1)),lambda:r.backfill([]),lambda:r.declare_new_match('b'),lambda:r.finish_match('a'),lambda:r.flush(),lambda:r.save()):
                    with self.assertRaises(RuntimeError):action()
                with patch.object(r.dispatcher,'save_runner_state',side_effect=AssertionError('must not save')):r.close()
                r.close()
            finally:
                # Preserve the original assertion if it fails; never leak the SQLite handle.
                r.close()
            with StreamingPaperRunner(path,standard_packet(),True) as restored:
                self.assertEqual(restored.dispatcher.load_runner_state('coordinator'),before)

    def test_failed_batch_write_poison_cannot_be_hidden_by_close(self):
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'ledger.db',standard_packet(),True) as r:
            r.feed({**event(0,line=8,ts=1),'market':0})
            with patch.object(r.dispatcher,'submit_batch',side_effect=sqlite3.OperationalError('injected commit failure')):
                with self.assertRaises(sqlite3.OperationalError):r.flush()
            self.assertTrue(r._faulted)

    def test_only_one_coordinator_per_ledger_and_failed_init_releases_lock(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'ledger.db';p=standard_packet()
            with StreamingPaperRunner(path,p,True):
                with self.assertRaises(WorkspaceBusy):StreamingPaperRunner(path,p,True)
            with self.assertRaises(ValueError):StreamingPaperRunner(path,p,True,checkpoint_every=0)
            with StreamingPaperRunner(path,p,True):pass

    def test_hardlinked_ledger_cannot_bypass_coordinator_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path=root/'ledger.db'
            with StreamingPaperRunner(path,standard_packet(),True):
                alias=root/'alias.db';os.link(path,alias)
                with self.assertRaisesRegex(ValueError,'硬链接'):StreamingPaperRunner(alias,standard_packet(),True)

    def test_history_declaration_rejection_is_atomic(self):
        p=standard_packet();engine=SignalEngine(p['rules'],contract=p['contract'],execution_policy=p['execution_policy'])
        engine.feed({**event(0,line=8),'market':0});before=engine.snapshot()
        with self.assertRaises(ValueError):engine.mark_history_complete('a')
        self.assertEqual(engine.snapshot(),before)

    def test_invalid_and_backwards_watermarks_do_not_change_state(self):
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'ledger.db',standard_packet()) as r:
            r.declare_new_match('a');r.flush(10,'a');before=copy.deepcopy(r.watermarks)
            for value in (True,10.5,'11',9):
                with self.assertRaises(ValueError):r.flush(value,'a')
                self.assertEqual(r.watermarks,before)

    def test_policy_numbers_are_not_silently_truncated_or_coerced(self):
        with tempfile.TemporaryDirectory() as td:
            for key,value in (('match_cap',True),('match_cap',1.5),('match_cap','4'),('stale_minutes',-1),('stale_minutes',1.5)):
                p=standard_packet();p['execution_policy'][key]=value;p['roster_hash']=digest({k:p[k] for k in ('rules','contract','execution_policy')})
                with self.assertRaises(ValueError):PaperDispatcher(Path(td)/'ledger.db',p)

    def test_global_completion_requires_unchanged_actual_proof(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/'mining/LIVE_GIVE';folder.mkdir(parents=True)
            atomic_json(root/'config.json',DEFAULT);atomic_json(folder/'search_spec.json',{'modules':{'M00':1,'M01':1},'binding':'test'})
            coverage={'units':{'water':100},'directions':{'LIVE_GIVE':{'status':'COMPLETE','search_backend':'standard_global_bound'}},'standard_search_complete':True}
            self.assertFalse(module_coverage(root,coverage)['evidence_complete'])
            with AtomCatalog(folder/'atoms.sqlite3',True) as atoms:
                atoms.add({'feature':'water','op':'ge','value':90},W|C,100);atoms.commit()
                prove_global_bound(folder,{'mid':np.array([0,1]),'pnl':np.array([200,-200])},atoms,DEFAULT,100,'test',{})
            self.assertTrue(module_coverage(root,coverage)['declared_standard_search_complete'])
            proof=folder/'global_upper_proof.json';original=proof.read_bytes();proof.unlink()
            self.assertFalse(module_coverage(root,coverage)['evidence_complete'])
            proof.write_bytes(original+b' ')
            self.assertFalse(module_coverage(root,coverage)['declared_standard_search_complete'])

    def test_complete_ledger_tampering_is_rejected_before_candidate_read(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);atomic_json(root/'state.json',{'status':'COMPLETE','binding':'test'});atomic_json(root/'standard_plan.json',{'blocks':[]})
            with closing(sqlite3.connect(root/'search.sqlite3')) as conn:
                conn.execute('CREATE TABLE ledger(net INTEGER)');conn.execute('INSERT INTO ledger VALUES(1)');conn.commit()
            seal_search_evidence(root,'standard_class_dfs','test');verify_search_evidence(root,'standard_class_dfs','test')
            with closing(sqlite3.connect(root/'search.sqlite3')) as conn:conn.execute('UPDATE ledger SET net=9999');conn.commit()
            with self.assertRaisesRegex(ValueError,'证据缺失或内容改变'):list(candidate_families(root,2000))

    def test_missing_partial_ledger_is_not_created_by_export(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);atomic_json(root/'standard_plan.json',{'blocks':[]})
            with self.assertRaises(sqlite3.OperationalError):list(candidate_families(root,2000))
            self.assertFalse((root/'search.sqlite3').exists())
