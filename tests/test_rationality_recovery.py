import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from lab.common import digest,source_fingerprint,execution_fingerprint
from lab.contracts import bind_rule
from lab.paper_runner import StreamingPaperRunner
from lab.paper_execution import PaperDispatcher
from lab import common
from test_core import event
from test_paper_execution import packet


class AtomicObservation(unittest.TestCase):
    def test_batched_failure_replays_every_feature_without_rewinding_time(self):
        for every in (1,20,128):
            with self.subTest(every=every),tempfile.TemporaryDirectory() as td:
                p=packet(['LIVE_GIVE'])
                p['rules']=[bind_rule({'id':'r0','direction':'LIVE_GIVE','priority':0,
                    'conditions':[{'feature':'water_prev_keep','op':'le','value':-10}]},p['contract'])]
                p['execution_policy'].update(feature_version='v3',cross_stale_minutes=5)
                p['roster_hash']=digest({k:p[k] for k in ('rules','contract','execution_policy')})
                quotes=[event(i,ts=i+1,w0=w) for i,w in enumerate((95,110,100,100))]
                path=Path(td)/'paper.db'
                with StreamingPaperRunner(path,p,checkpoint_every=every) as r:
                    r.declare_new_match('a');r.feed(quotes[0]);r.save(True)
                    r.feed(quotes[1]);self.assertEqual(len(r.feed(quotes[2])['signals']),1)
                    with patch.object(r.dispatcher,'save_runner_state',side_effect=OSError('isolated checkpoint failure')):
                        with self.assertRaises(OSError):r.save(True)
                with StreamingPaperRunner(path,p,checkpoint_every=every) as r:
                    self.assertEqual(r.watermarks['a'],3)
                    for q in quotes[1:]:r.feed(q)
                    r.flush(before=5,sid='a')
                    self.assertEqual(r.dispatcher.conn.execute('SELECT COUNT(*) FROM intents').fetchone()[0],1)
                    self.assertEqual(r.dispatcher.usage('a'),1)

    def test_observation_and_all_clocks_rollback_together(self):
        with tempfile.TemporaryDirectory() as td,PaperDispatcher(Path(td)/'paper.db',packet(['LIVE_GIVE'])) as d:
            d.save_runner_state('coordinator',{'value':'old'})
            original=d._advance_clock
            def failing(sid,minute):
                if sid=='b':raise OSError('isolated clock failure')
                original(sid,minute)
            with patch.object(d,'_advance_clock',side_effect=failing):
                with self.assertRaises(OSError):d.save_observation({'value':'new'},{'a':100,'b':200})
            self.assertEqual(d.observed_clocks(),{})
            self.assertEqual(d.load_runner_state('coordinator'),{'value':'old'})

    def test_execution_code_hash_refuses_unverified_packet(self):
        p=packet(['LIVE_GIVE']);p['execution_policy']['execution_code_hash']='wrong'
        p['roster_hash']=digest({k:p[k] for k in ('rules','contract','execution_policy')})
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):PaperDispatcher(Path(td)/'paper.db',p)

    def test_paper_only_edit_changes_runtime_but_not_research_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'lab').mkdir()
            (root/'lab/features.py').write_text('feature v1')
            (root/'lab/paper_runner.py').write_text('runner v1')
            with patch.object(common,'ROOT',root):
                before=source_fingerprint();runtime=execution_fingerprint()
                (root/'lab/paper_runner.py').write_text('runner v2')
                self.assertEqual(source_fingerprint(),before);self.assertNotEqual(execution_fingerprint(),runtime)
                (root/'lab/features.py').write_text('feature v2')
                self.assertNotEqual(source_fingerprint(),before)


if __name__=='__main__':unittest.main()
