"""Empty candidate shortcuts must still authenticate their search evidence."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from lab.common import DEFAULT, atomic_json
from lab.standard_review import select_standard


class ReviewEvidence(unittest.TestCase):
    def test_maximum_match_cap_still_displays_joint_roster_metrics(self):
        from lab.reporting import roster_lines
        row={'名单':'默认','整场上限':24,'情景':4,'政策':'每核心方向首单，无额外整场约束',
             'n':30,'net':15.0,'roi':0.5,'drawdown_match':2.0}
        lines=roster_lines({'comparisons':[row]},{'match_cap':24})
        self.assertEqual(len(lines),1)
        self.assertIn('30 笔',lines[0])
        self.assertIn('15.000',lines[0])

    def test_zero_candidates_without_sealed_proof_cannot_complete_review(self):
        for status in ('COMPLETE', 'NO_DATA'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                folder = root / 'mining' / 'LIVE_OVER'
                folder.mkdir(parents=True)
                atomic_json(folder / 'state.json', {'status': status, 'candidates': 0})
                config = {**DEFAULT, 'directions': ['LIVE_OVER']}
                with patch('lab.selection.finish_selection', return_value={'selection_status': 'FINITE_LOCAL_SEARCH_COMPLETE'}):
                    with self.assertRaisesRegex(ValueError, '证据'):
                        select_standard(root, [], {}, 100, config, lambda **kw: None, lambda: False)


if __name__ == '__main__':
    unittest.main()
