"""Disk-backed full-event masks with exact historical equivalence classes.

Every class retains all atom identities. Sharing a mask is not a claim that two
rules will remain equivalent on future matches.
"""
from pathlib import Path
from collections import OrderedDict,defaultdict
import sqlite3,zlib,hashlib,shutil
import numpy as np
from .common import digest,MISSING
from .mining import pack_masks,njit,_ctz,Paused,ResourcePaused

@njit(cache=True)
def mask_economics(words,ends,payoffs):
    count=0;net=0;upper=0;start=0
    while start<len(words):
        end=ends[start];seen=False;best=0
        for w in range(start,end):
            bits=words[w]
            while bits:
                bit=_ctz(bits);value=payoffs[w*64+bit]
                if not seen:count+=1;net+=value;seen=True
                if value>best:best=value
                bits=bits&(bits-np.uint64(1))
        upper+=best;start=end
    return count,net,upper

class _ScalarPrefixes:
    """Exact packed scalar truth sets; cached boundaries never inspect P&L."""
    def __init__(self,values,positions,buffer,limit):
        self.values=values;self.positions=positions;self.buffer=buffer;self.limit=limit
        self.unique=np.unique(values[values!=MISSING])
        self.zero=np.zeros(len(buffer)//64,np.uint64);self.valid=self._pack(values!=MISSING)
        self.cache=OrderedDict();self.bytes=0
    def _pack(self,truth):
        self.buffer.fill(0);self.buffer[self.positions]=truth
        return np.packbits(self.buffer,bitorder='little').view('<u8').copy()
    def prefix(self,count):
        if count==0:return self.zero
        if count==len(self.unique):return self.valid
        if count in self.cache:self.cache.move_to_end(count);return self.cache[count]
        words=self._pack((self.values!=MISSING)&(self.values<self.unique[count]))
        self.cache[count]=words;self.bytes+=words.nbytes
        while self.bytes>self.limit and len(self.cache)>1:
            _,previous=self.cache.popitem(last=False);self.bytes-=previous.nbytes
        return words
    def predicate(self,atom):
        lower=int(np.searchsorted(self.unique,atom['value'],side='left'))
        if atom['op']=='ge':return self.valid^self.prefix(lower)
        if atom['op']=='le':return self.prefix(int(np.searchsorted(self.unique,atom['value'],side='right')))
        if atom['op']=='eq':return self.prefix(int(np.searchsorted(self.unique,atom['value'],side='right')))^self.prefix(lower)
        if atom['op']=='range':
            if atom['upper']<=atom['value']:return self.zero
            return self.prefix(int(np.searchsorted(self.unique,atom['upper'],side='left')))^self.prefix(lower)
        raise ValueError('不支持的标量比较')

class MaskStore:
    def __init__(self,root,columns,atoms,config,binding):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.columns=columns;self.atoms=atoms;self.config=config
        self.conn=sqlite3.connect(self.root/'masks.sqlite3')
        self.binding=binding
        self._scalar_feature=None;self._scalar_prefix=None
        self._members_snapshot=None;self._members_revision=None;self._groups_cache={}
        self._group_update=lambda **kw:None;self._group_pause=lambda:False
        self.conn.execute('CREATE TABLE IF NOT EXISTS blobs(hash TEXT PRIMARY KEY,payload BLOB,n INTEGER,net INTEGER,upper INTEGER,checksum TEXT)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS members(atom INTEGER PRIMARY KEY,hash TEXT,checksum TEXT)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT)')
        old=self.conn.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
        if old and old[0]!=binding:self.conn.close();raise ValueError('事件掩码绑定改变，禁止复用')
        self.conn.execute("INSERT OR IGNORE INTO meta VALUES('binding',?)",(binding,));self.conn.commit()
        baseline,_,_,self.ends,self.payoffs=pack_masks(columns,[]);self.all_words=baseline[0];self.word_count=len(self.all_words)
        self.zero=np.zeros(self.word_count,np.uint64);self.cache=OrderedDict();self.bytes=0;self.limit=max(1,int(config.get('max_mask_mb',1024))//2)*1048576
        mids=columns['mid'];self.positions=np.empty(len(mids),np.int64);word=0
        cuts=np.r_[0,np.flatnonzero(mids[1:]!=mids[:-1])+1,len(mids)] if len(mids) else [0]
        for a,b in zip(cuts[:-1],cuts[1:]):
            self.positions[a:b]=np.arange(word*64,word*64+b-a);word+=(int(b-a)+63)//64
        self.buffer=np.zeros(self.word_count*64,np.uint8)
        self.conn.execute("INSERT OR IGNORE INTO blobs VALUES('ZERO',?,0,0,0,?)",(b'',digest([binding,'ZERO',0,0,0])));self.conn.commit()
    def _remember(self,key,words):
        if key in self.cache:self.cache.move_to_end(key);return self.cache[key]
        self.cache[key]=words;self.bytes+=words.nbytes
        while self.bytes>self.limit and len(self.cache)>1:
            _,old=self.cache.popitem(last=False);self.bytes-=old.nbytes
        return words
    def get(self,key):
        if key=='ZERO':return self.zero
        if key in self.cache:self.cache.move_to_end(key);return self.cache[key]
        row=self.conn.execute('SELECT payload FROM blobs WHERE hash=?',(key,)).fetchone()
        if row is None:raise ValueError('掩码缓存缺失: '+key)
        words=np.frombuffer(zlib.decompress(row[0]),dtype='<u8').copy()
        if len(words)!=self.word_count or hashlib.sha256(words.tobytes()).hexdigest()!=key:raise ValueError('掩码缓存校验不符')
        return self._remember(key,words)
    def store(self,words):
        if not np.any(words):return 'ZERO'
        raw=words.astype('<u8',copy=False).tobytes();key=hashlib.sha256(raw).hexdigest()
        if self.conn.execute('SELECT 1 FROM blobs WHERE hash=?',(key,)).fetchone() is None:
            n,net,upper=mask_economics(words,self.ends,self.payoffs)
            self.conn.execute('INSERT INTO blobs VALUES(?,?,?,?,?,?)',(key,zlib.compress(raw,3),int(n),int(net),int(upper),digest([self.binding,key,int(n),int(net),int(upper)])))
        self._remember(key,words);return key
    def statistics(self,key):
        row=self.conn.execute('SELECT n,net,upper,checksum FROM blobs WHERE hash=?',(key,)).fetchone()
        if row is None:raise ValueError('掩码指标不存在')
        values=tuple(int(v) for v in row[:3])
        if digest([self.binding,key,*values])!=row[3]:raise ValueError('掩码收益或安全上界校验失败')
        return values
    def intersection(self,left,right):
        if left=='ZERO' or right=='ZERO':return 'ZERO'
        if left==right:return left
        return self.store(self.get(left)&self.get(right))
    def _scalar_words(self,atom):
        # Compound path semantics stay on the existing predicate implementation.
        if set(atom)-{'feature','op','value','upper','label'} or atom.get('op') not in ('eq','le','ge','range'):return None
        # Text half-time (-2) never satisfies numeric minute comparisons; use the predicate path.
        if atom['feature']=='minute' and atom['op']!='eq':return None
        feature=atom['feature']
        if self._scalar_feature!=feature:
            values=self.columns.column(feature) if hasattr(self.columns,'column') else self.columns[feature]
            self._scalar_prefix=_ScalarPrefixes(values,self.positions,self.buffer,int(self.config.get('max_feature_cache_mb',128))*1048576)
            self._scalar_feature=feature
        return self._scalar_prefix.predicate(atom)
    def set_group_control(self,update,should_pause):
        self._group_update=update;self._group_pause=should_pause
    def _group_tick(self,number,total,message):
        if number%8192==0:
            if self._group_pause():raise Paused()
            if number%65536==0:self._group_update(message=message,group_rows=number,group_rows_total=total)
    def _member_revision(self):
        return self.conn.total_changes,self.conn.execute('PRAGMA data_version').fetchone()[0]
    def _validated_members(self):
        revision=self._member_revision()
        if revision==self._members_revision and self._members_snapshot is not None:return self._members_snapshot
        rows={}
        for number,(atom,key,checksum) in enumerate(self.conn.execute('SELECT atom,hash,checksum FROM members ORDER BY atom')):
            self._group_tick(number,len(self.atoms),'校验全部原子到掩码成员，建立可复用快照')
            if checksum!=digest([self.binding,atom,key]):raise ValueError('原子到历史掩码映射校验失败')
            rows[atom]=key
        if self._group_pause():raise Paused()
        if self._member_revision()!=revision:raise ValueError('成员映射校验期间发生变化，拒绝混合快照')
        if len(rows)!=len(self.atoms) or any(atom!=number for number,atom in enumerate(rows)):
            raise ValueError('原子到历史掩码成员不完整或越界，需重建损坏缓存')
        self._members_snapshot=rows;self._members_revision=revision;self._groups_cache.clear()
        self._group_update(message='完整成员校验完成；按冻结集合复用分组',group_rows=len(rows),group_rows_total=len(self.atoms))
        return rows
    def prepare(self,update,should_pause):
        done=self.conn.execute('SELECT COALESCE(MAX(atom),-1)+1 FROM members').fetchone()[0]
        for i in range(done,len(self.atoms)):
            if i%128==0:
                self.conn.commit()
                if should_pause():raise Paused()
                if shutil.disk_usage(self.root).free<int(self.config.get('min_free_disk_mb',256))*1048576:raise ResourcePaused('磁盘空间不足，完整字典及已提交掩码保留')
                update(message='计算完整事件原子掩码并精确归组',mask_atoms=i,mask_atoms_total=len(self.atoms))
            atom=self.atoms[i];words=self._scalar_words(atom)
            if words is not None:key=self.store(words)
            else:
                hit=self.columns.atom_mask(atom)
                if not np.any(hit):key='ZERO'
                else:
                    self.buffer.fill(0);self.buffer[self.positions]=hit
                    key=self.store(np.packbits(self.buffer,bitorder='little').view('<u8').copy())
            self.conn.execute('INSERT INTO members VALUES(?,?,?)',(i,key,digest([self.binding,i,key])))
        self.conn.commit()
        return {'atoms':len(self.atoms),'historical_mask_classes':self.conn.execute('SELECT COUNT(DISTINCT hash) FROM members').fetchone()[0]}
    def groups(self,ids):
        if self._group_pause():raise Paused()
        rows=self._validated_members();request=tuple(ids)
        if any(not isinstance(atom,(int,np.integer)) or isinstance(atom,bool) or atom not in rows for atom in request):raise ValueError('请求包含不存在或非法的原子ID')
        if request in self._groups_cache:return self._groups_cache[request]
        selected=sorted(set(request));out=defaultdict(list)
        for number,atom in enumerate(selected):
            self._group_tick(number,len(selected),'按原子顺序生成冻结历史掩码组')
            if atom in rows:out[rows[atom]].append(atom)
        # Registration (priority) order, not hash order: a budget stop covers the earliest registered classes first.
        result=[{'hash':key,'members':members} for key,members in sorted(out.items(),key=lambda item:item[1][0])]
        self._groups_cache[request]=result
        return result
    def close(self):
        self.conn.commit();self.conn.close();self.cache.clear();self._groups_cache.clear()
        self._members_snapshot=None;self._members_revision=None;self._scalar_prefix=None;self._scalar_feature=None
    def __enter__(self):return self
    def __exit__(self,*exc):self.close()
