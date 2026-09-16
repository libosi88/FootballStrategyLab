"""Transactional paper intents, reservations, receipts and second-signal shadow.

No account/network adapter exists here. A receipt is a simulation input, never
proof that a bookmaker accepted an order. Unknown and partial orders retain risk.
"""
from pathlib import Path
from copy import deepcopy
import sqlite3,json
from .common import canonical,digest,MISSING,side_for,scaled,execution_fingerprint
from .contracts import resolve_contract,validate_event,semantic_rule,LABEL_FIELDS
from .features import validate_rules

STAKE_SCALE=1_000_000

def core_slot(direction,line,side):
    prefix=direction.split('_',1)[0]
    if direction.endswith(('OVER','UNDER')):return direction
    if line==0:return prefix+('_PK_HOME' if side==0 else '_PK_AWAY')
    return prefix+('_GIVE' if side==(0 if line>0 else 1) else '_RECEIVE')

class PaperDispatcher:
    def __init__(self,path,packet):
        packet=deepcopy(packet)
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        validate_rules(packet['rules']);self.rules={r['id']:r for r in packet['rules']}
        self.contract=resolve_contract(list(self.rules.values()),packet.get('contract'));self.policy=dict(packet['execution_policy'])
        if self.policy.get('execution_code_hash') is not None and self.policy['execution_code_hash']!=execution_fingerprint():
            raise ValueError('模拟包执行代码身份不匹配，不能把旧验收用于新执行器')
        if self.policy.get('live_enabled',False) is not False or self.policy.get('second_slot_enabled',False) is not False:raise ValueError('本组件仅允许首单模拟，真实/第二笔执行关闭')
        cap=self.policy.get('match_cap',4);stale=self.policy.get('stale_minutes',5)
        if type(cap) is not int or not 1<=cap<=24 or type(stale) is not int or not 0<=stale<=1440:raise ValueError('模拟本金上限和陈旧度必须是范围内整数')
        self.cap=cap*STAKE_SCALE;self.stale=stale
        if not STAKE_SCALE<=self.cap<=24*STAKE_SCALE:raise ValueError('模拟整场本金上限无效')
        if self.policy.get('priority')!='frozen_rule_priority':raise ValueError('持久化模拟器要求冻结规则优先级')
        if packet.get('roster_hash')!=digest({'rules':packet['rules'],'contract':self.contract,'execution_policy':self.policy}):raise ValueError('模拟名单包完整性校验失败')
        if any(type(r.get('priority')) is not int or r['priority']<0 for r in self.rules.values()):raise ValueError('模拟器缺少冻结优先级')
        self.binding=digest({'contract':self.contract,'rules':sorted((semantic_rule(r) for r in self.rules.values()),key=lambda r:r['id']),'policy':self.policy,'schema':'FSL_PAPER_LEDGER_V1'})
        self.conn=sqlite3.connect(self.path,timeout=30);self.conn.row_factory=sqlite3.Row
        try:self._initialize_database()
        except BaseException:self.conn.close();raise
    def _initialize_database(self):
        tables={r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required={'meta','batches','signals','intents','receipts','shadows','clocks','runner_state','alarms'}
        if tables:
            if not required<=tables:raise ValueError('既有数据库不是完整模拟账，拒绝修改或补造缺失表')
            old=self.conn.execute("SELECT body FROM meta WHERE key='binding'").fetchone()
            if old is None or old[0]!=self.binding:raise ValueError('既有模拟账缺少匹配的身份绑定，拒绝重新绑定')
        # FULL WAL commits retain durable receipt/reservation transaction boundaries.
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,body TEXT);
        CREATE TABLE IF NOT EXISTS batches(key TEXT PRIMARY KEY,hash TEXT,body TEXT);
        CREATE TABLE IF NOT EXISTS signals(key TEXT PRIMARY KEY,hash TEXT,body TEXT);
        CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY,sid TEXT,slot TEXT,contract TEXT,rule TEXT,status TEXT,requested INTEGER,reserved INTEGER,filled INTEGER,body TEXT);
        CREATE INDEX IF NOT EXISTS intents_sid_slot ON intents(sid,slot);
        CREATE INDEX IF NOT EXISTS intents_sid_contract ON intents(sid,contract);
        CREATE TABLE IF NOT EXISTS receipts(key TEXT PRIMARY KEY,intent TEXT,hash TEXT,body TEXT);
        CREATE TABLE IF NOT EXISTS shadows(key TEXT PRIMARY KEY,body TEXT);
        CREATE TABLE IF NOT EXISTS clocks(sid TEXT PRIMARY KEY,minute INTEGER);
        CREATE TABLE IF NOT EXISTS runner_state(key TEXT PRIMARY KEY,body TEXT);
        CREATE TABLE IF NOT EXISTS alarms(id INTEGER PRIMARY KEY,kind TEXT,body TEXT);
        ''')
        old=self.conn.execute("SELECT body FROM meta WHERE key='binding'").fetchone()
        if old and old[0]!=self.binding:self.conn.close();raise ValueError('执行账与规则/契约/政策不匹配，不能复用名额')
        self.conn.execute("INSERT OR IGNORE INTO meta VALUES('binding',?)",(self.binding,));self.conn.commit()
        self._recover_observed_clocks()
    def _advance_clock(self,sid,minute):
        """Caller owns the transaction; observations can only advance, never rewind."""
        if not isinstance(sid,str) or not sid or type(minute) is not int:raise ValueError('持久化时间边界必须是比赛ID和整数时间')
        self.conn.execute('INSERT INTO clocks VALUES(?,?) ON CONFLICT(sid) DO UPDATE SET minute=excluded.minute WHERE excluded.minute>clocks.minute',(sid,minute))
    def _recover_observed_clocks(self):
        """One-time repair of old ledgers whose rejection outlived their checkpoint."""
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            if self.conn.execute("SELECT 1 FROM meta WHERE key='observed_clock_recovery_v2'").fetchone():return
            for row in self.conn.execute('SELECT body FROM signals'):
                body=json.loads(row['body']);observed=body.get('observed_ts')
                if type(observed) is int:self._advance_clock(body['sid'],observed)
            saved=self.load_runner_state('coordinator') or {}
            for sid,minute in saved.get('watermarks',{}).items():self._advance_clock(sid,minute)
            self.conn.execute("INSERT INTO meta VALUES('observed_clock_recovery_v2','complete')")
    def record_watermarks(self,watermarks):
        """Persist even empty batches before an alarm/checkpoint can fail."""
        values=dict(watermarks)
        if not values:return
        for sid,minute in values.items():
            if not isinstance(sid,str) or not sid or type(minute) is not int:raise ValueError('持久化时间边界必须是比赛ID和整数时间')
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            for sid,minute in values.items():self._advance_clock(sid,minute)
    def observed_clocks(self):
        return {row['sid']:row['minute'] for row in self.conn.execute('SELECT sid,minute FROM clocks')}
    def close(self):self.conn.close()
    def __enter__(self):return self
    def __exit__(self,*exc):self.close()
    def _intent(self,intent_id):
        row=self.conn.execute('SELECT * FROM intents WHERE id=?',(intent_id,)).fetchone()
        if row is None:raise ValueError('模拟订单不存在')
        return dict(row)
    def intent(self,intent_id):
        row=self._intent(intent_id);row['body']=json.loads(row['body']);return row
    def usage(self,sid):
        return self._usage_i(sid)/STAKE_SCALE
    def _usage_i(self,sid):
        row=self.conn.execute('SELECT COALESCE(SUM(reserved+filled),0) AS used FROM intents WHERE sid=?',(sid,)).fetchone()
        return row['used']
    def _slot_owner(self,sid,slot):
        return self.conn.execute('SELECT * FROM intents WHERE sid=? AND slot=? AND (reserved>0 OR filled>0) ORDER BY rowid LIMIT 1',(sid,slot)).fetchone()
    def _normalize(self,offer):
        if set(offer)-{'strategy_id','signal','quote','execution_ts','water'}:raise ValueError('模拟报价含未登记字段，拒绝终场/收益信息')
        rule=self.rules.get(offer.get('strategy_id'))
        if rule is None:raise ValueError('信号不属于冻结名单')
        q=offer['quote'];s=offer['signal'];validate_event(q,self.contract)
        if LABEL_FIELDS.intersection(s) or 'pnl' in s:raise ValueError('模拟信号不得携带未来标签/收益')
        if s.get('strategy_id')!=rule['id'] or s.get('direction')!=rule['direction'] or s.get('sid')!=q['sid']:raise ValueError('信号身份不一致')
        side=s.get('side');when=offer.get('execution_ts',s.get('ts'))
        if type(side) is not int or side not in (0,1) or type(when) is not int:raise ValueError('模拟实际侧/时间不合法')
        if type(s.get('ts')) is not int or when<s['ts']:raise ValueError('模拟执行时间不得早于有效的首信号时间')
        expected_phase=0 if rule['direction'].startswith('PRE') else 1;expected_market=0 if rule['direction'].endswith(('OVER','UNDER')) else 1
        if q['market']!=expected_market:raise ValueError('报价市场不属于该规则')
        water=offer.get('water',q['water'][side]);reason=''
        if not q['valid']:reason='最新报价封盘或无效'
        elif q['phase']!=expected_phase:reason='执行阶段改变'
        elif side_for(rule['direction'],q['line'])!=side or q['line']!=s.get('line'):reason='执行盘口或实际侧改变'
        elif q['market']==1 and (q['score']!=s.get('score') or q['phase']==1 and q['score'][0]==MISSING):reason='执行比分基准改变或缺失'
        elif when<q['ts'] or when-q['ts']>self.stale:reason='报价未来或已陈旧'
        elif type(water) is not int or not 0<water<=q['water'][side]:reason='执行水位无效或高于可观察报价'
        slot=core_slot(rule['direction'],s['line'],side)
        key=digest([q['sid'],rule['id']]);signal_hash=digest(s)
        contract=digest([q['sid'],q['market'],q['phase'],q['ts'],side,q['line'],water,q['score'] if q['market']==1 else None])
        return {'rule':rule,'signal':s,'signal_key':key,'signal_hash':signal_hash,'quote':q,'time':when,'side':side,'water':water,'slot':slot,'contract':contract,'reason':reason}
    def submit_batch(self,offers):
        if not offers:return []
        unique={}
        for offer in offers:
            o=self._normalize(offer);key=o['signal_key'];h=digest({'signal':o['signal_hash'],'quote':o['quote'],'time':o['time'],'water':o['water']})
            if key in unique and unique[key][0]!=h:raise ValueError('批次重复首信号携带不同执行报价')
            unique[key]=(h,o)
        normalized=[o for _,o in unique.values()]
        groups={(o['quote']['sid'],o['time']) for o in normalized}
        if len(groups)!=1:raise ValueError('一次提交必须是同场同完整决策分钟的批次')
        sid,minute=next(iter(groups));batch_key=digest([sid,minute]);batch_hash=digest(sorted((key,h) for key,(h,_) in unique.items()))
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            prior=self.conn.execute('SELECT hash,body FROM batches WHERE key=?',(batch_key,)).fetchone()
            if prior:
                if prior['hash']!=batch_hash:raise ValueError('已提交批次内容改变，不能追加或追溯重排')
                return [self._current_result(r) for r in json.loads(prior['body'])]
            clock=self.conn.execute('SELECT minute FROM clocks WHERE sid=?',(sid,)).fetchone()
            if clock and minute<clock['minute']:raise ValueError('迟到批次不能追溯生成模拟订单')
            fresh=[];results=[]
            for o in normalized:
                seen=self.conn.execute('SELECT hash,body FROM signals WHERE key=?',(o['signal_key'],)).fetchone()
                if seen:
                    if seen['hash']!=o['signal_hash']:raise ValueError('同策略本场首信号内容改变，不能重试成另一笔')
                    results.append(self._current_result(json.loads(seen['body'])))
                else:fresh.append(o)
            opposites={}
            for o in fresh:
                if not o['reason'] and self._slot_owner(sid,o['slot']) is None:opposites.setdefault(o['quote']['market'],set()).add(o['side'])
            for o in sorted(fresh,key=lambda o:(o['rule']['priority'],o['rule']['id'])):
                reason=o['reason'];owner=self._slot_owner(sid,o['slot'])
                duplicate=self.conn.execute('SELECT id FROM intents WHERE sid=? AND contract=? AND (reserved>0 OR filled>0)',(sid,o['contract'])).fetchone()
                if not reason:
                    if owner:reason='同核心方向首单已占用'
                    elif duplicate:reason='相同报价合约重复'
                    elif len(opposites.get(o['quote']['market'],set()))>1:reason='同决策分钟同市场相反信号'
                    elif self._usage_i(sid)+STAKE_SCALE>self.cap:reason='整场模拟本金达到上限'
                result={'strategy_id':o['rule']['id'],'sid':sid,'slot':o['slot'],'status':'REJECTED' if reason else 'RESERVED','reason':reason}
                if not reason:
                    intent_id=digest([self.binding,o['signal_key'],o['contract']]);result['intent_id']=intent_id
                    body={'strategy_id':o['rule']['id'],'direction':o['rule']['direction'],'signal':o['signal'],'quote':o['quote'],'side':o['side'],'water':o['water'],'execution_ts':minute,'simulation_only':True}
                    self.conn.execute('INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?,?)',(intent_id,sid,o['slot'],o['contract'],o['rule']['id'],'RESERVED',STAKE_SCALE,STAKE_SCALE,0,canonical(body)))
                elif owner and owner['filled']>0 and not duplicate:
                    shadow={'strategy_id':o['rule']['id'],'sid':sid,'first_intent':owner['id'],'signal':o['signal'],'quote':o['quote'],'reason':'首单已成交后的非重复信号，仅影子记录','second_slot_enabled':False}
                    self.conn.execute('INSERT OR IGNORE INTO shadows VALUES(?,?)',(o['signal_key'],canonical(shadow)))
                self.conn.execute('INSERT INTO signals VALUES(?,?,?)',(o['signal_key'],o['signal_hash'],canonical(result)));results.append(result)
            self._advance_clock(sid,minute)
            self.conn.execute('INSERT INTO batches VALUES(?,?,?)',(batch_key,batch_hash,canonical(results)))
            return results
    def reject_unsubmitted(self,offers,reason,observed_ts=None):
        """Persist a consumed first signal without fabricating an executable quote."""
        if not isinstance(reason,str) or not reason:raise ValueError('拒绝原因不能为空')
        normalized=[self._normalize({**o,'execution_ts':o['quote']['ts'] if observed_ts is None else observed_ts}) for o in offers]
        results=[]
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            for o in normalized:
                # The rejection and its observed time must be one durable fact.
                self._advance_clock(o['quote']['sid'],o['time'])
                previous=self.conn.execute('SELECT hash,body FROM signals WHERE key=?',(o['signal_key'],)).fetchone()
                if previous:
                    if previous['hash']!=o['signal_hash']:raise ValueError('拒绝记录的首信号身份发生改变')
                    results.append(self._current_result(json.loads(previous['body'])));continue
                result={'strategy_id':o['rule']['id'],'sid':o['quote']['sid'],'slot':o['slot'],'status':'REJECTED',
                        'reason':reason,'quote_check_reason':o['reason'],'signal':o['signal'],'quote':o['quote'],
                        'observed_ts':observed_ts,'execution_attempted':False}
                self.conn.execute('INSERT INTO signals VALUES(?,?,?)',(o['signal_key'],o['signal_hash'],canonical(result)))
                results.append(result)
        return results
    def _current_result(self,result):
        if 'intent_id' in result:
            row=self._intent(result['intent_id']);return {**result,'status':row['status'],'filled_units':row['filled']/STAKE_SCALE,'reserved_units':row['reserved']/STAKE_SCALE}
        return result
    def receipt(self,intent_id,receipt_id,status,filled_units='0',confirmed=False):
        try:return self._receipt(intent_id,receipt_id,status,filled_units,confirmed)
        except (ValueError,sqlite3.Error) as error:
            self.alarm('receipt_rejected',{'intent':intent_id,'receipt':receipt_id,'status':status,'error':str(error)})
            raise
    def _receipt(self,intent_id,receipt_id,status,filled_units,confirmed):
        if not isinstance(receipt_id,str) or not receipt_id:raise ValueError('回执ID必须为非空字符串')
        if type(confirmed) is not bool:raise ValueError('回执确认必须为布尔值')
        if status not in ('SUBMITTED_UNKNOWN','PARTIAL','FILLED','CANCELLED','REJECTED','SETTLED'):raise ValueError('未知模拟回执状态')
        filled=scaled(str(filled_units),STAKE_SCALE)
        if filled==MISSING or not 0<=filled<=STAKE_SCALE:raise ValueError('累计成交量不合法')
        if status in ('CANCELLED','REJECTED','SETTLED') and not confirmed:raise ValueError('撤销、拒绝、结算必须有明确模拟回执确认；未知订单继续占额')
        payload={'intent':intent_id,'status':status,'filled':filled,'confirmed':confirmed,'simulation_only':True};h=digest(payload)
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            prior=self.conn.execute('SELECT hash,intent FROM receipts WHERE key=?',(receipt_id,)).fetchone()
            if prior:
                if prior['hash']!=h or prior['intent']!=intent_id:raise ValueError('回执ID被用于不同内容')
                return self.intent(intent_id)
            row=self._intent(intent_id)
            if filled<row['filled']:raise ValueError('累计成交量不能倒退')
            # Cancelling the remainder does not settle already confirmed fills.
            settle_cancelled_fill=(row['status']=='CANCELLED' and status=='SETTLED'
                                   and 0<filled==row['filled'] and confirmed)
            if row['status'] in ('CANCELLED','REJECTED','SETTLED') and not settle_cancelled_fill:raise ValueError('终态订单不能重新打开')
            if row['status']=='FILLED' and status not in ('FILLED','SETTLED'):raise ValueError('已确认全成不能退回未知或部分状态')
            if status=='FILLED' and filled!=row['requested']:raise ValueError('全成状态的数量不足')
            if status=='PARTIAL' and not 0<filled<row['requested']:raise ValueError('部分成交数量不合法')
            if status=='REJECTED' and filled:raise ValueError('已成交订单不能标为全部拒绝')
            if status=='SETTLED' and (not filled or filled!=row['filled']):raise ValueError('结算只能针对已经确认的成交数量')
            reserved=0 if status in ('FILLED','CANCELLED','REJECTED','SETTLED') else row['requested']-filled
            self.conn.execute('UPDATE intents SET status=?,reserved=?,filled=? WHERE id=?',(status,reserved,filled,intent_id))
            self.conn.execute('INSERT INTO receipts VALUES(?,?,?,?)',(receipt_id,intent_id,h,canonical(payload)))
        return self.intent(intent_id)
    def alarm(self,kind,body):
        with self.conn:self.conn.execute('INSERT INTO alarms(kind,body) VALUES(?,?)',(kind,canonical(body)))
    def shadows(self):return [json.loads(r['body']) for r in self.conn.execute('SELECT body FROM shadows ORDER BY rowid')]
    def save_runner_state(self,key,value):
        body={'binding':self.binding,'payload':value};body['sha256']=digest(body)
        with self.conn:self.conn.execute('INSERT INTO runner_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET body=excluded.body',(key,canonical(body)))
    def save_observation(self,value,watermarks):
        """Commit reconstructible feature/pending state and its time fence atomically."""
        values=dict(watermarks)
        for sid,minute in values.items():
            if not isinstance(sid,str) or not sid or type(minute) is not int:
                raise ValueError('Observation boundary requires a match ID and integer time')
        body={'binding':self.binding,'payload':value};body['sha256']=digest(body)
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            self.conn.execute('INSERT INTO runner_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET body=excluded.body',('coordinator',canonical(body)))
            for sid,minute in values.items():self._advance_clock(sid,minute)
    def load_runner_state(self,key):
        row=self.conn.execute('SELECT body FROM runner_state WHERE key=?',(key,)).fetchone()
        if not row:return None
        body=json.loads(row['body']);h=body.pop('sha256')
        if body['binding']!=self.binding or digest(body)!=h:raise ValueError('触发与执行快照校验失败')
        return body['payload']
