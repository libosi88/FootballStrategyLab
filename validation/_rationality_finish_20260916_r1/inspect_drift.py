"""Read-only comparison of current release files against the failed acceptance seal."""
import json,sys
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from lab.common import sha,source_fingerprint,execution_fingerprint
from lab.release import release_files,release_fingerprint

source=ROOT/'validation/_rationality_finish_20260916_r1/acceptance_20260916T155545Z_2782d68a/finish_result.json'
report=json.loads(source.read_text(encoding='utf-8'))
before=report['source_manifest']
after={p.relative_to(ROOT).as_posix():sha(p) for p in release_files(ROOT)}
result={'expected_identity':report['identity'],
        'current_identity':{'research_hash':source_fingerprint(),'execution_hash':execution_fingerprint(),'release_hash':release_fingerprint(ROOT)},
        'changed':[{'file':k,'expected':before[k],'actual':after[k],
                    'modified_utc':datetime.fromtimestamp((ROOT/k).stat().st_mtime,timezone.utc).isoformat()}
                   for k in sorted(before.keys()&after.keys()) if before[k]!=after[k]],
        'added':sorted(after.keys()-before.keys()),'removed':sorted(before.keys()-after.keys()),
        'production_modified':False}
print(json.dumps(result,ensure_ascii=False,indent=2))
