"""Risk-constrained, resumable local search; no claim of global optimality.

Every candidate move is a complete deterministic order replay. Risk is a research return/drawdown
ratio (worst priced net >= R x worst match-ordered drawdown), never an inferred account authorization,
so the budget grows with the roster instead of capping its size. A profit tolerance of
max(absolute floor, share x anchor) is anchored to the best feasible profit seen along each search,
preventing many small permitted profit sacrifices from silently accumulating into a large loss.
"""
from pathlib import Path
from collections import OrderedDict
import os,time,hashlib
from .common import digest, atomic_json, read_json, canonical,money_threshold,historical_objective
from .mining import Paused
from .research_standard import portfolio_feasible,fraction

METHOD='FSL_portfolio_local_v06_segment_safe'


def move_count(pool,chosen):
    n=len(chosen);u=len(set(pool)-set(chosen))
    return u+n+n*u


class MoveList:
    """The ordered add/remove/replace neighbourhood of one roster, sorted once instead of per move."""
    def __init__(self,pool,chosen):
        self.ids=sorted(chosen);self.unselected=sorted(set(pool)-set(self.ids))
        self.total=len(self.unselected)+len(self.ids)+len(self.ids)*len(self.unselected)

    def at(self,index):
        ids=self.ids;unselected=self.unselected;u=len(unselected);n=len(ids)
        if not 0<=index<u+n+n*u:raise IndexError('组合动作游标越界')
        if index<u:
            add=unselected[index];return 'ADD',add,None,tuple(sorted(ids+[add]))
        index-=u
        if index<n:
            remove=ids[index];return 'REMOVE',None,remove,tuple(i for i in ids if i!=remove)
        index-=n;ri,ai=divmod(index,u)
        add,remove=unselected[ai],ids[ri]
        return 'REPLACE',add,remove,tuple(sorted([i for i in ids if i!=remove]+[add]))


def move_at(pool,chosen,index):
    return MoveList(pool,chosen).at(index)


def best_in_band(options,anchor,tolerance):
    viable=[s for s in options if s['feasible'] and s['min_profit_i']>=max(0,anchor-tolerance)]
    if not viable:return None
    # Within an explicitly fixed economic tolerance, lower risk takes precedence.
    return min(viable,key=lambda s:(s['max_drawdown_i'],-s['matches_floor'],
               s['condition_count'],len(s['ids']),-s['min_profit_i'],tuple(s['ids'])))


class PortfolioSearch:
    def __init__(self,pool,score_fn,config,scale,root,tag,risk_ratio,update=None,should_pause=None):
        self.pool={r['id']:r for r in pool};self.ids=sorted(self.pool)
        self.score_fn=score_fn;self.config=config;self.scale=scale;self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.tag=tag;self.risk_ratio=str(risk_ratio);fraction(self.risk_ratio)
        self.tol_floor=money_threshold(config['selection_profit_tolerance'],2*scale)
        self.tol_share=fraction(config.get('selection_profit_tolerance_share','0'))
        self.update=update or (lambda **kw:None);self.should_pause=should_pause or (lambda:False)
        self.cache=OrderedDict();self.score_computations=0;self.conditions={i:len(self.pool[i]['conditions']) for i in self.ids}
        # Exact input includes offers and their pnl; cache must not cross different quote scenarios.
        self.binding=digest({'method':METHOD,'config':config,'risk_ratio':self.risk_ratio,
                             'pool':[(i,self.pool[i]['conditions'],self.pool[i].get('_execution_priority'),self.pool[i]['_trade_ref'] if '_trade_ref' in self.pool[i] else self.pool[i]['_trades']) for i in self.ids]})

    def tolerance(self,anchor):
        """Relative tolerance: a few percent of the best feasible profit, never below the absolute floor."""
        return max(self.tol_floor,int(self.tol_share*max(0,anchor)))

    def _rebase(self,ids):
        # Incremental scorers replay the neighbours of this roster; plain score functions have no base.
        rebase=getattr(self.score_fn,'rebase',None)
        if rebase is not None:rebase(ids)

    def score(self,ids):
        key=tuple(sorted(ids))
        if key in self.cache:
            self.cache.move_to_end(key);return self.cache[key]
        # Id-based scorers skip building rule lists for every neighbour.
        fast=getattr(self.score_fn,'score_ids',None)
        value=fast(key) if fast is not None else self.score_fn([self.pool[i] for i in key]);self.score_computations+=1
        stress=value['stress'];raw=value['raw'];allm=[value.get('historical',raw)] if historical_objective(self.config) else [raw,*stress]
        s={'ids':list(key),'min_profit_i':min(x['net_i'] for x in allm),
           'max_drawdown_i':max(x['drawdown_match_i'] for x in allm),
           'matches_floor':min(x['matches'] for x in allm),
           'condition_count':sum(self.conditions[i] for i in key),
           'raw':raw,'stress':stress}
        s['feasible']=portfolio_feasible(s['min_profit_i'],s['max_drawdown_i'],self.risk_ratio,empty=not key)
        from .history_policy import portfolio_history_reasons
        s['stability_reasons']=portfolio_history_reasons(allm[0],self.config,empty=not key)
        s['feasible']=s['feasible'] and not s['stability_reasons']
        self.cache[key]=s
        if len(self.cache)>4096:self.cache.popitem(last=False)
        return s

    def _one(self,start,label):
        p=self.root/(label+'.json');journal=self.root/(label+'.jsonl');checkpoint=read_json(p)
        start=tuple(sorted(start))
        loaded=checkpoint is not None
        if loaded:
            if not isinstance(checkpoint,dict) or checkpoint.get('payload_sha256')!=digest({k:v for k,v in checkpoint.items() if k!='payload_sha256'}):
                raise ValueError('组合断点内容证据不完整或已改变')
            if checkpoint.get('binding')!=self.binding or checkpoint.get('seed')!=list(start):
                raise ValueError('组合筛选断点与规则/订单/风险政策不兼容')
        else:
            score=self.score(start)
            if not score['feasible']:start=();score=self.score(start)
            checkpoint={'binding':self.binding,'seed':list(start),'chosen':list(start),'iteration':0,
                        'cursor':0,'evaluated':0,'anchor':max(0,score['min_profit_i']),
                        'options':[score],'visited':[digest(list(start))], 'moves_committed':0,
                        'status':'RUNNING','journal_bytes':0,'actions':[]}
        if not journal.exists():
            if loaded:raise ValueError('组合审计日志缺失，拒绝复用已提交断点')
            journal.touch()
        if type(checkpoint.get('journal_bytes')) is not int or checkpoint['journal_bytes']<0:raise ValueError('组合审计日志断点长度无效')
        if journal.stat().st_size<checkpoint['journal_bytes']:raise ValueError('组合审计日志短于已提交断点')
        prefix_hash=hashlib.sha256()
        with journal.open('rb') as checked:
            left=checkpoint['journal_bytes']
            while left:
                if self.should_pause():raise Paused()
                chunk=checked.read(min(left,1048576))
                if not chunk:raise ValueError('组合审计日志在校验中缩短')
                prefix_hash.update(chunk);left-=len(chunk)
        if loaded and checkpoint.get('journal_sha256')!=prefix_hash.hexdigest():raise ValueError('组合审计日志已提交内容证据不符')
        if checkpoint['status'] in ('LOCAL_STOP','EVAL_BUDGET_STOP','ACTION_BUDGET_STOP'):
            if journal.stat().st_size!=checkpoint['journal_bytes']:raise ValueError('已停止组合审计日志长度改变')
            return checkpoint
        # Uncommitted log suffix can be safely regenerated. No orders are ever sent.
        with journal.open('r+b') as f:f.truncate(checkpoint['journal_bytes'])
        if len(checkpoint['visited'])!=1+len(checkpoint['actions']):raise ValueError('组合断点的访问记录与动作记录不一致')
        # Visited rosters are the seed and every committed move; membership is checked on their id tuples.
        visited_states={tuple(checkpoint['seed']),*(tuple(a['to']) for a in checkpoint['actions'])}
        with journal.open('ab') as f:
            saved_at=[time.monotonic()]
            def save():
                f.flush();os.fsync(f.fileno());checkpoint['journal_bytes']=f.tell()
                checkpoint['journal_sha256']=prefix_hash.hexdigest()
                checkpoint['payload_sha256']=digest({k:v for k,v in checkpoint.items() if k!='payload_sha256'})
                atomic_json(p,checkpoint);saved_at[0]=time.monotonic()
            while not int(self.config['select_budget']) or checkpoint['iteration']<int(self.config['select_budget']):
                chosen=checkpoint['chosen'];moves=MoveList(self.ids,chosen);total=moves.total;self._rebase(chosen)
                while checkpoint['cursor']<total:
                    if self.should_pause():checkpoint['status']='PAUSED';save();raise Paused()
                    limit=int(self.config.get('selection_eval_budget',200000))
                    if limit and checkpoint['evaluated']>=limit:
                        checkpoint['status']='EVAL_BUDGET_STOP';save();return checkpoint
                    pos=checkpoint['cursor'];kind,add,remove,target=moves.at(pos)
                    s=self.score(target);checkpoint['evaluated']+=1
                    visited=target in visited_states
                    if s['feasible']:
                        checkpoint['anchor']=max(checkpoint['anchor'],s['min_profit_i'])
                        floor=checkpoint['anchor']-self.tolerance(checkpoint['anchor'])
                        checkpoint['options']=[o for o in checkpoint['options'] if o['min_profit_i']>=floor]
                        if not visited:checkpoint['options'].append(s)
                    row={'run':self.tag,'seed':label,'iteration':checkpoint['iteration'],'move':pos,
                         'action':kind,'add':add,'remove':remove,'candidate_ids':list(target),
                         'net_min':s['min_profit_i']/(2*self.scale),'drawdown_max':s['max_drawdown_i']/(2*self.scale),
                         'risk_ratio':self.risk_ratio,'feasible':s['feasible'],
                         'status':'RISK_REJECTED' if not s['feasible'] else 'VISITED' if visited else 'EVALUATED'}
                    encoded=(canonical(row)+'\n').encode('utf-8');f.write(encoded);prefix_hash.update(encoded);checkpoint['cursor']+=1
                    # Count-based checkpoints, plus one at least every 30 seconds when evaluations are slow.
                    if checkpoint['cursor']%int(self.config.get('selection_checkpoint_every',64))==0 or time.monotonic()-saved_at[0]>=30:
                        checkpoint['status']='RUNNING';save()
                        self.update(message='实际比较组合加入/替换/移除，风险约束参与选择',
                                    selection_tag=self.tag,selection_evaluated=checkpoint['evaluated'])
                current=self.score(chosen)
                options=checkpoint['options']+[current]
                best=best_in_band(options,checkpoint['anchor'],self.tolerance(checkpoint['anchor']))
                if best is None or best['ids']==chosen:
                    checkpoint['status']='LOCAL_STOP';save();return checkpoint
                checkpoint['actions'].append({'from':chosen,'to':best['ids'],
                     'before_net':current['min_profit_i']/(2*self.scale),'after_net':best['min_profit_i']/(2*self.scale),
                     'before_dd':current['max_drawdown_i']/(2*self.scale),'after_dd':best['max_drawdown_i']/(2*self.scale)})
                checkpoint['chosen']=best['ids'];checkpoint['visited'].append(digest(best['ids']));visited_states.add(tuple(best['ids']));checkpoint['moves_committed']+=1
                checkpoint['iteration']+=1;checkpoint['cursor']=0;checkpoint['options']=[best]
                checkpoint['status']='RUNNING';save()
            checkpoint['status']='ACTION_BUDGET_STOP';save();return checkpoint

    def run(self,extra_seeds=()):
        # Complete set of eligible singletons for the fixed seed policies; never top-K by net only.
        # Singleton scores are deterministic under the frozen pool binding. A
        # paused precheck may be recomputed; no partial seed ranking is used.
        if self.should_pause():raise Paused()
        self._rebase(())
        total=len(self.ids);single=[];every=max(1,int(self.config.get('selection_checkpoint_every',64)))
        self.update(message='逐条预评分全部候选以确定固定起点；暂停后可确定性重算',
                    selection_tag=self.tag,selection_phase='SINGLETON_PRECHECK',
                    selection_singletons_evaluated=0,selection_singletons_total=total)
        for index,i in enumerate(self.ids,1):
            if self.should_pause():raise Paused()
            single.append(self.score((i,)))
            if index%every==0 or index==total:
                self.update(message='逐条预评分全部候选以确定固定起点；暂停后可确定性重算',
                            selection_tag=self.tag,selection_phase='SINGLETON_PRECHECK',
                            selection_singletons_evaluated=index,selection_singletons_total=total)
        if self.should_pause():raise Paused()
        feasible=[x for x in single if x['feasible']]
        self.update(message='全部候选单例预评分完成；进入冻结起点的组合比较',
                    selection_tag=self.tag,selection_phase='NEIGHBORHOOD_SEARCH',
                    selection_singletons_evaluated=total,selection_singletons_total=total)
        seeds=[('empty',())]
        if feasible:
            p=min(feasible,key=lambda x:(-x['min_profit_i'],x['max_drawdown_i'],-x['matches_floor'],tuple(x['ids'])))
            r=min(feasible,key=lambda x:(x['max_drawdown_i'],-x['matches_floor'],-x['min_profit_i'],tuple(x['ids'])))
            seeds += [('profit',tuple(p['ids'])),('risk',tuple(r['ids']))]
        for i,seed in enumerate(extra_seeds):
            if self.score(seed)['feasible']:seeds.append((f'direction_union_{i}',tuple(sorted(seed))))
        seen=set();paths=[]
        for name,seed in seeds:
            if seed in seen:continue
            seen.add(seed);st=self._one(seed,name);paths.append({'name':name,**st,'score':self.score(st['chosen'])})
        peak=max(x['score']['min_profit_i'] for x in paths)
        chosen=best_in_band([x['score'] for x in paths],peak,self.tolerance(peak)) or self.score(())
        out={'method':METHOD,'risk_ratio':self.risk_ratio,'binding':self.binding,'chosen':chosen['ids'],
             'score':chosen,'paths':[{'name':x['name'],'status':x['status'],'chosen':x['chosen'],
                'evaluated':x['evaluated'],'iterations':x['iteration'],'moves_committed':x['moves_committed'],'actions':x['actions']} for x in paths],
             'singleton_evaluations':total,'seed_count':len(paths),'score_computations':self.score_computations,'replay_method':getattr(self.score_fn,'method','dispatch_full_replay'),
             'budget_scope':'selection_eval_budget counts per-seed neighborhood actions; singleton precheck and seed definitions are disclosed separately',
             'status':'LOCAL_SEARCH_COMPLETE' if all(x['status']=='LOCAL_STOP' for x in paths) else 'PARTIAL_SELECTION_BUDGET',
             'scope':'full add/remove/one-for-one replacement neighborhoods in tested qualified pool; fixed starts; not global optimality'}
        atomic_json(self.root/'result.json',out)
        return out
