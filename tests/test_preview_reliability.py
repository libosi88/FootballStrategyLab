import io,json,sys,tempfile,unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from lab import server
from lab.common import DEFAULT,VALIDATION_DEFAULT,atomic_json,csv_write,sha

class PreviewReliability(unittest.TestCase):
    def setUp(self):
        with server.PAGE_INDEX_LOCK:server.PAGE_INDEX.clear()
        with server.JSON_CACHE_LOCK:server.JSON_CACHE.clear()

    def test_concurrent_eviction_preserves_each_file_and_page(self):
        with tempfile.TemporaryDirectory() as td:
            paths=[Path(td)/f'{i}.csv' for i in range(32)]
            for i,p in enumerate(paths):csv_write(p,[{'file':i,'row':j} for j in range(40)])
            def read(i):
                file=i%32;offset=(i//32)%4*10
                rows,more=server.preview_page(paths[file],offset,10)
                self.assertEqual([(int(r['file']),int(r['row'])) for r in rows],[(file,j) for j in range(offset,offset+10)])
                self.assertEqual(more,offset<30)
            interval=sys.getswitchinterval()
            try:
                sys.setswitchinterval(.000001)
                with ThreadPoolExecutor(max_workers=16) as pool:list(pool.map(read,range(256)))
            finally:sys.setswitchinterval(interval)
            with server.PAGE_INDEX_LOCK:self.assertLessEqual(len(server.PAGE_INDEX),16)

    def test_replaced_file_invalidates_index(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'data.csv';csv_write(p,[{'value':'old'}])
            self.assertEqual(server.preview_page(p,0)[0],[{'value':'old'}])
            replacement=Path(td)/'new.csv';csv_write(replacement,[{'value':'new longer value'},{'value':'second'}]);replacement.replace(p)
            self.assertEqual(server.preview_page(p,1)[0],[{'value':'second'}])
            with server.PAGE_INDEX_LOCK:self.assertEqual(sum(k[0]==str(p) for k in server.PAGE_INDEX),1)

    def test_unclosed_header_or_cell_fails_instead_of_spinning_at_eof(self):
        for data in ('"header\n','a,b\n1,"unfinished\n'):
            with self.subTest(data=data),tempfile.TemporaryDirectory() as td:
                p=Path(td)/'broken.csv';p.write_text(data,encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'引号未闭合'):server.preview_page(p,0)

    def test_large_utf8_preview_is_bounded_without_modifying_full_file(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'report.json';content='{"text":"'+('汉字🙂'*1000)+'"}'
            p.write_text(content,encoding='utf-8-sig');before=sha(p)
            preview=server.preview_text(p,limit=32)
            self.assertTrue(preview['text_truncated']);self.assertTrue(content.startswith(preview['text']))
            self.assertNotIn('\ufffd',preview['text']);self.assertLessEqual(len(preview['text'].encode('utf-8')),32)
            self.assertEqual(preview['file_size_bytes'],p.stat().st_size);self.assertEqual(sha(p),before)

    def test_small_text_keeps_exact_content_and_detects_missing_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'report.md';p.write_text('完整内容🙂',encoding='utf-8')
            result=server.preview_text(p,limit=p.stat().st_size)
            self.assertEqual(result['text'],'完整内容🙂');self.assertFalse(result['text_truncated'])

    def test_large_json_is_not_loaded_even_if_previously_cached(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'report.json';atomic_json(p,{'text':'a'*100})
            self.assertEqual(server.cached_json(p)['text'],'a'*100)
            with patch.object(Path,'open',side_effect=AssertionError('oversized file was opened')):
                self.assertIsNone(server.cached_json(p,None,max_bytes=32))
            self.assertEqual(server.cached_json(p,{},max_bytes=1024)['text'],'a'*100)

    def test_portfolio_preview_uses_historical_decision_scenario(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'组合政策对照.csv';csv_write(p,[
                {'名单':'默认','整场上限':4,'情景':0,'政策':'整场最多4单位','net':12},
                {'名单':'默认','整场上限':4,'情景':4,'政策':'整场最多4单位','net':7},
            ])
            self.assertEqual(server.preview_portfolio_row(p,'main',DEFAULT)['net'],'7')
            self.assertEqual(server.preview_portfolio_row(p,'main',VALIDATION_DEFAULT)['net'],'12')

    def test_json_cache_concurrent_replacement_returns_valid_objects(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'state.json';atomic_json(p,{'value':0})
            def read(_):self.assertIn(server.cached_json(p)['value'],range(16))
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures=[]
                for i in range(16):
                    atomic_json(p,{'value':i});futures.extend(pool.submit(read,i) for _ in range(8))
                for future in futures:future.result()
            self.assertEqual(server.cached_json(p),{'value':15})

if __name__=='__main__':unittest.main()
