import copy,unittest
from lab.features import SignalEngine
from lab.verify import verify_golden_evidence
from test_core import event,TEST_CONTRACT

class GoldenBaselineEvidence(unittest.TestCase):
    def signals(self,conditions):
        engine=SignalEngine([{'id':'baseline','direction':'LIVE_OVER','conditions':conditions}],contract=TEST_CONTRACT)
        return engine.feed({**event(0,line=8),'market':0})

    def test_zero_condition_legacy_rule_has_valid_empty_evidence(self):
        signals=self.signals([])
        self.assertEqual(len(signals),1);self.assertEqual(signals[0]['evidence'],{})
        verify_golden_evidence(copy.deepcopy(signals),signals)

    def test_absent_evidence_is_not_an_empty_condition_list(self):
        signals=self.signals([])
        for value in (None,[],False):
            with self.subTest(value=value),self.assertRaisesRegex(AssertionError,'缺少'):
                verify_golden_evidence([{**signals[0],'evidence':value}],signals)
        with self.assertRaisesRegex(AssertionError,'缺少'):
            verify_golden_evidence([{k:v for k,v in signals[0].items() if k!='evidence'}],signals)

    def test_nonempty_condition_evidence_cannot_be_erased_or_changed(self):
        signals=self.signals([{'feature':'water','op':'ge','value':90}])
        for evidence in ({},{'water':999}):
            with self.subTest(evidence=evidence),self.assertRaisesRegex(AssertionError,'不一致'):
                verify_golden_evidence([{**signals[0],'evidence':evidence}],signals)

if __name__=='__main__':unittest.main()
