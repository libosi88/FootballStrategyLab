"""Publication accepts JSON-serializable tuple state but rejects changed bytes."""
from pathlib import Path
from unittest.mock import patch
import json,unittest,zipfile
import test_packaging_publication as publication
from lab import packaging
from lab.common import atomic_json,canonical,read_json


class PackagingPublicationTuple(unittest.TestCase):
    # Reuse only the existing fixture, without rerunning its inherited test methods.
    fake_check=publication.PackagingPublication.fake_check
    run_package=publication.PackagingPublication.run_package
    assert_unpublished=publication.PackagingPublication.assert_unpublished

    def setUp(self):
        publication.PackagingPublication.setUp(self)
        self.coverage.update(standard_search_complete=False,local_search_complete=False,remaining=7,
            directions={'LIVE_OVER':{'status':'BUDGET_STOP','stack':[(0,(1,2),[('暂停',3)])]}})
        self.summary['selected']=1
        atomic_json(self.root/'results/rules.json',{'rules':[{'id':'fixture-nonempty','direction':'LIVE_OVER',
            'conditions':[{'feature':'water','op':'ge','value':90}]}]})

    def test_nonempty_partial_package_accepts_tuple_search_stack(self):
        packages,state=self.run_package()
        self.assertEqual(state['state'],'PARTIAL_RESULT')
        self.assertTrue(state['gates']['nonempty_roster'])
        self.assertEqual(self.trigger['rules'],1)
        with zipfile.ZipFile(self.root/'packages'/packages['developer']) as archive:
            self.assertEqual(len(json.loads(archive.read('results/rules.json'))['rules']),1)
        with zipfile.ZipFile(self.root/'packages'/packages['audit']) as archive:
            encoded=archive.read('summary.json');decoded=json.loads(encoded)
            self.assertEqual(encoded,canonical(decoded).encode('utf-8'))
            self.assertEqual(canonical(decoded['coverage']),canonical(self.coverage))
            self.assertEqual(decoded['coverage']['directions']['LIVE_OVER']['stack'],[[0,[1,2],[['暂停',3]]]])
        self.assertEqual(read_json(self.root/'summary.json')['selected'],1)

    def test_changed_valid_B_metadata_is_rejected_before_publication(self):
        original=zipfile.ZipFile.writestr;changed=[]
        def alter_summary(archive,name,data,*args,**kwargs):
            if Path(archive.filename).name.startswith('B_') and name=='summary.json':
                value=json.loads(data);value['coverage']['remaining']+=1
                data=canonical(value);changed.append(name)
            return original(archive,name,data,*args,**kwargs)
        # Mutation happens before compression, so the ZIP has valid CRCs. The
        # exact expected metadata, rather than CRC failure, must reject it.
        with patch.object(packaging.zipfile.ZipFile,'writestr',alter_summary):
            with self.assertRaisesRegex(ValueError,'元数据校验失败: summary.json'):self.run_package()
        self.assertEqual(changed,['summary.json'])
        self.assert_unpublished()


if __name__=='__main__':unittest.main()
