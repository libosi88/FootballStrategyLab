import tempfile,unittest
from pathlib import Path
from lab.common import DEFAULT,read_json
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.standard_mining import mine_standard,ordered_standard_events
from lab.standard_atoms import AtomCatalog,feature_kind
from test_core import event

def fixture():
    rows=[];labels={}
    for mid in range(2):
        sid=f'standard-{mid}';labels[sid]={'eligible':True,'final':[2,1],'year':2024}
        for market in (0,1):
            for phase in (0,1):
                for step in range(4):
                    i=len(rows);e=event(i,line=(8 if market==0 else 2)+step//2,w0=90+(step%2)*5,ts=phase*10+step)
                    e.update(sid=sid,mid=mid,market=market,phase=phase,status='滚' if phase else '早',minute=step if phase else -2)
                    rows.append(e)
    return ordered_standard_events(rows),labels

class StandardIntegration(unittest.TestCase):
    def test_full_standard_dictionary_and_search_with_exact_upper_proof(self):
        rows,labels=fixture();cfg={**DEFAULT,'profile':'standard','cross_stale_minutes':5,'search_grammar':'v3_full'}
        with tempfile.TemporaryDirectory() as td:
            state=mine_standard(td,rows,labels,100,'LIVE_GIVE',cfg,lambda **kw:None,lambda:False)
            self.assertEqual(state['status'],'COMPLETE');self.assertEqual(state['remaining'],0);self.assertEqual(state['candidates'],0)
            self.assertGreater(state['proven_nonprofitable'],0);self.assertTrue(state['standard_scope_complete'])
            root=Path(td)/'mining/LIVE_GIVE';spec=read_json(root/'dictionary.json')
            self.assertGreater(spec['groups']['T'],70000);self.assertGreater(spec['groups']['M06'],0);self.assertGreater(spec['groups']['M07'],0)
            self.assertGreater(state['raw_total'],10**6)
            again=mine_standard(td,rows,labels,100,'LIVE_GIVE',cfg,lambda **kw:None,lambda:False)
            self.assertEqual(again['covered'],state['covered'])
            with AtomCatalog(root/'atoms.sqlite3') as atoms:
                features={a['feature'] for a in atoms}
            self.assertIn('window_1_3_water_init',features)
            # Identically zero features stay auditable fields and never become search atoms.
            self.assertFalse(features&{'line_same','line_return_keep','line_return_reset'})
            path=root/'atoms.sqlite3';path.write_bytes(path.read_bytes()+b'tamper')
            with self.assertRaisesRegex(ValueError,'完整性'):mine_standard(td,rows,labels,100,'LIVE_GIVE',cfg,lambda **kw:None,lambda:False)

    def test_default_compact_grammar_is_small_complete_and_at_most_two_conditions(self):
        rows,labels=fixture();cfg={**DEFAULT,'profile':'standard'}
        with tempfile.TemporaryDirectory() as td:
            state=mine_standard(td,rows,labels,100,'LIVE_GIVE',cfg,lambda **kw:None,lambda:False)
            self.assertEqual(state['status'],'COMPLETE');self.assertEqual(state['remaining'],0);self.assertTrue(state['standard_scope_complete'])
            root=Path(td)/'mining/LIVE_GIVE';spec=read_json(root/'search_spec.json');dictionary=read_json(root/'dictionary.json')
            self.assertEqual(spec['spec_version'],'FSL_STANDARD_COMPACT_V1');self.assertEqual(spec['max_conditions'],2)
            self.assertEqual(spec['modules']['M03'],0);self.assertEqual(sum(spec['modules'][m] for m in ('M04','M05','M06','M07')),0)
            self.assertLess(state['raw_total'],10**5);self.assertEqual(dictionary['groups']['W'],dictionary['groups']['C'])
            with AtomCatalog(root/'atoms.sqlite3') as atoms:
                listed=list(atoms)
            self.assertTrue(listed)
            # No exact equality on price levels; "unchanged" (=0) movement atoms are intended.
            self.assertFalse([a for a in listed if a['feature'] in ('water','otherwater') and a['op']=='eq'])
            self.assertTrue(all(a['op'] in ('le','ge','range') or a['value']==0 or feature_kind(a['feature']) in ('category','integer') for a in listed))

if __name__=='__main__':unittest.main()
