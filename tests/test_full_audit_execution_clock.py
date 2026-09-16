"""Paper execution must never precede its causal first signal."""
import tempfile
import unittest
from pathlib import Path
from lab.paper_execution import PaperDispatcher
from test_core import event
from test_paper_execution import packet,offer


class ExecutionClock(unittest.TestCase):
    def test_execution_before_signal_is_rejected_without_reserving(self):
        p=packet(['LIVE_OVER']);q={**event(0,line=8,ts=9),'market':0}
        o=offer(p,0,q);o['signal']['ts']=10;o['execution_ts']=9
        with tempfile.TemporaryDirectory() as td,PaperDispatcher(Path(td)/'paper.db',p) as d:
            with self.assertRaisesRegex(ValueError,'信号|时间'):d.submit_batch([o])
            self.assertEqual(d.usage(q['sid']),0)
            self.assertEqual(d.conn.execute('SELECT COUNT(*) FROM intents').fetchone()[0],0)

    def test_explicit_execution_time_cannot_hide_invalid_signal_clock(self):
        for value in (None,True,'10',10.0):
            with self.subTest(value=value),tempfile.TemporaryDirectory() as td:
                p=packet(['LIVE_OVER']);q={**event(0,line=8,ts=9),'market':0}
                o=offer(p,0,q);o['signal']['ts']=value;o['execution_ts']=10
                with PaperDispatcher(Path(td)/'paper.db',p) as d:
                    with self.assertRaises(ValueError):d.submit_batch([o])
                    self.assertEqual(d.usage(q['sid']),0)

    def test_equal_and_later_execution_times_keep_valid_quote_behavior(self):
        for when in (10,11):
            with self.subTest(when=when),tempfile.TemporaryDirectory() as td:
                p=packet(['LIVE_OVER']);q={**event(0,line=8,ts=9),'market':0}
                o=offer(p,0,q);o['signal']['ts']=10;o['execution_ts']=when
                with PaperDispatcher(Path(td)/'paper.db',p) as d:
                    self.assertEqual(d.submit_batch([o])[0]['status'],'RESERVED')
                    self.assertEqual(d.usage(q['sid']),1)

    def test_one_backdated_offer_rejects_entire_uncommitted_batch(self):
        p=packet(['LIVE_OVER','LIVE_GIVE'])
        q={**event(0,line=8,ts=10),'market':0};good=offer(p,0,q)
        other=event(1,line=2,ts=9);bad=offer(p,1,other)
        bad['signal']['ts']=11;bad['execution_ts']=10
        with tempfile.TemporaryDirectory() as td,PaperDispatcher(Path(td)/'paper.db',p) as d:
            with self.assertRaises(ValueError):d.submit_batch([good,bad])
            self.assertEqual(d.usage(q['sid']),0)
            self.assertEqual(d.conn.execute('SELECT COUNT(*) FROM intents').fetchone()[0],0)


if __name__=='__main__':unittest.main()
