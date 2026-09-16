"""Run full suite while logging source fingerprint and target cache states per test."""
import sys, unittest
from pathlib import Path
root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
sys.path.insert(0, str(root/'tests'))

from lab.common import source_fingerprint, read_json, sha
import json

results = {}

class LoggedRunner(unittest.TextTestRunner):
    pass

# Wrap TextTestResult to record fingerprints around each test
from unittest.result import TestResult
class FPR(TestResult):
    def startTest(self, test):
        super().startTest(test)
        self._fp_before = source_fingerprint()
        self._t = test.id()
    def stopTest(self, test):
        fp_after = source_fingerprint()
        if fp_after != self._fp_before:
            results.setdefault('fp_changes', []).append((test.id(), self._fp_before, fp_after))
        super().stopTest(test)

loader = unittest.TestLoader()
suite = loader.discover(str(root/'tests'), pattern='test_*.py')
runner = unittest.TextTestRunner(resultclass=FPR, verbosity=1)
r = runner.run(suite)
print('\n===== FINGERPRINT CHANGES =====')
if results.get('fp_changes'):
    for item in results['fp_changes']:
        print(item)
else:
    print('none')
sys.exit(0 if r.wasSuccessful() else 1)
