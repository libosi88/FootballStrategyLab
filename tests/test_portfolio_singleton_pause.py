from pathlib import Path
import tempfile
import unittest
from lab.common import DEFAULT
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.mining import Paused
# Always the product module; a stray tests/portfolio_search.py must never be what is tested.
from lab.portfolio_search import PortfolioSearch


class PortfolioSingletonPause(unittest.TestCase):
    def pool(self):
        return [{'id': f'r{i}', 'conditions': [], '_trades': [[]]} for i in (4, 1, 3, 0, 2)]

    def scorer(self, calls):
        def score(rows):
            calls.append(tuple(row['id'] for row in rows))
            metric = {'net_i': sum((int(row['id'][1:]) + 1) * 400 for row in rows),
                      'drawdown_match_i': len(rows) * 100, 'matches': len(rows) * 10}
            return {'raw': metric.copy(), 'stress': [metric.copy() for _ in range(3)]}
        return score

    def solver(self, directory, calls, pause=lambda: False, updates=None):
        return PortfolioSearch(self.pool(), self.scorer(calls),
                               {**DEFAULT, 'selection_checkpoint_every': 2},
                               100, directory, 'TEST', '5',
                               (lambda **row: updates.append(row)) if updates is not None else None,
                               pause)

    def test_pause_before_precheck_does_no_scoring(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []
            with self.assertRaises(Paused):
                self.solver(td, calls, pause=lambda: True).run()
            self.assertEqual(calls, [])

    def test_mid_precheck_pause_can_deterministically_recompute(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            calls = []
            with self.assertRaises(Paused):
                self.solver(root / 'resumed', calls, pause=lambda: len(calls) >= 3).run()
            self.assertEqual(calls, [('r0',), ('r1',), ('r2',)])
            resumed = self.solver(root / 'resumed', []).run()
            uninterrupted = self.solver(root / 'fresh', []).run()
            self.assertEqual(resumed, uninterrupted)
            self.assertEqual(resumed['chosen'], ['r0', 'r1', 'r2', 'r3', 'r4'])
            self.assertEqual(resumed['status'], 'LOCAL_SEARCH_COMPLETE')

    def test_progress_does_not_change_sorted_candidate_order_or_result(self):
        with tempfile.TemporaryDirectory() as td:
            updates = []
            calls = []
            result = self.solver(Path(td) / 'progress', calls, updates=updates).run()
            reference = self.solver(Path(td) / 'quiet', []).run()
            self.assertEqual(result, reference)
            self.assertEqual(calls[:5], [(f'r{i}',) for i in range(5)])
            precheck = [row for row in updates if row.get('selection_phase') == 'SINGLETON_PRECHECK']
            self.assertEqual([row['selection_singletons_evaluated'] for row in precheck], [0, 2, 4, 5])
            self.assertTrue(all(row['selection_singletons_total'] == 5 for row in precheck))
            self.assertTrue(any(row.get('selection_phase') == 'NEIGHBORHOOD_SEARCH' for row in updates))


if __name__ == '__main__':
    unittest.main()
