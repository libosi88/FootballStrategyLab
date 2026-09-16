import tempfile,unittest,sqlite3,json,itertools
from pathlib import Path
import numpy as np
from lab.common import DEFAULT,read_json
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.rules import mask
from lab.mining import first_indices,Paused
from lab.standard_atoms import AtomCatalog,W,C,T,X,P
from lab.standard_masks import MaskStore
from lab.standard_search import extension_count,family_conditions,run_class_search,build_blocks

class Columns(dict):
    def atom_mask(self,a):return mask(a,dict(self))

class StandardSearch(unittest.TestCase):
    def test_corrupted_mask_membership_is_not_reused(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);_,atoms,config,masks=self.fixture(root)
            try:
                masks.conn.execute("UPDATE members SET checksum='corrupt' WHERE atom=0");masks.conn.commit()
                with self.assertRaisesRegex(ValueError,'映射校验'):masks.groups([0])
            finally:masks.close();atoms.close()
    def fixture(self,root):
        columns=Columns(eid=np.arange(48),mid=np.repeat(np.arange(24),2),side=np.zeros(48,np.int64),pnl=np.tile([-200,190],24),water=np.tile([90,100],24),minute=np.tile([0,1],24),abs_diff=np.zeros(48,np.int64),path2_keep=np.tile([11,44],24),cross_line=np.tile([0,5],24),pre_water=np.full(48,100,np.int64))
        atoms=AtomCatalog(root/'atoms.sqlite3',True)
        for a in [{'feature':'water','op':'ge','value':v} for v in (90,100,110)]+[{'feature':'minute','op':'eq','value':v} for v in (0,1)]+[{'feature':'abs_diff','op':op,'value':0} for op in ('le','ge')]:atoms.add(a,W|C,100)
        for a,g in [({'feature':'water','op':'ge','value':95},W),({'feature':'path2_keep','op':'eq','value':44},T),({'feature':'cross_line','op':'ge','value':5},X),({'feature':'pre_water','op':'ge','value':90},P)]:atoms.add(a,g,100)
        atoms.commit();config={**DEFAULT,'_water_scale':100,'standard_node_budget':0};masks=MaskStore(root,columns,atoms,config,'fixture');masks.prepare(lambda **kw:None,lambda:False)
        return columns,atoms,config,masks
    def test_extension_counts_and_recoverable_mapping(self):
        sizes=[3,2,4];block={'k':3,'sizes':sizes,'pool':[{'members':list(range(0,3))},{'members':list(range(3,5))},{'members':list(range(5,9))}],'anchors':[None]}
        full=list(family_conditions(block,None,[]));self.assertEqual(set(full),set(itertools.combinations(range(9),3)))
        for prefix in ([],[0],[1],[2],[0,0],[0,1],[1,2],[2,2],[0,1,2]):
            rows=list(family_conditions(block,None,prefix));self.assertEqual(len(rows),extension_count(prefix,3,sizes))
            self.assertEqual(len(rows),len(set(rows)))
    def test_entire_search_matches_unpruned_reference(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);columns,atoms,config,masks=self.fixture(root)
            try:
                result=run_class_search(root,atoms,masks,config,lambda **kw:None,lambda:False,'fixture')
                self.assertEqual(result['status'],'COMPLETE');self.assertEqual(result['remaining'],0)
                plan=read_json(root/'standard_plan.json');conn=sqlite3.connect(root/'search.sqlite3');seen=set();profitable=0;covered=0
                for block,anchor,prefix,kind,weight,n,net,upper in conn.execute('SELECT block,anchor,prefix,kind,weight,n,net,upper FROM ledger'):
                    rows=list(family_conditions(plan['blocks'][block],anchor,json.loads(prefix)));self.assertEqual(len(rows),int(weight));covered+=len(rows)
                    for ids in rows:
                        key=(block,ids);self.assertNotIn(key,seen);seen.add(key)
                        ix=first_indices(ids,columns,atoms);expected=(len(ix),int(columns['pnl'][ix].sum()))
                        if kind=='EVALUATED':self.assertEqual((n,net),expected)
                        elif kind=='PROVEN_ZERO':self.assertEqual(expected,(0,0))
                        else:self.assertLessEqual(expected[1],upper);self.assertLessEqual(upper,2000)
                        profitable+=expected[1]>2000
                conn.close();self.assertEqual(covered,plan['total']);self.assertEqual(profitable,result['candidates']);self.assertGreater(profitable,0)
                # The losing first parent must not suppress the profitable later child.
                self.assertLess(first_indices([0],columns,atoms)[0],first_indices([1],columns,atoms)[0])
            finally:masks.close();atoms.close()
    def test_pause_and_resume_preserve_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);columns,atoms,config,masks=self.fixture(root);calls=[0]
            def pause():calls[0]+=1;return calls[0]>=5
            try:
                with self.assertRaises(Paused):run_class_search(root,atoms,masks,config,lambda **kw:None,pause,'fixture')
                resumed=run_class_search(root,atoms,masks,config,lambda **kw:None,lambda:False,'fixture')
                conn=sqlite3.connect(root/'search.sqlite3');count=sum(int(r[0]) for r in conn.execute('SELECT weight FROM ledger'));conn.close()
                self.assertEqual(count,resumed['raw_total']);self.assertEqual(resumed['remaining'],0)
            finally:masks.close();atoms.close()
    def test_budget_is_not_silently_extended(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);_,atoms,config,masks=self.fixture(root);config['standard_node_budget']=4
            try:
                a=run_class_search(root,atoms,masks,config,lambda **kw:None,lambda:False,'fixture')
                b=run_class_search(root,atoms,masks,config,lambda **kw:None,lambda:False,'fixture')
                self.assertEqual(a['status'],'BUDGET_STOP');self.assertEqual(a['covered'],b['covered']);self.assertGreater(b['remaining'],0)
            finally:masks.close();atoms.close()

if __name__=='__main__':unittest.main()
