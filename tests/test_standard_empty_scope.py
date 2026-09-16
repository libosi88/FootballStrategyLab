"""Empty standard scopes distinguish unavailable research data from known zeros."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from lab.common import DEFAULT, MISSING, read_json
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.contracts import bind_event, make_contract
from lab.standard_mining import mine_standard


CONTRACT = make_contract('标准空样本边界', '皇冠', 100)


def quote(i=0, *, line=2, water=(95, 85), score=(0, 0),
          closed=False, market=1, phase=1):
    valid = (not closed and line != MISSING and min(water) > 0
             and (market == 1 or line >= 0))
    return bind_event({
        'sid': 'a', 'mid': 0, 'eid': i, 'market': market, 'phase': phase,
        'ts': i, 'row': i + 2, 'event_key': 'empty.csv:' + str(i + 2),
        'source': 'empty.csv', 'minute': i, 'minute_raw': str(i),
        'status': '滚' if phase else '早', 'score': list(score), 'line': line,
        'water': list(water), 'valid': valid, 'closed': closed,
    }, CONTRACT)


class StandardEmptyScope(unittest.TestCase):
    def check_scope(self, rows, *, direction='LIVE_GIVE', eligible=True,
                    complete=False, reason=None, counts=None):
        labels = {'a': {'eligible': eligible, 'final': [2, 1], 'year': 2024}}
        config = {**DEFAULT, 'profile': 'standard', 'directions': [direction]}
        with tempfile.TemporaryDirectory() as td:
            args = (td, rows, labels, 100, direction, config,
                    lambda **kwargs: None, lambda: False)
            state = mine_standard(*args)
            self.assertEqual(state['status'], 'NO_DATA')
            self.assertEqual(state['standard_scope_complete'], complete)
            self.assertEqual(bool(state['blocked']), not complete)
            if reason:
                self.assertIn(reason, state['blocked']['S0'])
            if counts:
                for key, value in counts.items():
                    self.assertEqual(state['empty_scope_counts'][key], value, key)
            self.assertEqual(state['candidates'], 0)
            self.assertEqual(state['raw_total'], sum(state[k] for k in (
                'representatives', 'equivalent_occurrences', 'proven_zero',
                'proven_nonprofitable', 'remaining')))
            self.assertIn('可评价报价子集', state['zero_proof_scope'])
            path = Path(td) / 'mining' / direction / 'state.json'
            self.assertEqual(read_json(path), state)
            # A prepare-only call and a search resume must retain the same
            # classification while reading the previously persisted arrays.
            self.assertEqual(mine_standard(*args, prepare_only=True), state)
            self.assertEqual(mine_standard(*args), state)
            self.assertEqual(read_json(path), state)
            return state

    def test_missing_live_ah_score_blocks_all_applicable_ah_sides(self):
        for direction, line in (('LIVE_GIVE', 2), ('LIVE_RECEIVE', -2),
                                ('LIVE_HOME', 2), ('LIVE_AWAY', -2),
                                ('LIVE_PK_HOME', 0), ('LIVE_PK_AWAY', 0)):
            with self.subTest(direction=direction):
                self.check_scope([quote(line=line, score=(MISSING, MISSING))],
                                 direction=direction, reason='当时比分',
                                 counts={'missing_live_score_rows': 1})

    def test_ineligible_results_block_a_valid_applicable_quote(self):
        self.check_scope([quote()], eligible=False, reason='没有合格赛果',
                         counts={'ineligible_result_rows': 1})

    def test_open_invalid_quote_blocks(self):
        for changes in ({'line': MISSING}, {'water': (MISSING, 85)},
                        {'water': (95, 0)}, {'water': (-1, 85)}):
            with self.subTest(changes=changes):
                self.check_scope([quote(**changes)], reason='合法盘口/水位',
                                 counts={'invalid_open_rows': 1})

    def test_all_explicitly_closed_is_a_known_zero_even_with_missing_fields(self):
        self.check_scope([quote(closed=True, line=MISSING,
                                water=(MISSING, MISSING),
                                score=(MISSING, MISSING))], eligible=False,
                         complete=True, counts={'closed_rows': 1,
                                                'invalid_open_rows': 0,
                                                'ineligible_result_rows': 0,
                                                'missing_live_score_rows': 0})

    def test_known_inapplicable_lines_are_zero_without_settlement_requirements(self):
        for direction, line in (('LIVE_GIVE', 0), ('LIVE_RECEIVE', 0),
                                ('LIVE_PK_HOME', 2), ('LIVE_PK_AWAY', -2),
                                ('PRE_PK_HOME', 2)):
            with self.subTest(direction=direction):
                self.check_scope([quote(line=line, score=(MISSING, MISSING),
                                        phase=0 if direction.startswith('PRE') else 1)],
                                 direction=direction, eligible=False, complete=True,
                                 counts={'nonapplicable_rows': 1})

    def test_inapplicable_line_does_not_require_a_usable_water_price(self):
        self.check_scope([quote(line=2, water=(MISSING, 0))],
                         direction='LIVE_PK_HOME', complete=True,
                         counts={'nonapplicable_rows': 1, 'invalid_open_rows': 0})

    def test_mixed_known_zeros_cannot_hide_missing_settlement_basis(self):
        self.check_scope([quote(0, line=0), quote(1, closed=True),
                          quote(2, score=(MISSING, MISSING))], reason='当时比分',
                         counts={'source_rows': 3, 'nonapplicable_rows': 1,
                                 'closed_rows': 1, 'missing_live_score_rows': 1})

    def test_mixed_known_zero_cannot_hide_unknown_applicability(self):
        self.check_scope([quote(0, line=2), quote(1, line=MISSING)],
                         direction='LIVE_PK_HOME', reason='合法盘口/水位',
                         counts={'nonapplicable_rows': 1, 'invalid_open_rows': 1})

    def test_result_and_score_blockers_are_both_reported(self):
        state = self.check_scope([quote(score=(MISSING, MISSING))], eligible=False,
                                 reason='没有合格赛果',
                                 counts={'ineligible_result_rows': 1,
                                         'missing_live_score_rows': 1})
        self.assertIn('当时比分', state['blocked']['S0'])
        self.assertIn('可以重叠', state['empty_scope_counting'])

    def test_no_target_source_blocks_for_empty_and_other_market_phase_inputs(self):
        for rows in ([], [quote(market=0)], [quote(phase=0)]):
            with self.subTest(rows=len(rows), scope=[(r['market'], r['phase']) for r in rows]):
                self.check_scope(rows, reason='没有该市场/阶段的源报价',
                                 counts={'source_rows': 0})

    def test_empty_features_cannot_silently_discard_an_evaluable_quote(self):
        labels = {'a': {'eligible': True, 'final': [2, 1], 'year': 2024}}
        with tempfile.TemporaryDirectory() as td, patch(
                'lab.standard_mining.build_direction',
                return_value={'eid': np.array([], dtype=np.int64),
                              'mid': np.array([], dtype=np.int64)}):
            with self.assertRaisesRegex(ValueError, '标准特征数组为空'):
                mine_standard(td, [quote()], labels, 100, 'LIVE_GIVE',
                              {**DEFAULT, 'profile': 'standard'},
                              lambda **kwargs: None, lambda: False)


if __name__ == '__main__':
    unittest.main()
