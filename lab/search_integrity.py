"""Bind completed search evidence so lost/modified ledgers cannot count as proof."""
from pathlib import Path
from .common import read_json,atomic_json,sha

REQUIRED={
    'standard_empty_scope':('state.json','dictionary.json','standard_features.json','arrays.npz'),
    'standard_class_dfs':('standard_plan.json','search.sqlite3','state.json'),
    'standard_global_bound':('global_bound_plan.json','global_upper_proof.json','state.json'),
    # A budget/pause/resource stop seals its committed partial ledger so truncation cannot hide candidates.
    'standard_class_dfs_partial':('standard_plan.json','search.sqlite3','state.json'),
}
PARTIAL_SEARCH_STATUSES=('BUDGET_STOP','PAUSED','RESOURCE_BLOCKED','INTERRUPTED')

def seal_search_evidence(root,backend,binding):
    root=Path(root);names=list(REQUIRED[backend])
    if (root/'search_spec.json').is_file():names.append('search_spec.json')
    record={'schema':'FSL_COMPLETED_SEARCH_EVIDENCE_V1','backend':backend,'binding':binding,'files':{name:sha(root/name) for name in names}}
    atomic_json(root/'search_evidence.json',record)
    return record

def verify_search_evidence(root,backend,binding=None):
    root=Path(root);record=read_json(root/'search_evidence.json')
    if not record:raise ValueError('完成搜索缺少证据封存清单')
    if backend not in REQUIRED or not isinstance(record,dict) or not isinstance(record.get('files'),dict):raise ValueError('搜索证据封存范围无效')
    required=set(REQUIRED[backend]);names=set(record.get('files',{}))
    if record.get('schema')!='FSL_COMPLETED_SEARCH_EVIDENCE_V1' or record.get('backend')!=backend or not required<=names or names-required-{'search_spec.json'}:raise ValueError('搜索证据封存范围无效')
    if binding is not None and record.get('binding')!=binding:raise ValueError('搜索证据绑定不一致')
    for name,h in record['files'].items():
        if not (root/name).is_file() or sha(root/name)!=h:raise ValueError('搜索证据缺失或内容改变: '+name)
    return record


def verify_stopped_search(root,binding=None):
    """Check committed stop evidence before any consumer can rewrite its files."""
    root=Path(root);state=read_json(root/'state.json')
    if state is not None and not isinstance(state,dict):raise ValueError('搜索公开状态结构无效')
    status=(state or {}).get('status')
    if status in PARTIAL_SEARCH_STATUSES or status in ('COMPLETE','NO_DATA'):
        backend='standard_class_dfs_partial' if status in PARTIAL_SEARCH_STATUSES else state.get('search_backend','standard_class_dfs')
        verify_search_evidence(root,backend,binding if binding is not None else state.get('binding'))
    elif state is None and (root/'search_evidence.json').exists():
        # Missing state must not turn an existing sealed run into a fresh one.
        raise ValueError('搜索证据缺失或内容改变: state.json')
    elif (root/'search.sqlite3').is_file():
        # A stale/damaged public status must not bypass a committed SQLite stop.
        # Read-only by default and never creating; a hot rollback journal is rolled back once.
        # DELETE journal mode restores the exact sealed bytes, so the evidence check still matches.
        import json,sqlite3
        uri=(root/'search.sqlite3').resolve().as_uri()
        def committed():
            conn=sqlite3.connect(uri+'?mode=ro',uri=True)
            try:
                has_checkpoint=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoint'").fetchone()
                return conn.execute('SELECT body FROM checkpoint WHERE id=1').fetchone() if has_checkpoint else None
            finally:conn.close()
        try:row=committed()
        except sqlite3.OperationalError:
            # A hard kill during a commit leaves a hot rollback journal that only a writable connection can roll back.
            if not (root/'search.sqlite3-journal').is_file():raise
            conn=sqlite3.connect(uri+'?mode=rw',uri=True)
            try:conn.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()
            finally:conn.close()
            row=committed()
        if row:
            checkpoint=json.loads(row[0]);checkpoint_status=checkpoint.get('status')
            if checkpoint_status in PARTIAL_SEARCH_STATUSES or checkpoint_status=='COMPLETE':
                backend='standard_class_dfs_partial' if checkpoint_status in PARTIAL_SEARCH_STATUSES else 'standard_class_dfs'
                verify_search_evidence(root,backend,binding if binding is not None else checkpoint.get('binding'))
    return state
