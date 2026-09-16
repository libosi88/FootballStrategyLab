import copy,tempfile,unittest,json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
import numpy as np
from lab import common,packaging
from lab.common import DEFAULT,settlement
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.store import Store
from lab.selection import neighbors,metrics,row_report,second_quote_indices
from lab.standard_review import diagnostic_variants,basic_reasons

class RecheckSeven(unittest.TestCase):
    def test_catalog_batches_empty_windows_and_resumes_exact_cursor(self):
        from lab import standard_atoms as module
        from lab.mining import Paused
        class Columns(dict):
            def __init__(self):super().__init__({f'custom_{i:04d}':None for i in range(257)});self.calls=[]
            def domain(self,name):self.calls.append(name);return None
        original=module.AtomCatalog.commit;commits=[]
        def commit(atoms):commits.append(1);return original(atoms)
        with tempfile.TemporaryDirectory() as td,ExitStack() as stack:
            for name in ('time_windows','required_difference_templates','required_templates','path_atoms','minute_atoms'):
                stack.enter_context(patch.object(module,name,return_value=()))
            stack.enter_context(patch.object(module,'EXTRA_FIELDS',set()))
            stack.enter_context(patch.object(module.AtomCatalog,'commit',new=commit))
            # The resumable per-feature cursor belongs to the full v3 grammar build.
            columns=Columns();root=Path(td)/'resume';v3={**DEFAULT,'search_grammar':'v3_full'}
            with self.assertRaises(Paused):module.build_standard_catalog(root,columns,'LIVE_GIVE',100,v3,should_pause=lambda:len(columns.calls)>=70)
            with module.AtomCatalog(root/'atoms.sqlite3') as db:
                saved=json.loads(db.conn.execute("SELECT body FROM meta WHERE key='build_cursor_v1'").fetchone()[0]);self.assertEqual(saved['features'],70)
            remaining=Columns();atoms,info=module.build_standard_catalog(root,remaining,'LIVE_GIVE',100,v3);atoms.close()
            self.assertEqual(len(remaining.calls),187);self.assertFalse(set(remaining.calls)&set(columns.calls))
            self.assertLess(len(commits),15)
            fresh=Columns();atoms,reference=module.build_standard_catalog(Path(td)/'fresh',fresh,'LIVE_GIVE',100,v3);atoms.close()
            self.assertEqual(info['domains'],reference['domains']);self.assertEqual(info['atom_count'],reference['atom_count'])

    def test_frontend_change_keeps_resume_but_engine_change_blocks_resume_and_packaging(self):
        with tempfile.TemporaryDirectory() as td:
            source=Path(td)/'source';(source/'lab').mkdir(parents=True);(source/'web').mkdir()
            core=source/'lab/core.py';core.write_text('v1');web=source/'web/app.js';web.write_text('v1')
            with patch.object(common,'ROOT',source):
                store=Store(Path(td)/'jobs');jid=store.create('test',DEFAULT,{'files':[]},True)
                original=common.source_fingerprint();web.write_text('v2')
                self.assertEqual(original,common.source_fingerprint())
                store.control(jid,'resume');self.assertEqual(store.get(jid)['status'],'QUEUED')
                store.control(jid,'pause');core.write_text('v2')
                self.assertNotEqual(original,common.source_fingerprint())
                with self.assertRaisesRegex(ValueError,'代码'):store.control(jid,'resume')
                self.assertEqual(store.get(jid)['status'],'PAUSED')
                with self.assertRaisesRegex(ValueError,'混合版本'):packaging.package_run(store.root/jid,None,None,None,None,None,lambda **kw:None)

    def test_path_and_linepath_semantics_are_in_shared_neighbors(self):
        a={'feature':'path2_keep','op':'eq','value':12,'pulse':True};original=copy.deepcopy(a)
        variants=neighbors(a,100)
        self.assertTrue(any(not v.get('pulse') for v in variants))
        self.assertTrue(any(v['feature']=='path2_reset' for v in variants));self.assertEqual(a,original)
        line={'feature':'linepath2_keep','op':'eq','value':12}
        self.assertEqual(neighbors(line,100)[0]['feature'],'linepath2_reset')
        self.assertTrue(any(mode=='joint' for mode,_,_ in diagnostic_variants([a,line],100)))

    def test_quote_loss_uses_second_qualifying_quote_not_best_quote(self):
        np.testing.assert_array_equal(second_quote_indices(np.array([False,True,True,True,True,True]),np.array([0,0,0,1,1,2])),[2,4])

    def test_legacy_selector_reaches_quote_loss_diagnostic(self):
        from lab.mining import build_direction
        from lab.selection import select,Quotes
        from lab.common import atomic_json
        from lab.rules import label
        from test_core import event
        events=[];labels={}
        for mid in range(90):
            sid=str(mid);year=2024+mid//30
            labels[sid]={'eligible':True,'final':[3,0],'year':year,'kickoff':mid*10,'date':f'{year}-01-01'}
            for step in range(3):
                events.append({**event(len(events),line=8,w0=105,ts=mid*10+step),'sid':sid,'mid':mid,'market':0})
        atom={'feature':'status','op':'eq','value':2};atom['label']=label(atom,100)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/'mining/LIVE_OVER';folder.mkdir(parents=True)
            cfg={**DEFAULT,'profile':'smoke','directions':['LIVE_OVER']};arr=build_direction(events,labels,100,'LIVE_OVER',cfg)
            atomic_json(folder/'dictionary.json',{'atoms':[atom]});atomic_json(folder/'state.json',{'parts':[{'file':'part.npz'}]})
            np.savez(folder/'arrays.npz',**arr);np.savez(folder/'part.npz',conditions=np.array([[0]]),metrics=np.array([[90,18900]]))
            with patch('lab.selection.finish_selection') as finish:
                select(root,events,labels,100,cfg,lambda **kw:None,lambda:False)
                records=finish.call_args.args[-2]
                self.assertEqual(records[0]['类型'],'first_quote_missing');self.assertEqual(records[0]['状态'],'PASS')

    def trades(self,pnls,water=105):
        return [{'sid':str(i),'sort_time':i,'ts':i,'eid':i,'pnl':p,'water':water,'year':2024,'quality':0} for i,p in enumerate(pnls)]

    def test_injected_mixed_pnl_is_visible_and_cannot_qualify(self):
        trades=self.trades([5]);m=metrics(trades,100,[2024])
        rule={'id':'x','direction':'LIVE_GIVE','conditions':[],'metrics':m,'status':'观察'}
        self.assertEqual(row_report(rule)['未分类结算'],1)
        self.assertEqual(row_report(rule)['结算分类状态'],'UNCLASSIFIED_PRESENT')
        self.assertIn('SETTLEMENT_CLASSIFICATION_FAILED',basic_reasons(trades,m,[],DEFAULT,100)[1])
        for line in range(-40,41):
            for margin in range(-10,11):
                for side in (0,1):self.assertIn(settlement(line,105,margin,side),{210,105,0,-100,-200})

    def test_three_winners_expose_actual_removal_without_relaxing_requirement(self):
        m=metrics(self.trades([210,210,210,-200]),100,[2024])
        self.assertEqual(m['remove_top5'],-1);self.assertEqual(m['remove_top5_removed_count'],3)
        self.assertEqual(m['remove_top5_sample_status'],'FEWER_THAN_FIVE_WINNERS')
