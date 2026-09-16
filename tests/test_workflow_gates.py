import tempfile,unittest
from pathlib import Path
import numpy as np
from lab.common import DEFAULT,read_json
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.standard_atoms import AtomCatalog,W,C,T,X,P
from lab.global_bound import prove_global_bound,raw_conditions
from lab.standard_spec import scalar_atoms,required_difference_templates
from lab.workflow_gates import completion_state
from lab.server import json_ui


class WorkflowGates(unittest.TestCase):
    def test_every_gate_is_required_for_standard_completion(self):
        coverage={'standard_search_complete':True,'remaining':0,'local_search_complete':True,'local_profile':'standard'}
        summary={'standard_review':{'complete':True},'selection_status':'FINITE_LOCAL_SEARCH_COMPLETE'}
        trigger={'status':'PASS','rules':1,'rule_cases':{'status':'PASS'},'execution_economics':{'status':'PASS'},'streaming_paper_execution':{'status':'PASS'},'persistent_paper_execution':{str(i):{'status':'PASS'} for i in range(4)}}
        self.assertEqual(completion_state(coverage,summary,trigger,{'status':'PASS'},True)['state'],'STANDARD_HANDOFF_COMPLETE')
        self.assertEqual(completion_state(coverage,summary,trigger,{'status':'PASS'},False)['state'],'PARTIAL_RESULT')
        for changed in ({**coverage,'remaining':1},{**coverage,'standard_search_complete':False}):self.assertEqual(completion_state(changed,summary,trigger,{'status':'PASS'},True)['state'],'PARTIAL_RESULT')
        summary['standard_review']['complete']=False
        self.assertEqual(completion_state(coverage,summary,trigger,{'status':'PASS'},True)['state'],'PARTIAL_RESULT')
    def test_large_counts_survive_ui_transport_exactly(self):
        value=10**27+1;self.assertEqual(json_ui({'count':value,'small':3,'flag':True}),{'count':str(value),'small':3,'flag':True})
    def test_high_precision_equalities_and_cent_interval_starts(self):
        atoms=list(scalar_atoms('water',951,971,1000,'W','water'))
        self.assertIn({'feature':'water','op':'eq','value':951},atoms)
        self.assertTrue(all(a['value']%10==0 for a in atoms if a['op']=='range'))
        self.assertIn({'feature':'line_init','op':'ge','value':3},list(required_difference_templates('line_init',100,'line')))
    def test_global_proof_uses_later_quotes_and_preserves_every_definition(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            with AtomCatalog(root/'atoms.sqlite3',True) as atoms:
                for i,group in enumerate((W|C,W|C,W,T,X,P)):atoms.add({'feature':'water','op':'ge','value':90+i},group,100)
                atoms.commit();base={'mid':np.array([0,0,1,1]),'pnl':np.array([-200,190,-200,190])}
                state=prove_global_bound(root,base,atoms,DEFAULT,100,'fixture',{})
                self.assertEqual(state['global_upper_i'],380)
                plan=read_json(root/'global_bound_plan.json')
                self.assertEqual(sum(len(list(raw_conditions(b))) for b in plan['blocks']),state['raw_total'])
                base['pnl']=np.array([-200,2001,-200,190])
                self.assertIsNone(prove_global_bound(root,base,atoms,DEFAULT,100,'fixture',{}))


if __name__=='__main__':unittest.main()
