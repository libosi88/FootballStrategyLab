"""Real archive I/O with subprocess validation isolated by the existing fixture."""
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from lab import packaging, release
import test_packaging_publication as fixtures


class SameSourcePublicationRepair(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.PackagingPublication('test_both_archives_and_certificate_are_published_before_root_completion')
        self.case.setUp();self.addCleanup(self.case.doCleanups)

    def test_both_archives_contain_exactly_the_same_approved_source(self):
        source=packaging.ROOT
        (source/'config').mkdir()
        (source/'config/default_config.json').write_text('{}',encoding='utf-8')
        (source/'config/private_audit_marker.env').write_text('HARMLESS_TEST_MARKER=1',encoding='utf-8')
        (source/'docs/private.pem').write_text('NOT_A_REAL_KEY',encoding='utf-8')
        expected={p.relative_to(source).as_posix() for p in release.release_files(source)}
        packages,state=self.case.run_package()
        self.assertEqual(state['state'],'STANDARD_HANDOFF_COMPLETE')
        with zipfile.ZipFile(self.case.root/'packages'/packages['developer']) as a,zipfile.ZipFile(self.case.root/'packages'/packages['audit']) as b:
            declared=json.loads(a.read('source_release_manifest.json'))
            self.assertEqual(json.loads(b.read('source_release_manifest.json')),declared)
            self.assertEqual(set(declared['files']),expected)
            self.assertEqual({n[len('software/'):] for n in b.namelist() if n.startswith('software/')},expected)
            for name,digest in declared['files'].items():
                self.assertEqual(a.read(name),b.read('software/'+name))
                self.assertEqual(hashlib.sha256(a.read(name)).hexdigest(),digest)
            for name in ('config/private_audit_marker.env','docs/private.pem'):
                self.assertNotIn(name,a.namelist());self.assertNotIn('software/'+name,b.namelist())
        certificate=json.loads((self.case.root/'packages'/packages['developer']).parent.joinpath('交接验收证书.json').read_text(encoding='utf-8'))
        self.assertEqual(certificate['source_release_hash'],declared['release_hash'])
        self.assertTrue(certificate['same_whitelisted_source_snapshot'])

    def test_document_drift_aborts_without_publishing_completion(self):
        changed=False
        def check(command,**kwargs):
            nonlocal changed
            result=self.case.fake_check(command,**kwargs)
            if not changed:
                (packaging.ROOT/'docs/fixture.txt').write_text('changed during build',encoding='utf-8');changed=True
            return result
        with patch.object(packaging.subprocess,'run',side_effect=check):
            with self.assertRaisesRegex(ValueError,'发布源码集合'):self.case.run_package()
        self.case.assert_unpublished()

    def test_added_approved_file_also_invalidates_publication(self):
        def check(command,**kwargs):
            result=self.case.fake_check(command,**kwargs)
            (packaging.ROOT/'docs/added.md').write_text('new source file',encoding='utf-8')
            return result
        with patch.object(packaging.subprocess,'run',side_effect=check):
            with self.assertRaisesRegex(ValueError,'发布源码集合'):self.case.run_package()
        self.case.assert_unpublished()

    def test_removed_approved_file_also_invalidates_publication(self):
        def check(command,**kwargs):
            result=self.case.fake_check(command,**kwargs)
            (packaging.ROOT/'docs/fixture.txt').unlink(missing_ok=True)
            return result
        with patch.object(packaging.subprocess,'run',side_effect=check):
            with self.assertRaisesRegex(ValueError,'发布源码集合'):self.case.run_package()
        self.case.assert_unpublished()


class ReleaseSnapshotRepair(unittest.TestCase):
    def test_snapshot_is_stable_after_original_is_edited(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'source';(root/'docs').mkdir(parents=True)
            original=root/'docs/a.md';original.write_text('before',encoding='utf-8')
            frozen=Path(td)/'frozen';manifest=release.freeze_release_tree(frozen,root)
            original.write_text('after',encoding='utf-8')
            self.assertEqual((frozen/'docs/a.md').read_text(encoding='utf-8'),'before')
            self.assertEqual(release.release_fingerprint(frozen),release.manifest_fingerprint(manifest))
            self.assertNotEqual(release.release_fingerprint(root),release.manifest_fingerprint(manifest))

    def test_snapshot_refuses_overwrite_or_source_directory_target(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'source';root.mkdir();frozen=Path(td)/'frozen';frozen.mkdir()
            with self.assertRaises(FileExistsError):release.freeze_release_tree(frozen,root)
            with self.assertRaises(ValueError):release.freeze_release_tree(root/'docs/new',root)

    def test_change_during_copy_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'source';(root/'docs').mkdir(parents=True)
            original=root/'docs/a.md';original.write_text('before',encoding='utf-8')
            real_copy=release.shutil.copy2
            def copy(src,dst,*args,**kwargs):
                Path(src).write_text('changed during copy',encoding='utf-8')
                return real_copy(src,dst,*args,**kwargs)
            with patch.object(release.shutil,'copy2',side_effect=copy):
                with self.assertRaisesRegex(ValueError,'冻结期间'):release.freeze_release_tree(Path(td)/'frozen',root)


if __name__=='__main__':unittest.main()
