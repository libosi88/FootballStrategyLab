"""Regressions for the five input-layer repairs: handoff discovery, quote rows,
index labels, archive member names and the paired cross-market staleness window."""
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from lab.common import check_config, sha
from lab.data import inspect, load_inputs, safe_extract
from test_v031 import quote, write_csv

QUALITY_FIELDS = ['sId', 'reason', 'scope', 'source']
INDEX_FIELDS = ['sId', '联赛', '全场比分', '状态', '日期']


def write_quality(path, sid='one', reason='pending_score_verify'):
    write_csv(path, [{'sId': sid, 'reason': reason, 'scope': 'all', 'source': 'audit'}], QUALITY_FIELDS)


def write_index(path, scores, sid='one'):
    write_csv(path, [{'sId': sid, '联赛': '真实甲组', '全场比分': value, '状态': '完',
                      '日期': '2024-03-01'} for value in scores], INDEX_FIELDS)


def data_tree(folder, sid='one'):
    """One data tree: quote archive plus its own quality handoff under _build."""
    write_csv(folder / '完整指数_甲_2024.csv', [quote(sid)])
    write_quality(folder / '_build' / 'handoff_current' / 'exclude_sids.csv', sid)
    return folder / '_build' / 'handoff_current' / 'exclude_sids.csv'


class Temporary(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / 'work'

    def quality_records(self, manifest):
        return [record for record in manifest['files'] if record['kind'] == 'quality']


class QualityHandoffDiscovery(Temporary):
    def test_selected_parent_folder_finds_the_handoff_inside_the_data_tree(self):
        selected = self.root / 'selected'
        handoff = data_tree(selected / '按国家分类数据')
        manifest = inspect([str(selected)], self.work)
        records = self.quality_records(manifest)
        self.assertEqual(len(records), 1)
        self.assertEqual(Path(records[0]['path']), handoff.resolve())
        self.assertEqual(manifest['quality']['files'], [str(handoff.resolve())])
        _, labels, _, audit = load_inputs(manifest, '真实甲组', '皇冠')
        self.assertFalse(labels['one']['eligible'])
        self.assertEqual(audit['counts']['source_quality_excluded_matches'], 1)

    def test_zip_input_finds_the_handoff_inside_the_extracted_archive(self):
        staging = self.root / 'staging' / '按国家分类数据'
        data_tree(staging)
        archive = self.root / 'input.zip'
        with zipfile.ZipFile(archive, 'w') as stream:
            for source in sorted(staging.rglob('*.csv')):
                stream.write(source, '按国家分类数据/' + source.relative_to(staging).as_posix())
        manifest = inspect([str(archive)], self.work)
        records = self.quality_records(manifest)
        self.assertEqual(len(records), 1)
        discovered = Path(records[0]['path'])
        self.assertEqual(discovered.parts[-3:], ('_build', 'handoff_current', 'exclude_sids.csv'))
        self.assertIn((self.work / 'unpacked').resolve(), discovered.parents)

    def test_upward_search_from_a_selected_file_or_folder_still_works(self):
        tree = self.root / '按国家分类数据'
        season = tree / '中国' / '中超' / '2024'
        write_csv(season / '完整指数_甲_2024.csv', [quote()])
        handoff = tree / '_build' / 'handoff_current' / 'exclude_sids.csv'
        write_quality(handoff)
        for number, selected in enumerate((season / '完整指数_甲_2024.csv', season)):
            with self.subTest(selected=selected.name):
                manifest = inspect([str(selected)], self.root / f'work_{number}')
                records = self.quality_records(manifest)
                self.assertEqual([Path(record['path']) for record in records], [handoff.resolve()])

    def test_two_data_trees_under_one_selection_are_refused(self):
        selected = self.root / 'selected'
        data_tree(selected / '甲树', 'one')
        data_tree(selected / '乙树', 'two')
        with self.assertRaisesRegex(ValueError, '多份质量交割清单'):
            inspect([str(selected)], self.work)

    def test_handoff_below_another_underscore_folder_is_ignored(self):
        selected = self.root / 'selected'
        write_csv(selected / '完整指数_甲_2024.csv', [quote()])
        write_quality(selected / '_归档' / '按国家分类数据' / '_build' / 'handoff_current' / 'exclude_sids.csv')
        manifest = inspect([str(selected)], self.work)
        self.assertEqual(self.quality_records(manifest), [])
        self.assertEqual(manifest['quality']['files'], [])
        _, labels, _, audit = load_inputs(manifest, '真实甲组', '皇冠')
        self.assertTrue(labels['one']['eligible'])
        self.assertEqual(audit['counts'].get('source_quality_excluded_matches', 0), 0)


class QuoteRowValidation(Temporary):
    def quotes(self, rows):
        path = self.root / 'quotes.csv'
        write_csv(path, rows)
        return path

    def direct_manifest(self, path):
        return {'files': [{'path': str(path), 'sha256': sha(path), 'kind': 'quotes', 'joined_results': True}]}

    def test_supported_quote_without_identity_is_rejected(self):
        path = self.quotes([{**quote(), 'sId': ''}])
        with self.assertRaisesRegex(ValueError, 'sId缺失或变化时间'):
            load_inputs(self.direct_manifest(path), '真实甲组', '皇冠')
        with self.assertRaisesRegex(ValueError, 'sId缺失或变化时间'):
            inspect([str(path)], self.work)

    def test_supported_quote_with_unreadable_change_time_is_rejected(self):
        for value in ('', '2024-03-01', '2024-03-01 20:20:30', '2024/03/01 20:20', '不是时间'):
            with self.subTest(value=value):
                path = self.quotes([{**quote(), '变化时间': value}])
                with self.assertRaisesRegex(ValueError, 'sId缺失或变化时间'):
                    load_inputs(self.direct_manifest(path), '真实甲组', '皇冠')

    def test_unsupported_market_or_status_rows_are_counted_not_rejected(self):
        path = self.quotes([quote('one'), {**quote('other_market'), '盘口类型': '波胆'},
                            {**quote('other_status'), '状态': '未'}])
        events, labels, _, audit = load_inputs(inspect([str(path)], self.work), '真实甲组', '皇冠')
        self.assertEqual(audit['counts']['quote_rows'], 3)
        self.assertEqual(audit['counts']['unsupported_market_or_status_rows'], 2)
        self.assertEqual(len(events), 1)
        self.assertEqual(set(labels), {'one'})


class IndexLabelIsolation(Temporary):
    def load(self, scores):
        quotes = self.root / 'quotes.csv'
        write_csv(quotes, [quote()])
        index = self.root / 'index.csv'
        write_index(index, scores)
        manifest = inspect([str(quotes), str(index)], self.work)
        return load_inputs(manifest, '真实甲组', '皇冠')

    def test_unreadable_index_result_isolates_the_match(self):
        _, labels, _, audit = self.load(['比分未知'])
        self.assertFalse(labels['one']['eligible'])
        self.assertTrue(labels['one']['conflict'])
        self.assertIn('final', labels['one']['malformed_index_label_fields'])
        self.assertEqual(audit['counts']['malformed_index_label_rows'], 1)

    def test_later_valid_index_row_does_not_clear_the_isolation(self):
        _, labels, _, audit = self.load(['比分未知', '2-1'])
        self.assertFalse(labels['one']['eligible'])
        self.assertTrue(labels['one']['conflict'])
        self.assertIn('final', labels['one']['malformed_index_label_fields'])
        self.assertEqual(labels['one']['final'], [2, 1])
        self.assertEqual(audit['counts']['malformed_index_label_rows'], 1)

    def test_empty_index_result_is_absence_not_a_malformed_label(self):
        _, labels, _, audit = self.load(['', '-', '--'])
        self.assertTrue(labels['one']['eligible'])
        self.assertEqual(labels['one']['final'], [2, 1])
        self.assertNotIn('malformed_index_label_fields', labels['one'])
        self.assertEqual(audit['counts'].get('malformed_index_label_rows', 0), 0)


class ArchiveMemberNames(Temporary):
    def archive(self, members, name='input.zip'):
        path = self.root / name
        with warnings.catch_warnings():
            # zipfile warns about the repeated member name that is under test here.
            warnings.simplefilter('ignore')
            with zipfile.ZipFile(path, 'w') as stream:
                for member, text in members:
                    stream.writestr(member, text)
        return path

    def test_repeated_member_name_is_refused_before_extracting(self):
        path = self.archive([('data/one.csv', 'sId\na\n'), ('data/one.csv', 'sId\nb\n')])
        destination = self.root / 'out'
        with self.assertRaisesRegex(ValueError, '重复或仅大小写不同'):
            safe_extract(path, destination)
        self.assertEqual(list(destination.iterdir()), [])

    def test_members_differing_only_by_case_are_refused(self):
        path = self.archive([('data/One.csv', 'sId\na\n'), ('data/one.csv', 'sId\nb\n')])
        destination = self.root / 'out'
        with self.assertRaisesRegex(ValueError, '重复或仅大小写不同'):
            safe_extract(path, destination)
        self.assertEqual(list(destination.iterdir()), [])

    def test_distinct_members_and_directory_entries_still_extract(self):
        path = self.archive([('data/', ''), ('data/one.csv', 'sId\na\n'), ('data/two.csv', 'sId\nb\n')])
        destination = safe_extract(path, self.root / 'out')
        self.assertEqual(sorted(p.name for p in destination.rglob('*.csv')), ['one.csv', 'two.csv'])


class CrossStaleConfiguration(unittest.TestCase):
    def test_prematch_staleness_cannot_be_left_empty_on_its_own(self):
        with self.assertRaisesRegex(ValueError, '赛前跨市场陈旧度不能单独留空'):
            check_config({'cross_stale_minutes': 5, 'cross_stale_minutes_prematch': None})

    def test_live_staleness_cannot_be_left_empty_on_its_own_either(self):
        # The mirror case is silently discarded downstream, so it must be refused here as well.
        with self.assertRaisesRegex(ValueError, '同时填写或同时留空'):
            check_config({'cross_stale_minutes': None, 'cross_stale_minutes_prematch': 1440})

    def test_default_configuration_keeps_both_staleness_windows(self):
        config = check_config({})
        self.assertEqual((config['cross_stale_minutes'], config['cross_stale_minutes_prematch']), (5, 1440))

    def test_blocking_cross_market_conditions_needs_both_left_empty(self):
        config = check_config({'cross_stale_minutes': None, 'cross_stale_minutes_prematch': None})
        self.assertIsNone(config['cross_stale_minutes'])
        self.assertIsNone(config['cross_stale_minutes_prematch'])


if __name__ == '__main__':
    unittest.main()
