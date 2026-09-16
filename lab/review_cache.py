"""Atomic completed-direction checkpoints for candidate screening.

Incomplete directions are recomputed; completed directions are never trusted
merely because files exist. A committed manifest binds data/rules/config/code and
all output contents. No pickle, eval or generated code is loaded.
"""
from pathlib import Path
from .common import read_json,atomic_json,write_jsonl,read_jsonl,sha

NAMES=('rows','qualified','neighbors','stress')
def load_review(root,binding):
    root=Path(root);m=read_json(root/'manifest.json')
    if m is None:return None
    if not isinstance(m,dict) or not isinstance(m.get('files'),dict) or set(m['files'])!={n+'.jsonl.gz' for n in NAMES}:
        raise ValueError('筛选断点封存范围无效，必须校验全部输出文件')
    if m.get('binding')!=binding:raise ValueError('筛选断点与输入/代码/配置不一致')
    for name,h in m['files'].items():
        if not (root/name).is_file() or sha(root/name)!=h:raise ValueError('筛选断点内容校验失败')
    return {**{n:list(read_jsonl(root/(n+'.jsonl.gz'))) for n in NAMES},'summary':m['summary']}

def save_review(root,binding,summary,**streams):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    # Manifest is written last. Aborted writes without manifest are not resumable.
    for n in NAMES:write_jsonl(root/(n+'.jsonl.gz'),streams[n])
    atomic_json(root/'manifest.json',{'binding':binding,'summary':summary,
      'files':{n+'.jsonl.gz':sha(root/(n+'.jsonl.gz')) for n in NAMES},
      'scope':'complete_direction_review; current direction not fine-grained resumable'})
