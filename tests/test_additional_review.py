import copy,json,tempfile,unittest,sqlite3
from pathlib import Path
from unittest.mock import patch
from lab.common import DEFAULT,score,atomic_json
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import DEFAULT
from lab.features import FeatureStream
from lab.standard_features import StandardFeatureStream
from lab.standard_spec import required_templates
from lab.standard_review import TradeArchive
from lab.standard_masks import MaskStore
from lab.workflow_gates import module_coverage
from lab.paper_runner import StreamingPaperRunner
from test_core import event,TEST_CONTRACT
from test_paper_runner import standard_packet

class AdditionalReview(unittest.TestCase):
    def test_duplicate_identity_rejected_before_feature_mutation(self):
        for cls in (FeatureStream,StandardFeatureStream):
            stream=cls(contract=TEST_CONTRACT);e=event(0,ts=1);stream.feed(e)
            before=copy.deepcopy(stream.state)
            for bad in ({**e,'ts':2},{**e,'water':[100,90]}):
                with self.assertRaises(ValueError):stream.feed(bad)
                self.assertEqual(stream.state,before)
            self.assertIsNone(stream.feed(e));self.assertEqual(stream.state,before)

    def test_score_cache_does_not_alias_mutable_events(self):
        a=score('1-2');a[0]=99;self.assertEqual(score('1-2'),[1,2])

    def test_ah_high_line_templates_registered(self):
        templates=list(required_templates(100,True))
        self.assertIn({'feature':'absline','op':'range','value':12,'upper':14},templates)
        self.assertIn({'feature':'absline','op':'ge','value':14},templates)

    def test_missing_ledger_is_readonly_incomplete_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/'mining/LIVE_GIVE';folder.mkdir(parents=True)
            atomic_json(root/'config.json',DEFAULT)
            atomic_json(folder/'search_spec.json',{'modules':{'M00':1}})
            atomic_json(folder/'standard_plan.json',{'blocks':[{'module':'M00'}]})
            cov={'units':{'water':100},'directions':{'LIVE_GIVE':{'status':'PARTIAL'}},'standard_search_complete':True}
            for empty_db in (False,True):
                if empty_db:sqlite3.connect(folder/'search.sqlite3').close()
                result=module_coverage(root,cov)
                self.assertFalse(result['evidence_complete']);self.assertFalse(result['declared_standard_search_complete'])
                self.assertEqual(result['modules']['M00']['remaining'],1)
                self.assertEqual((folder/'search.sqlite3').exists(),empty_db)

    def test_unknown_mask_member_cannot_silently_disappear(self):
        store=object.__new__(MaskStore);store._group_pause=lambda:False
        store._validated_members=lambda:{0:'known'};store._groups_cache={}
        with self.assertRaises(ValueError):store.groups([0,99])

    def test_trade_cache_checks_reference_even_on_hit(self):
        with tempfile.TemporaryDirectory() as td:
            archive=TradeArchive(Path(td)/'trades.db',max_cache_bytes=100)
            try:
                ref=archive.put('a',{'major':[]});archive.get(ref)
                with self.assertRaises(ValueError):archive.get({**ref,'sha256':'wrong'})
                self.assertLessEqual(archive.cache_bytes,100)
            finally:archive.close()

    def test_waiting_duplicate_does_not_consume_capacity(self):
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'ledger.db',standard_packet(),max_waiting_quotes=2) as r:
            a=event(0);b=event(1);r.feed(a);r.feed(b);r.feed(a)
            self.assertEqual(len(r.waiting['a']),2)

    def test_storage_failure_restores_memory_and_prevents_close_save(self):
        with tempfile.TemporaryDirectory() as td:
            r=StreamingPaperRunner(Path(td)/'ledger.db',standard_packet())
            before=r.engine.snapshot()
            def failure(events):
                r.watermarks['a']=99;r.last_sid='a';raise sqlite3.OperationalError('injected')
            with patch.object(r,'_backfill',side_effect=failure):
                with self.assertRaises(sqlite3.OperationalError):r.backfill([event(0)])
            self.assertEqual(r.engine.snapshot(),before);self.assertEqual(r.watermarks,{});self.assertIsNone(r.last_sid)
            with self.assertRaises(RuntimeError):r.feed(event(1))
            with patch.object(r.dispatcher,'save_runner_state',side_effect=AssertionError('unsafe save')):r.close()

    def test_default_config_template_is_synchronized(self):
        from lab.common import ROOT
        self.assertEqual(json.loads((ROOT/'config/default_config.json').read_text(encoding='utf-8')),DEFAULT)
