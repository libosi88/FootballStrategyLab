"""Preserve concurrent working-tree edits and verify a private immutable candidate.

Copies approved source and reviewed acceptance helpers only. Never changes or
rebases the user's running workspace. Validation outputs are inside the candidate.
"""
import json,shutil,sys,uuid
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from lab.release import freeze_release_tree,manifest_fingerprint
from lab.common import sha,atomic_json

stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8]
target=ROOT/'dist'/('FSL_070_candidate_'+stamp)
manifest=freeze_release_tree(target,ROOT)
helpers=['validation/_rationality_finish_20260916_r1/run_acceptance.py',
         'validation/_rationality_finish_20260916_r1/verify_extended.py',
         'validation/_rationality_upgrade_20260916_r1/update_presentation.py']
for name in helpers:
    dest=target/name;dest.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(ROOT/name,dest)
    if sha(dest)!=sha(ROOT/name):raise RuntimeError('Helper changed while copied: '+name)
record={'status':'CANDIDATE_NOT_YET_ACCEPTED','candidate':str(target),
        'original_project':str(ROOT),'initial_release_hash':manifest_fingerprint(manifest),
        'files':manifest,'helpers':{name:sha(target/name) for name in helpers},
        'original_worktree_preserved':True,'real_jobs_started':False}
atomic_json(BASE/('isolation_'+stamp+'.json'),record)
atomic_json(BASE/'CURRENT_ISOLATED_CANDIDATE.json',record)
print(json.dumps({k:v for k,v in record.items() if k not in ('files','helpers')},ensure_ascii=True,indent=2))
