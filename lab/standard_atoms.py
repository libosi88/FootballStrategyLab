"""Disk-backed v3 dictionary with reversible normalization aliases."""
from pathlib import Path
from collections import OrderedDict
import sqlite3,json,time
import shutil
from .common import canonical,digest,atomic_json,read_json,sha,MISSING
from .standard_features import EXTRA_FIELDS
from .standard_spec import *
from .rules import label,unit

W=1;C=2;T=4;X=8;P=16

def normalize_atom(atom):
    a={k:v for k,v in atom.items() if k!='label'}
    if a.get('pulse') is False:a.pop('pulse')
    if a.get('sequence')=='contiguous':a.pop('sequence')
    if a.get('event_model')=='projected':a.pop('event_model')
    for name in ('min_line_step','min_water_step'):
        if a.get(name)==1:a.pop(name)
    return a

class AtomCatalog:
    def __init__(self,path,writable=False):
        self.path=Path(path);self.cache=OrderedDict()
        if writable:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            self.conn=sqlite3.connect(self.path)
        else:
            self.conn=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            self.conn.execute('PRAGMA cache_size=-65536')
            if writable:
                self.conn.execute('CREATE TABLE IF NOT EXISTS atoms(id INTEGER PRIMARY KEY,key TEXT UNIQUE,body TEXT,groups INTEGER,feature TEXT)')
                self.conn.execute('CREATE TABLE IF NOT EXISTS aliases(key TEXT PRIMARY KEY,atom_id INTEGER,body TEXT,groups INTEGER)')
                self.conn.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,body TEXT)')
            self.next_id=self.conn.execute('SELECT COALESCE(MAX(id),-1)+1 FROM atoms').fetchone()[0]
        except BaseException:
            self.conn.close()
            raise
    def add(self,a,groups,scale):
        normalized=normalize_atom(a);key=digest(normalized);body={**a,'label':label(a,scale)}
        found=self.conn.execute('SELECT id FROM atoms WHERE key=?',(key,)).fetchone()
        if found:
            i=found[0];self.conn.execute('UPDATE atoms SET groups=groups|? WHERE id=?',(groups,i))
        else:
            i=self.next_id;self.next_id+=1
            self.conn.execute('INSERT INTO atoms VALUES(?,?,?,?,?)',(i,key,canonical(body),groups,a['feature']))
        alias_key=digest({k:v for k,v in a.items() if k!='label'})
        self.conn.execute('INSERT INTO aliases VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET groups=groups|excluded.groups',(alias_key,i,canonical(body),groups))
        return i
    def __len__(self):return self.next_id
    def __getitem__(self,i):
        if isinstance(i,slice):return [self[n] for n in range(*i.indices(len(self))) ]
        i=int(i)
        if i in self.cache:self.cache.move_to_end(i);return self.cache[i]
        row=self.conn.execute('SELECT body FROM atoms WHERE id=?',(i,)).fetchone()
        if row is None:raise IndexError(i)
        a=json.loads(row[0]);self.cache[i]=a
        if len(self.cache)>1024:self.cache.popitem(last=False)
        return a
    def __iter__(self):
        for row in self.conn.execute('SELECT body FROM atoms ORDER BY id'):yield json.loads(row[0])
    def ids(self,groups):return [r[0] for r in self.conn.execute('SELECT id FROM atoms WHERE groups & ? != 0 ORDER BY id',(groups,))]
    def commit(self):self.conn.commit()
    def close(self):self.conn.close();self.cache.clear()
    def __enter__(self):return self
    def __exit__(self,*exc):self.close()

def feature_kind(name):
    if name.startswith(('path','linepath')) or name.startswith('pre_last_path'):return 'category'
    if name in ('status','score_code','stoppage_base','cross_phase','pre_closed','cross_line_alignment','cross_init_alignment'):return 'category'
    if name in ('minute','goal_diff','abs_diff','total_goals','role_diff','stoppage_added','stoppage_total','cross_age') or name.endswith('_changes'):return 'integer'
    if 'water' in name or name.startswith('other_') or name in ('samewater','otherwater','other_samewater'):return 'water'
    return 'line'

def verify_frozen_dictionary(root):
    """The single integrity gate for a committed dictionary.

    Every path that reuses a frozen direction goes through here, so a tampered
    atoms.sqlite3 can never be trusted by a shortcut that skips the build.
    Returns the committed manifest, or None when nothing has been committed yet.
    """
    root=Path(root);marker=root/'dictionary_complete.json';path=root/'atoms.sqlite3'
    if not marker.exists():return None
    info=read_json(marker)
    if not info.get('database_sha256') or not path.exists() or sha(path)!=info['database_sha256']:
        raise ValueError('已冻结标准字典的完整性校验失败，不能复用')
    return info


def build_compact_catalog(root,columns,direction,scale,config,update,should_pause):
    """Frozen compact dictionary: every atom is both a single condition (W) and a pair member (C)."""
    import numpy as np
    from .mining import Paused
    path=root/'atoms.sqlite3';marker=root/'dictionary_complete.json'
    atoms=AtomCatalog(path,True);domains={};blocked={}
    try:
        def domain(name):
            if name.startswith('cross_') and config.get('cross_stale_minutes') is None:
                blocked['M06']='未冻结跨市场报价陈旧度，另一市场条件未纳入紧凑语法';return None
            try:span=columns.domain(name)
            except KeyError:span=None
            domains[name]={'domain':span,'kind':feature_kind(name),'unit':unit(name,scale)}
            return span
        def values(name):
            if domain(name) is None:return None
            try:column=np.asarray(columns[name])
            except KeyError:return None
            return {int(v) for v in np.unique(column[column!=MISSING])}
        generated=balanced_atoms(direction,scale,domain,values) if grammar_of(config)=='balanced_v1' else ((a,True) for a in compact_atoms(direction,scale,domain,values))
        for number,(a,core) in enumerate(generated):
            atoms.add(a,W|C if core else W,scale)
            if number%256==0:
                atoms.commit()
                if should_pause():raise Paused()
        atoms.commit()
        update(message='生成已登记规格规则字典：'+grammar_of(config),dictionary_atoms=len(atoms))
        info={'spec_version':spec_version_for(config),'grammar':grammar_of(config),'max_conditions':max_conditions_for(config),'atoms_file':'atoms.sqlite3','atom_count':len(atoms),
              'raw_atom_templates':atoms.conn.execute('SELECT COUNT(*) FROM aliases').fetchone()[0],
              'groups':{name:len(atoms.ids(flag)) for name,flag in (('W',W),('C',C),('T',T),('M06',X),('M07',P))},'domains':domains,'blocked':blocked,
              'normalization':'Pre-registered compact grids clipped to observed feature domains; identical predicates share an id. Historical mask equivalence is recorded separately.'}
        info['database_sha256']=sha(path)
        atomic_json(marker,info);return atoms,info
    except BaseException:atoms.close();raise

def build_standard_catalog(root,columns,direction,scale,config,update=None,should_pause=None):
    from .mining import Paused,ResourcePaused
    root=Path(root);root.mkdir(parents=True,exist_ok=True);update=update or (lambda **kw:None);should_pause=should_pause or (lambda:False)
    info=verify_frozen_dictionary(root)
    if info is not None:return AtomCatalog(root/'atoms.sqlite3'),info
    if grammar_of(config) in ('compact_v1','balanced_v1'):return build_compact_catalog(root,columns,direction,scale,config,update,should_pause)
    path=root/'atoms.sqlite3';marker=root/'dictionary_complete.json'
    atoms=AtomCatalog(path,True);domains={};blocked={}
    saved=atoms.conn.execute("SELECT body FROM meta WHERE key='build_cursor_v1'").fetchone()
    cursor=json.loads(saved[0]) if saved else {'features':0,'timed':0,'domains':{},'blocked':{}}
    domains=cursor['domains'];blocked=cursor['blocked']
    last_checkpoint=time.monotonic()
    def checkpoint(force=False,**kw):
        nonlocal last_checkpoint
        cursor.update(kw,domains=domains,blocked=blocked)
        # The cursor contains all preceding domains. Serializing and fsyncing it
        # for every empty window is quadratic work. Incomplete feature batches
        # are replayed idempotently; pause/end always commit their exact cursor.
        now=time.monotonic()
        if not force and 'features' in kw and kw['features']%64 and now-last_checkpoint<2:return
        atoms.conn.execute("INSERT INTO meta VALUES('build_cursor_v1',?) ON CONFLICT(key) DO UPDATE SET body=excluded.body",(canonical(cursor),));atoms.commit()
        last_checkpoint=now
    phase=direction.startswith('LIVE');ah=not direction.endswith(('OVER','UNDER'))
    # Historical numerical fields already supplied by the base causal stream.
    # line_same / line_return_* are identically zero by definition: auditable fields, never search atoms.
    names=sorted((set(columns)|EXTRA_FIELDS)-{'eid','mid','side','pnl','year','ts','quality','line_same','line_return_keep','line_return_reset'})
    names=[n for n in names if not n.startswith(('window_','path','linepath','span','pulse','pmin'))]
    names=[n for n in names if phase or n not in ('minute','goal_diff','abs_diff','total_goals','score_code','role_diff','stoppage_base','stoppage_added','stoppage_total')]
    names=[n for n in names if ah or not n.startswith('role_')]
    if phase:
        names += [f'window_{lo}_{hi}_{kind}' for lo,hi in time_windows('W') for kind in ('line_init','water_init','otherwater_init')]
    try:
        for number,name in enumerate(names):
            if number<cursor['features']:continue
            if should_pause():checkpoint(force=True);raise Paused()
            if name.startswith('cross_') and config.get('cross_stale_minutes') is None:
                blocked['M06']='未冻结跨市场报价陈旧度，跨市场价格不可用';continue
            if name.startswith('pre_') and not phase:continue
            domain=columns.domain(name);domains[name]={'domain':domain,'kind':feature_kind(name),'unit':unit(name,scale)}
            mandatory_group=X if name.startswith('cross_') else P if name.startswith('pre_') else W if name.startswith('window_') else W|C
            for template in required_difference_templates(name,scale,feature_kind(name)):atoms.add(template,mandatory_group,scale)
            if domain is None:checkpoint(features=number+1);continue
            kind=feature_kind(name)
            if name.startswith('cross_'):levels=(('C',X),)
            elif name.startswith('pre_'):levels=(('C',P),)
            elif name.startswith('window_'):levels=(('W',W),)
            else:levels=(('W',W),('C',C))
            for level,group in levels:
                if name=='minute':generated=minute_atoms(level)
                elif kind=='category':generated=({'feature':name,'op':'eq','value':int(v)} for v in sorted(set(columns[name][columns[name]!=MISSING])))
                else:generated=scalar_atoms(name,*domain,scale if kind=='water' else 4 if kind=='line' else 1,level,kind)
                for offset,a in enumerate(generated):
                    atoms.add(a,group,scale)
                    if offset%512==0:
                        atoms.commit()
                        if should_pause():checkpoint(force=True);raise Paused()
                        if shutil.disk_usage(root).free<int(config.get('min_free_disk_mb',256))*1048576:raise ResourcePaused('字典落盘空间不足；已生成规则保留，未缩小阈值网格')
            checkpoint(features=number+1);update(message='生成v3完整特征网格与可恢复规则字典',dictionary_features=number+1,dictionary_feature_total=len(names),dictionary_atoms=len(atoms),dictionary_feature=name)
        if phase:
            for level,group in (('W',W),('C',C)):
                for a in minute_atoms(level):atoms.add(a,group,scale)
        for a in required_templates(scale,ah):atoms.add(a,W|C,scale)
        for composite in (False,True):
            for a in path_atoms(scale,False,composite):atoms.add(a,W|C,scale)
        for mode in ('keep','reset'):
            for code in (11,12,21,22):atoms.add({'feature':'linepath2_'+mode,'op':'eq','value':code},W|C,scale)
        for number,a in enumerate(path_atoms(scale,True)):
            if number<cursor['timed']:continue
            if number%512==0:
                checkpoint(timed=number)
                if should_pause():raise Paused()
                update(message='生成全部T跨度/步幅/顺序语义',dictionary_atoms=len(atoms),timed_atoms_generated=number)
            found=atoms.conn.execute('SELECT id,groups FROM atoms WHERE key=?',(digest(normalize_atom(a)),)).fetchone()
            if found and not found[1]&T:
                # Default T parameters normalize to an already registered W/C path atom. Keep the
                # original definition as an alias, but do not count it again as a new T atom (M04).
                atoms.conn.execute('INSERT INTO aliases VALUES(?,?,?,?) ON CONFLICT(key) DO NOTHING',(digest({k:v for k,v in a.items() if k!='label'}),found[0],canonical({**a,'label':label(a,scale)}),0))
                continue
            atoms.add(a,T,scale)
        checkpoint(force=True)
        info={'spec_version':SPEC_VERSION,'atoms_file':'atoms.sqlite3','atom_count':len(atoms),'raw_atom_templates':atoms.conn.execute('SELECT COUNT(*) FROM aliases').fetchone()[0],
              'groups':{name:len(atoms.ids(flag)) for name,flag in (('W',W),('C',C),('T',T),('M06',X),('M07',P))},'domains':domains,'blocked':blocked,
              'normalization':'Explicit defaults and identical predicates share an id; aliases retain the original full machine condition. Historical mask equivalence is recorded separately.'}
        info['database_sha256']=sha(path)
        atomic_json(marker,info);return atoms,info
    except BaseException:atoms.close();raise
