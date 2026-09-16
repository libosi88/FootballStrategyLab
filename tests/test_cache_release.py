import unittest,tempfile,json
from pathlib import Path
from lab.common import DEFAULT,check_config,read_json
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.standard_mining import release_direction_caches,DERIVED_CACHES,EVIDENCE_NEVER_RELEASED
from lab.standard_atoms import verify_frozen_dictionary


def make_direction(root):
    """A finished direction: rebuildable caches plus the frozen evidence beside them."""
    root=Path(root)
    (root/'masks.sqlite3').write_bytes(b'M'*4096)
    (root/'masks.sqlite3-journal').write_bytes(b'J'*128)
    for name in ('virtual_columns','review_columns'):
        d=root/name;d.mkdir()
        (d/'0.npy').write_bytes(b'C'*2048)
        (d/'1.npy').write_bytes(b'C'*1024)
    for name in EVIDENCE_NEVER_RELEASED:
        (root/name).write_bytes(b'E'*64)
    return root


class CacheRelease(unittest.TestCase):
    def test_finished_direction_releases_only_rebuildable_caches(self):
        with tempfile.TemporaryDirectory() as td:
            root=make_direction(td)
            record=release_direction_caches(root,{'status':'COMPLETE'},DEFAULT)
            for name in DERIVED_CACHES:
                self.assertFalse((root/name).exists(),name+' 应被释放')
            self.assertFalse((root/'masks.sqlite3-journal').exists(),'sqlite 附属文件应一并释放')
            for name in EVIDENCE_NEVER_RELEASED:
                self.assertTrue((root/name).exists(),name+' 是冻结证据，不得删除')
            self.assertEqual(record['freed_bytes'],4096+128+2*(2048+1024))
            saved=read_json(root/'cache_release.json')
            self.assertEqual(saved['direction_status'],'COMPLETE')
            self.assertTrue(saved['rebuild'])

    def test_unfinished_direction_keeps_everything_for_resume(self):
        for status in ('RUNNING','PAUSED','BUDGET_STOP','RESOURCE_BLOCKED','INTERRUPTED',None):
            with tempfile.TemporaryDirectory() as td:
                root=make_direction(td)
                record=release_direction_caches(root,{'status':status},DEFAULT)
                self.assertEqual(record['freed_bytes'],0)
                for name in DERIVED_CACHES:
                    self.assertTrue((root/name).exists(),f'{status} 状态下 {name} 必须保留以便续跑')
                self.assertFalse((root/'cache_release.json').exists())

    def test_no_data_direction_is_finished(self):
        with tempfile.TemporaryDirectory() as td:
            root=make_direction(td)
            self.assertGreater(release_direction_caches(root,{'status':'NO_DATA'},DEFAULT)['freed_bytes'],0)

    def test_retention_switch_is_honoured_and_validated(self):
        with tempfile.TemporaryDirectory() as td:
            root=make_direction(td)
            record=release_direction_caches(root,{'status':'COMPLETE'},{**DEFAULT,'release_direction_caches':False})
            self.assertEqual(record['freed_bytes'],0)
            self.assertTrue((root/'masks.sqlite3').exists())
        self.assertIs(check_config({})['release_direction_caches'],True)
        with self.assertRaises(ValueError):check_config({'release_direction_caches':'yes'})

    def test_releasing_frozen_evidence_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root=make_direction(td)
            for name in ('atoms.sqlite3','search.sqlite3','arrays.npz','state.json'):
                with self.assertRaises(ValueError):
                    release_direction_caches(root,{'status':'COMPLETE'},DEFAULT,None,(name,))
                self.assertTrue((root/name).exists())

    def test_partial_names_never_match_evidence_by_prefix(self):
        """masks.sqlite3* must not reach a same-prefixed evidence file."""
        with tempfile.TemporaryDirectory() as td:
            root=make_direction(td)
            (root/'virtual_columns_summary.json').write_bytes(b'K'*32)
            release_direction_caches(root,{'status':'COMPLETE'},DEFAULT,None,('masks.sqlite3',))
            self.assertTrue((root/'virtual_columns').exists())
            self.assertTrue((root/'virtual_columns_summary.json').exists())

    def test_second_release_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root=make_direction(td)
            first=release_direction_caches(root,{'status':'COMPLETE'},DEFAULT)
            second=release_direction_caches(root,{'status':'COMPLETE'},DEFAULT)
            self.assertGreater(first['freed_bytes'],0)
            self.assertEqual(second['freed_bytes'],0)


class FrozenDictionaryGate(unittest.TestCase):
    """Every reuse shortcut must pass the same integrity gate as a rebuild."""

    def commit(self,root,body=b'D'*512):
        root=Path(root);(root/'atoms.sqlite3').write_bytes(body)
        from lab.common import sha
        (root/'dictionary_complete.json').write_text(json.dumps(
            {'spec_version':'x','database_sha256':sha(root/'atoms.sqlite3')}),encoding='utf-8')
        return root

    def test_returns_none_before_anything_is_committed(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(verify_frozen_dictionary(td))

    def test_accepts_an_intact_commit(self):
        with tempfile.TemporaryDirectory() as td:
            root=self.commit(td)
            self.assertEqual(verify_frozen_dictionary(root)['spec_version'],'x')

    def test_rejects_tampered_appended_or_truncated_database(self):
        for mutate in (lambda b:b+b'tamper',lambda b:b[:-16],lambda b:b.replace(b'D',b'E',1)):
            with tempfile.TemporaryDirectory() as td:
                root=self.commit(td);path=root/'atoms.sqlite3'
                path.write_bytes(mutate(path.read_bytes()))
                with self.assertRaisesRegex(ValueError,'完整性'):verify_frozen_dictionary(root)

    def test_rejects_missing_database_or_missing_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root=self.commit(td);(root/'atoms.sqlite3').unlink()
            with self.assertRaisesRegex(ValueError,'完整性'):verify_frozen_dictionary(root)
        with tempfile.TemporaryDirectory() as td:
            root=self.commit(td)
            (root/'dictionary_complete.json').write_text(json.dumps({'spec_version':'x'}),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'完整性'):verify_frozen_dictionary(root)

    def test_build_and_reuse_share_one_gate(self):
        import inspect
        from lab import standard_atoms,standard_mining
        for module in (standard_atoms.build_standard_catalog,standard_mining.mine_standard):
            self.assertIn('verify_frozen_dictionary',inspect.getsource(module),
                          module.__name__+' 必须经由同一个完整性闸门')


if __name__=='__main__':unittest.main()
