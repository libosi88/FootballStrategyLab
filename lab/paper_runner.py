"""Restartable standard-JSON -> signals -> paper intent coordinator.

History warmup consumes missed historical first signals without backdating an
execution. Archive replay is explicit. This component cannot send real orders.
"""
from .features import SignalEngine
from .paper_execution import PaperDispatcher
from .contracts import validate_event
from .common import canonical,digest
from copy import deepcopy
import json
from pathlib import Path
from .locking import WorkspaceLock,WorkspaceBusy

class StreamingPaperRunner:
    def __init__(self,path,packet,archive_history=False,auto_fill=False,max_waiting_quotes=10000,checkpoint_every=1,execution_delay=0,water_reduction=0):
        path=Path(path).resolve()
        if path.exists() and path.stat().st_nlink>1:raise ValueError('模拟账不能使用多硬链接文件，无法保证唯一协调器')
        self._runner_lock=WorkspaceLock(path.parent,path.name+'.coordinator.lock');self._closed=False
        try:self._runner_lock.__enter__()
        except WorkspaceBusy as error:raise WorkspaceBusy('同一模拟账已有协调器，不能并发覆盖触发状态') from error
        try:self._initialize(path,packet,archive_history,auto_fill,max_waiting_quotes,checkpoint_every,execution_delay,water_reduction)
        except BaseException:
            try:
                if hasattr(self,'dispatcher'):self.dispatcher.close()
            finally:self._runner_lock.__exit__(None,None,None)
            raise
    def _initialize(self,path,packet,archive_history,auto_fill,max_waiting_quotes,checkpoint_every,execution_delay,water_reduction):
        packet=deepcopy(packet)
        for name,value,minimum in (('execution_delay',execution_delay,0),('water_reduction',water_reduction,0),('checkpoint_every',checkpoint_every,1),('max_waiting_quotes',max_waiting_quotes,1)):
            if type(value) is not int or value<minimum:raise ValueError(name+'必须为范围内整数')
        if type(archive_history) is not bool or type(auto_fill) is not bool:raise ValueError('模拟模式标记必须为布尔值')
        self.delay=int(execution_delay);self.reduction=int(water_reduction)
        if self.delay<0 or self.reduction<0 or not archive_history and (self.delay or self.reduction):raise ValueError('报价压力参数仅用于显式离线模拟')
        self.checkpoint_every=max(1,int(checkpoint_every));self._save_count=0;self._faulted=False
        self.dispatcher=PaperDispatcher(path,packet);self.packet=packet;self.archive_history=archive_history;self.auto_fill=auto_fill;self.max_waiting=max_waiting_quotes
        try:
            saved=self.dispatcher.load_runner_state('coordinator') or {}
            if saved and saved.get('archive_history')!=archive_history:raise ValueError('实时等待历史与完整档案回放模式不能混用')
            if saved and (saved.get('delay',0),saved.get('reduction',0))!=(self.delay,self.reduction):raise ValueError('不能更改已有模拟账的报价压力情景')
            self.engine=SignalEngine(packet['rules'],saved.get('engine'),contract=packet['contract'],execution_policy=packet['execution_policy'])
        except BaseException:self.dispatcher.close();raise
        self.pending=saved.get('pending',{});self.waiting=saved.get('waiting',{});self.last_sid=saved.get('last_sid');self.watermarks=saved.get('watermarks',{})
        # A ledger commit may be newer than the coordinator checkpoint after a crash.
        for sid,minute in self.dispatcher.observed_clocks().items():
            self.watermarks[sid]=max(minute,self.watermarks.get(sid,minute))
        if saved and self.pending and saved.get('quote_mapping') not in ('minute_close_latest_v2','minute_close_latest_v3'):
            self.dispatcher.close();raise ValueError('旧版待执行批次缺少最新报价证据，不能直接恢复；请使用新模拟账并回补历史')
    def save(self,force=False):
        self._require_active()
        self._save_count+=1
        if force or self._save_count%self.checkpoint_every==0:
            value=self._snapshot();h=digest(value)
            if not force and h==getattr(self,'_last_saved_hash',None):return
            self._persist(self.dispatcher.save_runner_state,'coordinator',value);self._last_saved_hash=h
    def _snapshot(self,watermarks=None):
        return {'engine':self.engine.snapshot(),'pending':self.pending,'waiting':self.waiting,
                'last_sid':self.last_sid,'archive_history':self.archive_history,
                'watermarks':self.watermarks if watermarks is None else watermarks,
                'quote_mapping':'minute_close_latest_v3','delay':self.delay,'reduction':self.reduction,
                'observation_commit':'feature_pending_and_fence_atomic_v1'}
    def _seal_observation(self,matches,before):
        updated=dict(self.watermarks)
        for match in matches:updated[match]=max(before,updated.get(match,before))
        if updated==self.watermarks:return
        value=self._snapshot(updated)
        self._persist(self.dispatcher.save_observation,value,{match:updated[match] for match in matches})
        self.watermarks=updated;self._last_saved_hash=digest(value)
    def _require_active(self):
        if self._closed or self._faulted:raise RuntimeError('协调器已关闭或持久化状态不确定，必须重新打开模拟账')
    def _persist(self,operation,*args,**kwargs):
        try:return operation(*args,**kwargs)
        except Exception:self._faulted=True;raise
    def close(self):
        if self._closed:return
        try:
            if not self._faulted:self.save(True)
        finally:
            self._closed=True
            try:self.dispatcher.close()
            finally:self._runner_lock.__exit__(None,None,None)
    def __enter__(self):return self
    def __exit__(self,*exc):self.close()
    def declare_new_match(self,sid):
        """Adapter assertion: subscribed from the true beginning, no missing history."""
        self._require_active()
        # Quotes already waiting prove the feed did not start at the beginning of the match.
        if self.waiting.get(sid):raise ValueError('该场已有等待历史的报价，不能声明从开场完整订阅；请先发送history回补历史')
        # Control acknowledgements must survive batched quote checkpoints.
        self.engine.mark_history_complete(sid);self.save(True)
    def backfill(self,events):
        self._require_active()
        events=list(events)
        # Validate the entire historical prefix before changing durable intents.
        probe=SignalEngine(self.packet['rules'],self.engine.snapshot(),contract=self.packet['contract'],execution_policy=self.packet['execution_policy'])
        for e in events:validate_event(e,self.packet['contract'])
        for sid in {e['sid'] for e in events}:probe.mark_history_complete(sid)
        for e in sorted(events,key=lambda e:(e['sid'],e['ts'],e['row'],e['market'],e['phase'])):probe.feed(e)
        seen={(e['sid'],e['event_key']):e for e in events};sids={e['sid'] for e in events}
        queued=[]
        for sid in sids:
            for e in self.waiting.get(sid,[]):
                key=(e['sid'],e['event_key'])
                if key in seen:
                    if canonical(seen[key])!=canonical(e):raise ValueError('回补与等待队列中同一事件ID内容不同')
                else:queued.append(e)
        for e in sorted(queued,key=lambda e:(e['sid'],e['ts'],e['row'],e['market'],e['phase'])):
            if e['ts']<self.watermarks.get(e['sid'],-1):raise ValueError('等待报价迟于已封存分钟')
            probe.feed(e)
        old_engine=self.engine.snapshot();old_pending=deepcopy(self.pending);old_waiting=deepcopy(self.waiting)
        old_watermarks=dict(self.watermarks);old_last_sid=self.last_sid
        try:return self._backfill(events)
        except Exception as error:
            self.engine=SignalEngine(self.packet['rules'],old_engine,contract=self.packet['contract'],execution_policy=self.packet['execution_policy'])
            self.pending=old_pending;self.waiting=old_waiting
            self.watermarks=old_watermarks;self.last_sid=old_last_sid
            # Earlier ledger transactions may already be durable. Never overwrite
            # their coordinator checkpoint using a rolled-back in-memory state.
            self._faulted=True
            try:self._persist(self.dispatcher.alarm,'backfill_rejected',{'error':str(error),'reopen_required':True})
            except Exception:pass
            raise
    def _backfill(self,events):
        if not events:return {'missed_signals':0,'replayed_queued_quotes':0}
        sids={e['sid'] for e in events}
        for e in events:validate_event(e,self.packet['contract'])
        # A re-sent history of a match that was already complete cannot have missed any submission.
        gaps={sid for sid in sids if not all(canonical([sid,m,p]) in self.engine.history_ready for m in (0,1) for p in (0,1))}
        for sid in sids:self.engine.mark_history_complete(sid)
        # Pending but unsubmitted historical offers cannot become fictional fills.
        for key in list(self.pending):
            if self.pending[key] and self.pending[key][0]['quote']['sid'] in gaps:
                self._persist(self.dispatcher.reject_unsubmitted,self.pending[key],'历史回补期间错过首信号提交；不倒填订单')
                self._persist(self.dispatcher.alarm,'missed_submission_during_history_gap',{'batch':key,'signals':len(self.pending[key])});del self.pending[key]
        seen={(e['sid'],e['event_key']) for e in events};missed=0;horizon={}
        for e in sorted(events,key=lambda e:(e['sid'],e['ts'],e['row'],e['market'],e['phase'])):
            missed+=len(self.engine.feed(e));horizon[e['sid']]=max(horizon.get(e['sid'],-1),e['ts'])
        queued=[]
        for sid in sids:
            self.waiting[sid]=[e for e in self.waiting.get(sid,[]) if (e['sid'],e['event_key']) not in seen];queued.extend(self.waiting[sid])
        self.save()
        # Quotes buffered while history was missing update state first and are submitted once at the latest observed
        # minute, so a first signal older than the stale limit is rejected instead of filled at a superseded price.
        for e in sorted(queued,key=lambda e:(e['sid'],e['ts'],e['row'],e['market'],e['phase'])):
            self.waiting[e['sid']].remove(e);self.feed(e,_flush=False);horizon[e['sid']]=max(horizon[e['sid']],e['ts'])
        # The watermark also seals the backfilled minutes: a later quote for them is rejected as late, not an engine fault.
        for sid in sorted(horizon):self.flush(before=max(horizon[sid],self.watermarks.get(sid,-1)),sid=sid)
        return {'missed_signals':missed,'replayed_queued_quotes':len(queued)}
    def feed(self,e,_flush=True):
        self._require_active()
        validate_event(e,self.packet['contract'])
        if e['sid'] in self.engine.closed_matches:raise ValueError('已封存比赛不能重新接收报价或等待历史')
        if self.engine.stream.preflight(e):return {'status':'DUPLICATE_IGNORED','signals':[],'intents':[]}
        if e['ts']<self.watermarks.get(e['sid'],-1):raise ValueError('报价迟于已封存分钟，不能回写已执行批次')
        # A quote older than the match's processed wall clock is late input to reject, not a reason to fault the coordinator.
        clock=getattr(self.engine.stream,'extra',{}).get('clocks',{}).get(e['sid'])
        if clock is not None and e['ts']<clock:raise ValueError('报价早于本场已处理的时间，按迟到消息拒绝')
        if self.archive_history:
            if self.last_sid is not None and self.last_sid!=e['sid']:self.finish_match(self.last_sid)
            self.engine.mark_history_complete(e['sid']);self.last_sid=e['sid']
        if not self.engine.is_history_ready(e):
            pending=self.waiting.setdefault(e['sid'],[])
            for queued in pending:
                if queued['event_key']==e['event_key']:
                    if queued!=e:raise ValueError('等待队列中同一事件ID内容改变')
                    return {'status':'WAITING_HISTORY','signals':[],'intents':[]}
            if len(pending)>=self.max_waiting:
                self._persist(self.dispatcher.alarm,'history_buffer_full',{'sid':e['sid'],'quotes':len(pending)})
                raise ValueError('历史未准备，等待队列达到上限；必须回补后继续')
            pending.append(deepcopy(e))
            self.save();return {'status':'WAITING_HISTORY','signals':[],'intents':[]}
        try:signals=self.engine.feed(e)
        except Exception:self._faulted=True;raise
        # Even a non-triggering quote (including a closed quote) supersedes the
        # original offer. Never keep an earlier favourable state of this market.
        for offers in self.pending.values():
            for offer in offers:
                q=offer['quote']
                if (q['sid'],q['market'],q['phase'])==(e['sid'],e['market'],e['phase']) and (q['ts']<=e['ts']<=offer['signal']['ts']+self.delay if self.archive_history else offer['signal']['ts']<=e['ts']):offer['quote']=deepcopy(e)
        for signal in signals:
            key=canonical([e['sid'],e['ts']+self.delay]);self.pending.setdefault(key,[]).append({'strategy_id':signal['strategy_id'],'signal':deepcopy(signal),'quote':deepcopy(e)})
        # One coordinator checkpoint per quote: the durable batch submit is idempotent, so a
        # crash between ledger commit and this save only replays already-recorded batches.
        intents=self.flush(before=e['ts'],sid=e['sid'],_save=False) if _flush else []
        self.save();return {'status':'PAPER_ONLY','signals':signals,'intents':intents}
    def flush(self,before=None,sid=None,_save=True):
        self._require_active()
        if before is not None and type(before) is not int:raise ValueError('完整分钟watermark必须为整数')
        if sid is not None and (not isinstance(sid,str) or not sid):raise ValueError('watermark比赛ID必须是非空字符串')
        if before is not None and any(before<t for key,t in self.watermarks.items() if sid is None or key==sid):raise ValueError('完整分钟watermark不能倒退')
        if not self.archive_history and before is None and self.pending:raise ValueError('实时提交必须提供完整分钟 watermark，不能猜测执行时刻')
        matches=([sid] if sid is not None else
                 set(self.watermarks)|set(self.waiting)|{json.loads(scope)[0] for scope in self.engine.history_ready}|
                 {v[0]['quote']['sid'] for v in self.pending.values() if v})
        if before is not None and not self.archive_history:
            # Live orders in this flush execute at `before`. Seal that observation
            # first, including empty batches. Failure afterwards cannot rewind it.
            self._seal_observation(matches,before)
        out=[]
        keys=[k for k,v in self.pending.items() if v and (sid is None or v[0]['quote']['sid']==sid) and (before is None or v[0]['signal']['ts']+self.delay<before)]
        keys.sort(key=lambda k:(self.pending[k][0]['signal']['ts'],self.pending[k][0]['quote']['sid']))
        execution_groups={}
        for key in keys:
            if not self.archive_history and before is not None and before-self.pending[key][0]['signal']['ts']>self.dispatcher.stale:
                out.extend(self._persist(self.dispatcher.reject_unsubmitted,self.pending[key],'首信号超过提交时限，未尝试执行',before))
                self._persist(self.dispatcher.alarm,'missed_stale_unsubmitted_batch',{'batch':key,'observed_watermark':before,'signals':len(self.pending[key])})
                del self.pending[key]
                if _save:self.save()
                continue
            offers=self.pending[key]
            when=offers[0]['signal']['ts']+self.delay if self.archive_history else before
            execution_groups.setdefault((offers[0]['quote']['sid'],when),[]).append(key)
        # Buffered signals can have different first-signal times but one actual
        # decision time. Submit them together for atomic priority/conflict/cap checks.
        # A pre-submit checkpoint retains every input needed to replay an archive batch.
        if self.archive_history and execution_groups:self.save(True)
        for (_,when),batch_keys in execution_groups.items():
            offers=[offer for key in batch_keys for offer in self.pending[key]]
            offers=[{**o,'execution_ts':when,'water':o['quote']['water'][o['signal']['side']]-self.reduction} for o in offers]
            result=self._persist(self.dispatcher.submit_batch,offers)
            if self.auto_fill:
                for item in result:
                    if item['status']=='RESERVED':self._persist(self.dispatcher.receipt,item['intent_id'],'paper_fill_'+item['intent_id'],'FILLED','1')
                result=[self.dispatcher._current_result(item) for item in result]
            for key in batch_keys:del self.pending[key]
            out.extend(result)
            if _save:self.save()
        if before is not None:
            # Archive replay intentionally prices earlier signals. Seal its boundary
            # only after those batches, not before their legitimate historical fills.
            if self.archive_history:
                self._seal_observation(matches,before)
            if _save:self.save()
        return out
    def finish_match(self,sid):
        self._require_active()
        if not isinstance(sid,str) or not sid:raise ValueError('比赛ID必须是非空字符串')
        if self.archive_history:result=self.flush(sid=sid)
        else:
            result=[]
            for key in list(self.pending):
                if self.pending[key] and self.pending[key][0]['quote']['sid']==sid:
                    result.extend(self._persist(self.dispatcher.reject_unsubmitted,self.pending[key],'比赛结束时尚未提交；不倒填订单'))
                    self._persist(self.dispatcher.alarm,'unsubmitted_batch_at_match_end',{'sid':sid,'batch':key,'signals':len(self.pending[key])})
                    del self.pending[key]
        waiting=self.waiting.pop(sid,[])
        if waiting:self._persist(self.dispatcher.alarm,'history_missing_at_match_end',{'sid':sid,'unprocessed_quotes':len(waiting),'event_keys':[e['event_key'] for e in waiting]})
        self.engine.close_match(sid);self.save(True);return result
