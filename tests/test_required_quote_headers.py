"""Quote input cannot silently infer openness or years from absent columns."""
import csv
import tempfile
import unittest
from pathlib import Path

from lab.catalog import scan_file
from lab.common import atomic_json, sha
from lab.data import inspect, load_inputs


# Exact header sequence observed in the user's real joined 23-column archive.
FIELDS = '日期,sId,联赛,主队,客队,开球时间,全场比分,半场比分,公司,盘口类型,阶段,比赛分钟,当时比分,盘口数值,盘口中文,上水/大球,下水/小球,变化时间,状态,封盘,比赛状态,让球val,大小val'.split(',')


def write_quote(path, omitted=(), closed=''):
    row = dict(zip(FIELDS, [
        '2024-03-01', 'one', '真实甲组', '主', '客', '2024-03-01 19:30',
        '="2-1"', '="1-0"', '皇冠', '让球', '滚球', '中场', '1-0', '-0.75',
        '受让半球/一球', '0.95', '0.85', '2024-03-01 20:20', '滚', closed,
        '完', '8', '9',
    ]))
    fields = [field for field in FIELDS if field not in omitted]
    with path.open('w', encoding='utf-8-sig', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerow({field: row[field] for field in fields})


def direct_manifest(path):
    return {'files': [{'path': str(path), 'sha256': sha(path),
                       'kind': 'quotes', 'joined_results': True}]}


class RequiredQuoteHeaders(unittest.TestCase):
    def test_catalog_rejects_each_missing_required_field_explicitly(self):
        for omitted in (('封盘',), ('日期',), ('封盘', '日期')):
            with self.subTest(omitted=omitted), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                path = root / '完整指数_真实甲组_2024.csv'
                write_quote(path, omitted)
                before = sha(path)
                with self.assertRaises(ValueError) as failure:
                    inspect([str(path)], root / 'work')
                message = str(failure.exception)
                self.assertIn(path.name, message)
                self.assertIn('完整指数CSV缺少必要列', message)
                for field in omitted:
                    self.assertIn(field, message)
                self.assertEqual(sha(path), before)

    def test_loader_rechecks_headers_when_supplied_a_quote_manifest_directly(self):
        for omitted in (('封盘',), ('日期',), ('封盘', '日期')):
            with self.subTest(omitted=omitted), tempfile.TemporaryDirectory() as td:
                path = Path(td) / 'quotes.csv'
                write_quote(path, omitted)
                before = sha(path)
                with self.assertRaises(ValueError) as failure:
                    load_inputs(direct_manifest(path), '真实甲组', '皇冠')
                self.assertIn('完整指数CSV缺少必要列', str(failure.exception))
                for field in omitted:
                    self.assertIn(field, str(failure.exception))
                self.assertEqual(sha(path), before)

    def test_old_catalog_cache_cannot_bypass_the_new_header_contract(self):
        for omitted in (('封盘',), ('日期',)):
            with self.subTest(omitted=omitted), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                path = root / 'quotes.csv'
                write_quote(path)
                old_info = scan_file(path, lambda **kwargs: None, lambda: False)
                write_quote(path, omitted)
                old_info['columns'] -= 1
                atomic_json(root / 'work' / 'catalog_cache' / (sha(path) + '.json'),
                            {'version': 'FSL_csv_catalog_v1', 'info': old_info})
                with self.assertRaisesRegex(ValueError, omitted[0]):
                    inspect([str(path)], root / 'work')

    def test_real_23_column_shape_preserves_open_closed_dates_and_results(self):
        self.assertEqual(len(FIELDS), 23)
        for closed in ('', '是'):
            with self.subTest(closed=closed), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                path = root / '完整指数_真实甲组_2024.csv'
                write_quote(path, closed=closed)
                before = sha(path)
                manifest = inspect([str(path)], root / 'work')
                events, labels, scale, audit = load_inputs(manifest, '真实甲组', '皇冠')
                self.assertEqual(manifest['scan']['quote_files'], 1)
                self.assertEqual(manifest['files'][0]['columns'], 23)
                self.assertEqual(manifest['leagues'][0]['years'], ['2024'])
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]['closed'], closed == '是')
                self.assertEqual(events[0]['valid'], closed != '是')
                self.assertEqual(events[0]['line'], -3)
                self.assertEqual(scale, 100)
                self.assertTrue(labels['one']['eligible'])
                self.assertEqual(labels['one']['date'], '2024-03-01')
                self.assertEqual(labels['one']['final'], [2, 1])
                self.assertEqual(audit['counts']['quote_rows'], 1)
                self.assertEqual(sha(path), before)

    def test_complete_quote_and_regular_result_index_still_classify_separately(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / 'quotes.csv'
            write_quote(path)
            index = root / 'index.csv'
            index.write_text('sId,联赛,全场比分,状态,日期\none,真实甲组,2-1,完,2024-03-01\n',
                             encoding='utf-8-sig')
            manifest = inspect([str(path), str(index)], root / 'work')
            self.assertEqual([record['kind'] for record in manifest['files']],
                             ['quotes', 'index'])


if __name__ == '__main__':
    unittest.main()
