import unittest

# Always the product module; a stray tests/selection.py must never be what is tested.
from lab.selection import describe_alternative


def rule(net=2000, streak=3, drawdown=600):
    return {'stress': [{'net_i': net + delta} for delta in (20, 0, 10)],
            'metrics': {'streak': streak, 'drawdown_i': drawdown}}


class IndiaAlternativeComparisons(unittest.TestCase):
    def compare(self, current, alternative, criterion):
        return describe_alternative(current, alternative, criterion, {'a', 'b'}, {'a', 'c'}, 100)

    def test_profit_better_equal_and_worse_are_relative_to_current(self):
        for net, relation, improves in ((2200, '较优', True), (2000, '持平', False), (1800, '较差', False)):
            with self.subTest(net=net):
                row = self.compare(rule(), rule(net=net), '压力净胜较高')
                self.assertEqual(row['相对当前'], relation)
                self.assertEqual(row['是否优于当前'], improves)
                self.assertEqual(row['对照类型'] == '压力净胜较高', improves)
                self.assertEqual(row['当前值'], 10)
                self.assertEqual(row['备选值'], net / 200)
                self.assertEqual(row['差值_备选减当前'], (net - 2000) / 200)
                self.assertIn('备选中最差压力净胜最高', row['备选池排名依据'])

    def test_streak_better_equal_and_worse(self):
        for streak, relation, improves in ((2, '较优', True), (3, '持平', False), (4, '较差', False)):
            with self.subTest(streak=streak):
                row = self.compare(rule(), rule(streak=streak), '历史连亏较低')
                self.assertEqual((row['相对当前'], row['是否优于当前']), (relation, improves))
                self.assertEqual(row['对照类型'] == '历史连亏较低', improves)
                self.assertEqual(row['差值_备选减当前'], streak - 3)

    def test_drawdown_better_equal_and_worse(self):
        for drawdown, relation, improves in ((400, '较优', True), (600, '持平', False), (800, '较差', False)):
            with self.subTest(drawdown=drawdown):
                row = self.compare(rule(), rule(drawdown=drawdown), '历史回撤较低')
                self.assertEqual((row['相对当前'], row['是否优于当前']), (relation, improves))
                self.assertEqual(row['对照类型'] == '历史回撤较低', improves)
                self.assertEqual(row['差值_备选减当前'], (drawdown - 600) / 200)

    def test_observed_india_alternative_is_not_better(self):
        current = rule(net=3877, streak=3, drawdown=624)  # 19.385, 3, 3.12
        alternative = rule(net=2002, streak=3, drawdown=787)  # 10.01, 3, 3.935
        rows = {criterion: self.compare(current, alternative, criterion) for criterion in
                ('压力净胜较高', '历史连亏较低', '历史回撤较低')}
        self.assertEqual([row['相对当前'] for row in rows.values()], ['较差', '持平', '较差'])
        self.assertTrue(all(row['是否优于当前'] is False for row in rows.values()))
        self.assertEqual(rows['压力净胜较高']['差值_备选减当前'], -9.375)
        self.assertAlmostEqual(rows['历史回撤较低']['差值_备选减当前'], 0.815)

    def test_coverage_is_descriptive_and_identical_sets_are_not_different(self):
        same = describe_alternative(rule(), rule(), '覆盖不同', {'a', 'b'}, {'b', 'a'}, 100)
        self.assertEqual(same['对照类型'], '覆盖相同')
        self.assertIsNone(same['是否优于当前'])
        self.assertEqual((same['新增覆盖比赛'], same['减少覆盖比赛']), (0, 0))
        changed = self.compare(rule(), rule(), '覆盖不同')
        self.assertEqual(changed['对照类型'], '覆盖不同')
        self.assertIsNone(changed['是否优于当前'])
        self.assertEqual((changed['新增覆盖比赛'], changed['减少覆盖比赛']), (1, 1))
        self.assertEqual(changed['差值_备选减当前'], 0)


if __name__ == '__main__':
    unittest.main()
