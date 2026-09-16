"""Preserve the current repair tree without overwriting earlier evidence."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from lab.common import VERSION, source_fingerprint, execution_fingerprint, sha
from lab.release import build_snapshot, release_files, release_fingerprint

target = OUT / 'continuation_baseline.json'
if target.exists():
    raise FileExistsError('Preserve existing continuation evidence')
report = {
    'version': VERSION,
    'research_hash': source_fingerprint(),
    'execution_hash': execution_fingerprint(),
    'release_hash': release_fingerprint(),
    'files': {p.relative_to(ROOT).as_posix(): sha(p) for p in release_files(ROOT)},
    'scope': 'CONTINUATION_BASELINE_AFTER_FIVE_TEST_CORRECTIONS_NOT_ACCEPTANCE',
    'snapshot': build_snapshot(OUT / 'continuation_baseline_source.zip', ROOT),
}
target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({k: v for k, v in report.items() if k != 'files'}, ensure_ascii=True))
