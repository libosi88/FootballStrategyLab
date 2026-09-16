"""Completed portfolio results still require intact committed audit evidence."""
import tempfile
import unittest
from pathlib import Path
from lab.common import DEFAULT, atomic_json, read_json
from lab.portfolio_search import PortfolioSearch


class PortfolioEvidence(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.pool = [{'id': name, 'conditions': [], '_trades': [[]]} for name in ('a', 'b')]

    def search(self):
        def score(rules):
            metrics = {'net_i': 400 * len(rules), 'drawdown_match_i': 100 if rules else 0,
                       'matches': 20 * len(rules)}
            return {'raw': metrics, 'stress': [], 'historical': metrics}
        return PortfolioSearch(self.pool, score, DEFAULT, 100, self.root, 'audit', '2').run()

    def test_completed_result_is_reusable_when_evidence_is_intact(self):
        first = self.search()
        second = self.search()
        self.assertEqual(first['chosen'], second['chosen'])
        self.assertEqual(first['paths'], second['paths'])

    def test_missing_completed_journal_is_rejected(self):
        self.search()
        (self.root / 'empty.jsonl').unlink()
        with self.assertRaisesRegex(ValueError, '日志|证据'):
            self.search()

    def test_changed_completed_journal_is_rejected_even_at_same_length(self):
        self.search()
        journal = self.root / 'empty.jsonl'
        original = journal.read_bytes()
        self.assertTrue(original)
        journal.write_bytes(b'!' + original[1:])
        with self.assertRaisesRegex(ValueError, '日志|证据'):
            self.search()

    def test_changed_completed_checkpoint_is_rejected(self):
        self.search()
        path = self.root / 'empty.json'
        checkpoint = read_json(path)
        checkpoint['evaluated'] += 100000
        atomic_json(path, checkpoint)
        with self.assertRaisesRegex(ValueError, '断点|证据'):
            self.search()


if __name__ == '__main__':
    unittest.main()
