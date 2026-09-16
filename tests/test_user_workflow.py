"""User-visible regressions: price expiry, directory changes, durable job creation."""
import tempfile,time,sys,unittest
from pathlib import Path
from unittest.mock import patch
from lab.common import DEFAULT,MISSING,validate_quote_fields
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.catalog import quality_files
from lab.store import Store
from lab.paper_runner import StreamingPaperRunner
from lab.paper_verification import verify_stream_orders
from lab.features import SignalEngine
from lab.packaging import checked_process,disposable_directory
from lab.mining import Paused
from lab.workflow_gates import completion_state
from test_core import event
from test_paper_runner import standard_packet

class UserWorkflow(unittest.TestCase):
    def quotes(self):
        a={**event(0,line=8,ts=1),'market':0}
        b={**event(1,line=8,ts=1),'market':0,'closed':True,'valid':False,'line':MISSING,'water':[MISSING,MISSING]}
        c={**event(2,line=8,ts=2),'market':0}
        return a,b,c

    def test_later_closure_survives_restart_and_cannot_fill(self):
        a,b,c=self.quotes();p=standard_packet()
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'ledger.db'
            with StreamingPaperRunner(path,p,True,True) as r:r.feed(a);r.feed(b)
            with StreamingPaperRunner(path,p,True,True) as r:
                items=r.feed(c)['intents'];self.assertEqual(len(items),1)
                self.assertEqual(items[0]['status'],'REJECTED');self.assertIn('封盘',items[0]['reason'])
                self.assertEqual(r.dispatcher.usage('a'),0)

    def test_live_latest_price_and_actual_watermark_time(self):
        a,_,c=self.quotes();b={**event(1,line=8,ts=1,w0=80),'market':0};p=standard_packet()
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'ledger.db',p) as r:
            r.declare_new_match('a');r.feed(a);r.feed(b)
            with self.assertRaisesRegex(ValueError,'watermark'):r.flush()
            items=r.flush(before=2,sid='a');body=r.dispatcher.intent(items[0]['intent_id'])['body']
            self.assertEqual(body['water'],80);self.assertEqual(body['execution_ts'],2);self.assertEqual(body['quote']['eid'],1)
            before=r.engine.snapshot();watermarks=dict(r.watermarks)
            self.assertEqual(r.feed(a)['status'],'DUPLICATE_IGNORED')
            self.assertEqual(r.engine.snapshot(),before);self.assertEqual(r.watermarks,watermarks)
            self.assertEqual(r.dispatcher.intent(items[0]['intent_id'])['body'],body)
            with self.assertRaisesRegex(ValueError,'封存分钟'):r.feed({**a,'eid':3,'row':5,'event_key':'late:5'})
            with self.assertRaisesRegex(ValueError,'同一事件ID'):r.feed({**a,'water':[70,85]})

    def test_live_incoming_closed_quote_is_not_ignored_before_flush(self):
        a,b,_=self.quotes();b={**b,'ts':2};p=standard_packet()
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'ledger.db',p) as r:
            r.declare_new_match('a');r.feed(a)
            result=r.feed(b)['intents'];self.assertEqual(result[0]['status'],'REJECTED');self.assertEqual(r.dispatcher.usage('a'),0)

    def test_expired_signal_is_rejected_in_durable_ledger(self):
        import json
        a,_,_=self.quotes();p=standard_packet()
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'ledger.db'
            with StreamingPaperRunner(path,p) as r:
                r.declare_new_match('a');r.feed(a);result=r.flush(before=10,sid='a')
                self.assertEqual(result[0]['status'],'REJECTED');self.assertFalse(result[0]['execution_attempted']);self.assertEqual(r.dispatcher.usage('a'),0)
            with StreamingPaperRunner(path,p) as r:
                saved=[json.loads(row[0]) for row in r.dispatcher.conn.execute('SELECT body FROM signals')]
                self.assertEqual(len(saved),1);self.assertEqual(saved[0]['signal']['eid'],a['eid']);self.assertEqual(saved[0]['status'],'REJECTED')

    def test_rejected_backfill_restores_watermark_with_buffered_quotes(self):
        p=standard_packet();rows=[{**event(i,line=8,ts=i+1),'market':0} for i in range(3)]
        conflict={**rows[2],'water':[80,85]}
        with tempfile.TemporaryDirectory() as td,StreamingPaperRunner(Path(td)/'ledger.db',p) as r:
            r.feed(rows[1]);r.feed(rows[2]);before=dict(r.watermarks)
            with self.assertRaises(ValueError):r.feed(conflict)
            self.assertEqual(r.watermarks,before);self.assertFalse(r.engine.is_history_ready(rows[1]));self.assertEqual(len(r.waiting['a']),2)

    def test_actual_coordinator_checked_against_minute_close_oracle(self):
        events=list(self.quotes());p=standard_packet();e=SignalEngine(p['rules'],contract=p['contract'],execution_policy=p['execution_policy']);e.mark_history_complete('a')
        signals=[]
        for q in events:signals.extend(e.feed(q))
        result=verify_stream_orders(p,signals,events)
        self.assertEqual(result['status'],'PASS');self.assertEqual(result['orders'],0);self.assertEqual(result['durable_restarts'],1)
        # Reproduce the old wiring bug by dropping the closure only in the runner.
        original=StreamingPaperRunner.feed
        def buggy(r,q):return {'signals':[]} if q['closed'] else original(r,q)
        with patch.object(StreamingPaperRunner,'feed',buggy):
            self.assertEqual(verify_stream_orders(p,signals,events)['status'],'FAIL')
        for scenario in (1,2,3):
            report=verify_stream_orders(p,signals,events,scenario=scenario)
            self.assertEqual(report['status'],'PASS');self.assertEqual(report['orders'],int(scenario>=2))

    def test_quality_lookup_is_independent_of_country_tree_depth(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'按国家分类数据';q=root/'_build/handoff_current/exclude_sids.csv';q.parent.mkdir(parents=True);q.write_text('sId,reason,scope,source\n')
            deep=root/'中国/中超/2024/皇冠';deep.mkdir(parents=True);csv=deep/'quotes.csv';csv.touch()
            self.assertEqual(quality_files([str(deep)]),[q]);self.assertEqual(quality_files([str(csv)]),[q]);self.assertEqual(quality_files([str(root/'中国/中超')]),[q])

    def test_invalid_closed_and_calendar_date_fail_closed(self):
        for closed in ('true','1','未知'):
            with self.assertRaises(ValueError):validate_quote_fields('2024-01-01',closed)
        for date in ('2024-02-30','2024-99-99','2024-1-1',''):
            with self.assertRaises(ValueError):validate_quote_fields(date,'')
        self.assertFalse(validate_quote_fields('2024-02-29',''));self.assertTrue(validate_quote_fields('2024-02-29','是'))

    def test_job_file_failure_does_not_leave_queue_entry(self):
        with tempfile.TemporaryDirectory() as td:
            store=Store(td)
            with patch('lab.store.atomic_json',side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):store.create('test',DEFAULT,{'files':[]})
            self.assertEqual(store.jobs(),[])

    def test_idempotency_and_duplicate_active_submission(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(td);args=('test',DEFAULT,{'files':[]})
            first=s.create(*args,request_id='a')
            self.assertEqual(s.create(*args,request_id='a'),first)
            self.assertEqual(s.create(*args,request_id='b'),first)
            self.assertEqual(len(s.jobs()),1)
            with self.assertRaises(ValueError):s.create('other',DEFAULT,{'files':[]},request_id='a')

    def test_resume_rejects_different_code_before_enqueue(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(td);jid=s.create('test',DEFAULT,{'files':[]},True)
            with patch('lab.store.source_fingerprint',return_value='changed'):
                with self.assertRaises(ValueError):s.control(jid,'resume')
            self.assertEqual(s.get(jid)['status'],'PAUSED')

    def test_recycled_pid_does_not_hold_queue_forever(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            s=Store(td);jid=s.create('test',DEFAULT,{'files':[]})
            with patch('lab.store.process_birth',return_value='original'):self.assertTrue(s.claim(jid,os.getpid()))
            with patch('lab.store.process_birth',return_value='replacement'):s.recover()
            self.assertEqual(s.get(jid)['status'],'INTERRUPTED')

    def test_dictionary_resume_skips_committed_features_without_rule_loss(self):
        from contextlib import ExitStack
        from lab.standard_atoms import build_standard_catalog
        class Columns(dict):
            calls=[]
            def domain(self,name):self.calls.append(name);return (0,1)
        columns=Columns({'line':[0,1],'water':[90,95]});stop=[False]
        with tempfile.TemporaryDirectory() as td,ExitStack() as stack:
            stack.enter_context(patch('lab.standard_atoms.EXTRA_FIELDS',set()))
            for name in ('required_difference_templates','required_templates','path_atoms'):
                stack.enter_context(patch('lab.standard_atoms.'+name,return_value=[]))
            stack.enter_context(patch('lab.standard_atoms.scalar_atoms',side_effect=lambda name,*args:[{'feature':name,'op':'ge','value':0}]))
            def progress(**kw):stop[0]=kw.get('dictionary_features')==1
            v3={**DEFAULT,'search_grammar':'v3_full'}
            with self.assertRaises(Paused):build_standard_catalog(Path(td)/'resume',columns,'PRE_GIVE',100,v3,progress,lambda:stop[0])
            columns.calls=[]
            atoms,_=build_standard_catalog(Path(td)/'resume',columns,'PRE_GIVE',100,v3)
            resumed=list(atoms);atoms.close();self.assertNotIn('line',columns.calls)
            atoms,_=build_standard_catalog(Path(td)/'whole',columns,'PRE_GIVE',100,v3)
            self.assertEqual(resumed,list(atoms));atoms.close()

    def test_packaging_child_is_stopped_on_pause(self):
        started=time.monotonic()
        with self.assertRaises(Paused):checked_process([sys.executable,'-c','import time;time.sleep(60)'],lambda:time.monotonic()-started>.2)
        self.assertLess(time.monotonic()-started,5)

    def test_packaging_without_pause_keeps_validation_timeout(self):
        with patch('lab.packaging.subprocess.run') as run:
            run.return_value=type('Completed',(),{'returncode':0})()
            checked_process(['validation-child'])
            self.assertEqual(run.call_args.kwargs['timeout'],1800)

    def test_packaging_pause_survives_a_transient_windows_cleanup_lock(self):
        import shutil
        with tempfile.TemporaryDirectory() as td:
            parent=Path(td);calls=[];real_rmtree=shutil.rmtree
            def delayed_cleanup(path,*args,**kwargs):
                calls.append(Path(path))
                if len(calls)==1:raise PermissionError('simulated transient handle lock')
                return real_rmtree(path,*args,**kwargs)
            with patch('lab.packaging.shutil.rmtree',side_effect=delayed_cleanup),patch('lab.packaging.time.sleep'):
                with self.assertRaises(Paused):
                    with disposable_directory('fsl_handoff_check_',parent) as fresh:
                        self.assertTrue(fresh.is_dir())
                        raise Paused()
            self.assertEqual(len(calls),2)
            self.assertFalse(calls[-1].exists())

    def test_packaging_normal_exit_survives_a_transient_windows_cleanup_lock(self):
        import shutil
        with tempfile.TemporaryDirectory() as td:
            parent=Path(td);calls=[];real_rmtree=shutil.rmtree
            def delayed_cleanup(path,*args,**kwargs):
                calls.append(Path(path))
                if len(calls)==1:raise PermissionError('simulated transient handle lock')
                return real_rmtree(path,*args,**kwargs)
            with patch('lab.packaging.shutil.rmtree',side_effect=delayed_cleanup),patch('lab.packaging.time.sleep'):
                with disposable_directory('fsl_handoff_check_',parent) as fresh:
                    self.assertTrue(fresh.is_dir())
            self.assertEqual(len(calls),2)
            self.assertFalse(calls[-1].exists())

    def test_direct_ledger_pass_cannot_certify_missing_stream_check(self):
        coverage={'standard_search_complete':True,'remaining':0,'local_profile':'standard','local_search_complete':True}
        summary={'standard_review':{'complete':True},'selection_status':'FINITE_LOCAL_SEARCH_COMPLETE'}
        trigger={'status':'PASS','rules':1,'rule_cases':{'status':'PASS'},'persistent_paper_execution':{str(i):{'status':'PASS'} for i in range(4)}}
        self.assertEqual(completion_state(coverage,summary,trigger,{'status':'PASS'},True)['state'],'PARTIAL_RESULT')

if __name__=='__main__':unittest.main()
