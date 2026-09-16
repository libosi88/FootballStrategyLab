"""Algorithm/storage/execution validation without recursive pipeline self-tests."""
import unittest
from .common import ROOT

def flatten(suite):
    for item in suite:
        if isinstance(item,unittest.TestSuite):yield from flatten(item)
        else:yield item

def main():
    import sys
    sys.path.insert(0,str(ROOT/'tests'))
    discovered=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    suite=unittest.TestSuite(t for t in flatten(discovered) if not t.id().startswith('test_pipeline_standard.'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1

if __name__=='__main__':raise SystemExit(main())
