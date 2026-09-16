"""Explicit dry-run/apply cleanup of reconstructible completed-direction caches."""
from pathlib import Path
from contextlib import ExitStack,closing
import os,sqlite3
from .common import read_json,DEFAULT,sha
from .locking import AllWorkerSlots as WorkspaceLock,WorkspaceBusy
from .standard_mining import cache_entries,_entry_bytes,release_direction_caches

def cleanup_workspace(workspace,apply=False):
    root=Path(workspace).resolve()
    if not (root/'registry.sqlite3').is_file():raise ValueError('请选择已初始化的工作区，不能对任意数据目录清理')
    records=[];skipped=[];planned=freed=0;owners={}
    with ExitStack() as locks:
        for directory,dirs,files in os.walk(root,followlinks=False):
            folder=Path(directory)
            kept=[]
            for name in dirs:
                p=folder/name
                if p.is_symlink() or getattr(p,'is_junction',lambda:False)() or not p.resolve().is_relative_to(root):skipped.append({'path':str(p),'reason':'链接或外部目录保留'})
                else:kept.append(name)
            dirs[:]=kept
            if folder.parent.name!='mining' or 'state.json' not in files:continue
            dirs[:]=[]
            owner=root
            for ancestor in folder.parents:
                if (ancestor/'registry.sqlite3').is_file():owner=ancestor;break
                if ancestor==root:break
            if owner not in owners:
                try:
                    if apply:locks.enter_context(WorkspaceLock(owner))
                    with closing(sqlite3.connect((owner/'registry.sqlite3').as_uri()+'?mode=ro',uri=True)) as conn:
                        owners[owner]=conn.execute("SELECT 1 FROM jobs WHERE status IN ('RUNNING','QUEUED','PAUSING') LIMIT 1").fetchone() is None
                except WorkspaceBusy:owners[owner]=False
            if not owners[owner]:skipped.append({'path':str(folder),'reason':'工作区仍有活动/排队任务'});continue
            state=read_json(folder/'state.json',{})
            if state.get('status') not in ('COMPLETE','NO_DATA'):skipped.append({'path':str(folder),'reason':'未完成方向，保留恢复缓存'});continue
            if not all((folder/name).is_file() for name in ('arrays.npz','atoms.sqlite3')):skipped.append({'path':str(folder),'reason':'缺少可重建基础文件，保留缓存'});continue
            try:entries=cache_entries(folder)
            except ValueError as error:skipped.append({'path':str(folder),'reason':str(error)});continue
            size=sum(_entry_bytes(p) for p in entries);planned+=size
            if not entries:continue
            try:
                features=read_json(folder/'standard_features.json',{})
                dictionary=read_json(folder/'dictionary_complete.json',{}) or read_json(folder/'dictionary.json',{})
                if features.get('sha256')!=sha(folder/'arrays.npz') or (dictionary.get('database_sha256') or dictionary.get('atoms_sha256'))!=sha(folder/'atoms.sqlite3'):raise ValueError('重建基础文件缺少匹配的冻结哈希')
            except (ValueError,OSError) as error:
                planned-=size;skipped.append({'path':str(folder),'reason':str(error)});continue
            record={'path':str(folder),'eligible_bytes':size,'entries':[p.name for p in entries]}
            if apply:
                result=release_direction_caches(folder,state,DEFAULT);record['result']=result;freed+=result['freed_bytes']
            records.append(record)
    return {'mode':'APPLY' if apply else 'DRY_RUN','workspace':str(root),'eligible_bytes':planned,'freed_bytes':freed,'directions':records,'retained':skipped,'scope':'Only declared caches of completed directions; original data, arrays, dictionaries, search ledgers, proofs and paused directions retained.'}
