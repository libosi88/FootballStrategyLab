import sys, unittest
from pathlib import Path
root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
sys.path.insert(0, str(root/'tests'))

import lab.standard_review as sr
import lab.common as common

_orig = sr.select_standard
def patched(jobdir, events, labels, scale, config, update, should_pause):
    jobdir = Path(jobdir)
    import json
    binding = common.digest({'version': common.VERSION, 'engine': common.source_fingerprint(), 'config': config,
        'input': common.read_json(jobdir/'prepared_manifest.json'),
        'mining': {d: common.read_json(jobdir/'mining'/d/'state.json') for d in config['directions']}})
    import sqlite3
    db = jobdir/'results'/'standard_review.sqlite3'
    if db.exists():
        c = sqlite3.connect(db)
        old = c.execute("SELECT body FROM metadata WHERE key='binding'").fetchone()
        c.close()
        if old and old[0] != binding:
            print('=== BINDING MISMATCH in select_standard ===')
            print('old:', old[0])
            print('new:', binding)
            print('engine:', common.source_fingerprint())
            print('config:', json.dumps(config, ensure_ascii=False, sort_keys=True, default=str))
            print('prepared:', common.read_json(jobdir/'prepared_manifest.json'))
            st = common.read_json(jobdir/'mining/LIVE_OVER/state.json')
            print('state.json status:', st and st.get('status'), 'len', len(json.dumps(st, ensure_ascii=False, default=str)) if st else 0)
    return _orig(jobdir, events, labels, scale, config, update, should_pause)
sr.select_standard = patched

loader = unittest.TestLoader()
suite = loader.loadTestsFromName('test_standard_review.StandardReview')
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
