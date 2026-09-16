"""Synthetic restart probe: no real jobs, data or production source changes."""
import hashlib,json,sys,tempfile
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from lab.common import digest,source_fingerprint
from lab.contracts import bind_rule
from lab.paper_runner import StreamingPaperRunner
from lab.release import release_files,release_fingerprint
from test_core import event
from test_paper_execution import packet
def manifest():
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in release_files(ROOT)}
def case(directory,every,fault):
    p=packet(['LIVE_GIVE'])
    p['rules']=[bind_rule({'id':'r0','direction':'LIVE_GIVE','priority':0,'conditions':[{'feature':'water_prev_keep','op':'le','value':-10}]},p['contract'])]
    p['execution_policy'].update(feature_version='v3',cross_stale_minutes=5,warmup='complete_archive_history_required_for_history_based_features')
    p['roster_hash']=digest({k:p[k] for k in ('rules','contract','execution_policy')})
    quotes=[event(i,ts=i+1,w0=w) for i,w in enumerate((95,110,100,100))]
    result={'checkpoint_every':every,'fail_checkpoint':fault};path=directory/'paper.sqlite3'
    with StreamingPaperRunner(path,p,checkpoint_every=every) as r:
        r.declare_new_match('a');r.feed(quotes[0]);r.save(True)
        result['signals_before_reopen']=[r.feed(q)['signals'] for q in quotes[1:3]]
        result['pending_before_reopen']=list(r.pending)
        if fault:
            with patch.object(r.dispatcher,'save_runner_state',side_effect=OSError('AUDIT_ONLY_CHECKPOINT_WRITE_FAILURE')):
                try:r.save(True)
                except OSError as error:result['injected_error']=str(error)
        result['durable_watermarks']=r.dispatcher.observed_clocks()
    with StreamingPaperRunner(path,p,checkpoint_every=every) as r:
        result['restored_watermarks']=dict(r.watermarks);result['restored_pending']=list(r.pending);result['replayed_inputs']=[]
        for q in quotes[1:]:
            try:result['replayed_inputs'].append({'ts':q['ts'],'result':r.feed(q)})
            except ValueError as error:
                if r._faulted:raise
                result['replayed_inputs'].append({'ts':q['ts'],'rejected':str(error)})
        r.flush(before=5,sid='a')
        result['intents']=[r.dispatcher.intent(row[0]) for row in r.dispatcher.conn.execute('SELECT id FROM intents')]
        result['intent_count']=len(result['intents'])
    return result
def main():
    target=OUT/'checkpoint_gap_results.json'
    if target.exists():raise FileExistsError('Do not overwrite diagnostic evidence')
    before=manifest();report={'scope':'TARGETED_POST_REPAIR_FAULT_INJECTION_NOT_FULL_ACCEPTANCE','engine_hash':source_fingerprint(),'release_hash':release_fingerprint(),'real_jobs_started':False,'production_source_modified':False,'cases':{}}
    with tempfile.TemporaryDirectory(prefix='synthetic_',dir=OUT) as tmp:
        for name,every,fault in [('batched_checkpoint_failure',20,True),('batched_graceful_control',20,False),('per_quote_failure_control',1,True)]:report['cases'][name]=case(Path(tmp)/name,every,fault)
    report['defect_reproduced']=(report['cases']['batched_checkpoint_failure']['intent_count']==0 and report['cases']['batched_graceful_control']['intent_count']==1 and report['cases']['per_quote_failure_control']['intent_count']==1)
    report['source_unchanged']=before==manifest()
    with target.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps(report,ensure_ascii=True,indent=2))
    return 0 if report['source_unchanged'] else 2
if __name__=='__main__':raise SystemExit(main())
