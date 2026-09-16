"""Live totals must respect an earlier known score without filling raw score cells."""
import tempfile
import unittest
from pathlib import Path

from lab.common import MISSING, sha
from lab.data import inspect, load_inputs
from lab.features import SignalEngine
from lab.historical_pricing import minute_close_payoffs
from lab.selection import Quotes
from test_v031 import quote, write_csv


class MissingLiveScoreQuality(unittest.TestCase):
    def load_quotes(self, specifications):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        rows = []
        for index, fields in enumerate(specifications):
            minute = 40 + index
            row = quote()
            row.update({'盘口类型': '大小球', '比赛分钟': str(minute),
                        '变化时间': f'2024-03-01 20:{minute:02d}'})
            row.update(fields)
            rows.append(row)
        source = root / 'quotes.csv'
        write_csv(source, rows)
        original = sha(source)
        result = load_inputs(inspect([str(root)], root / 'cache'), '真实甲组', '皇冠')
        self.assertEqual(sha(source), original)
        return result

    def test_missing_score_cannot_reopen_total_below_last_known_goals(self):
        events, _, _, audit = self.load_quotes([
            {'当时比分': '2-0', '盘口数值': '2.5'},
            {'当时比分': '', '盘口数值': '1.5', '上水/大球': '0.94'},
            {'当时比分': '', '盘口数值': '1.5', '上水/大球': '0.93'},
            {'当时比分': '2-0', '盘口数值': '2.5', '上水/大球': '0.92'},
        ])
        self.assertEqual([e['valid'] for e in events], [True, False, False, True])
        self.assertEqual([e['quality_blocked'] for e in events], [False, True, True, False])
        self.assertEqual(events[1]['score'], [MISSING, MISSING])
        self.assertEqual(audit['counts']['live_total_line_below_known_goals'], 2)

    def test_missing_score_quote_contributes_no_trade_payoff_or_signal(self):
        events, labels, scale, audit = self.load_quotes([
            {'当时比分': '2-0', '盘口数值': '2.5'},
            {'当时比分': '', '盘口数值': '1.5', '上水/大球': '0.94'},
        ])
        quotes = Quotes(events, labels, scale, 5, minute_close=True)
        self.assertIsNone(quotes.trade(events[1]['eid'], 0, reject_pregoal=True))
        self.assertEqual(minute_close_payoffs(events, labels, scale)[1].tolist(), [0, 0])
        engine = SignalEngine(
            [{'id': 'after_40', 'direction': 'LIVE_OVER',
              'conditions': [{'feature': 'minute', 'op': 'ge', 'value': 41}]}],
            contract=audit['contract'], execution_policy={'feature_version': 'v3'})
        engine.mark_history_complete('one')
        self.assertEqual([signal for e in events for signal in engine.feed(e)], [])

    def test_plausible_total_with_missing_score_is_not_overblocked(self):
        events, _, _, _ = self.load_quotes([
            {'当时比分': '2-0', '盘口数值': '2.5'},
            {'当时比分': '', '盘口数值': '3', '上水/大球': '0.94'},
        ])
        self.assertTrue(all(e['valid'] for e in events))
        self.assertEqual(events[1]['score'], [MISSING, MISSING])

    def test_score_correction_replaces_previous_known_score(self):
        events, _, _, _ = self.load_quotes([
            {'当时比分': '2-0', '盘口数值': '2.5'},
            {'当时比分': '1-0', '盘口数值': '1.5', '上水/大球': '0.94'},
            {'当时比分': '', '盘口数值': '1.5', '上水/大球': '0.93'},
        ])
        self.assertTrue(all(e['valid'] for e in events))

    def test_unknown_score_does_not_use_other_match_or_final_score(self):
        events, _, _, _ = self.load_quotes([
            {'sId': 'one', '当时比分': '2-0', '盘口数值': '2.5'},
            {'sId': 'two', '当时比分': '', '盘口数值': '0.5'},
            {'sId': 'two', '当时比分': '0-0', '盘口数值': '0.5', '上水/大球': '0.94'},
        ])
        self.assertTrue(all(e['valid'] for e in events))
        self.assertEqual(events[1]['score'], [MISSING, MISSING])

    def test_price_refresh_alone_cannot_clear_impossible_total(self):
        events, _, _, audit = self.load_quotes([
            {'当时比分': '0-0', '盘口数值': '1.5'},
            {'当时比分': '2-0', '盘口数值': '1.5'},
            {'当时比分': '', '盘口数值': '1.5', '上水/大球': '0.94'},
            {'当时比分': '', '盘口数值': '2.5', '上水/大球': '0.93'},
        ])
        self.assertEqual([e['valid'] for e in events], [True, False, False, True])
        self.assertEqual(audit['counts']['live_total_line_below_known_goals'], 2)


if __name__ == '__main__':
    unittest.main()
