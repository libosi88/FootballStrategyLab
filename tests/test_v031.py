import csv, io, json, tempfile, unittest
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch
import numpy as np
from lab import cli,common
from lab.common import DEFAULT, ROOT, atomic_json
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.data import inspect, load_inputs
from lab.store import Store
from lab.server import job_view
from lab.catalog import InspectionCancelled
from lab.mining import pack_masks, Paused
from lab.sparse_plan import prepare_sparse_plan
from lab.search_plan import make_plan_spec

FIELDS='日期,sId,联赛,主队,客队,开球时间,全场比分,半场比分,公司,盘口类型,阶段,比赛分钟,当时比分,盘口数值,盘口中文,上水/大球,下水/小球,变化时间,状态,封盘,比赛状态,让球val,大小val'.split(',')

def quote(sid='one',company='皇冠',league='真实甲组'):
    return dict(zip(FIELDS,['2024-03-01',sid,league,'主','客','2024-03-01 19:30','="2-1"','="1-0"',company,'让球','滚球','中场','1-0','-0.75','受让半球/一球','0.95','0.85','2024-03-01 20:20','滚','','完','8','9']))

def write_csv(path,rows,fields=FIELDS):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)

class CliConfiguration(unittest.TestCase):
    def captured(self,args,config):
        with patch.object(cli.sys,'argv',['app.py','run','demo','--league','L',*args]),patch.object(cli,'read_json',return_value=config),patch.object(cli,'inspect',return_value={'files':[]}),patch.object(cli,'Store') as store,patch('lab.pipeline.run',return_value=None),redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(),1);return store.return_value.create.call_args.args[1]
    def test_config_company_and_profile_preserved(self):
        c=self.captured(['--config','custom.json'],{'company':'平博','profile':'expanded'})
        self.assertEqual((c['company'],c['profile']),('平博','expanded'))
    def test_only_explicit_options_override_config(self):
        c=self.captured(['--config','custom.json','--profile','smoke'],{'company':'平博','profile':'expanded'})
        self.assertEqual((c['company'],c['profile']),('平博','smoke'))
    def test_absent_config_uses_application_defaults(self):
        c=self.captured([],None);self.assertEqual((c['company'],c['profile']),('皇冠','standard'))
    def test_missing_or_non_object_config_rejected(self):
        for c in (None,[],False):
            with self.subTest(c=c),self.assertRaises(ValueError):self.captured(['--config','bad.json'],c)

class JoinedArchive(unittest.TestCase):
    def test_actual_fields_split_file_aliases_and_companies(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);data=root/'selected';data.mkdir()
            write_csv(data/'完整指数_简称_2024.csv',[quote(),quote('two',league='真实乙组'),quote('three',company='平博')])
            write_csv(data/'_COPY_MANIFEST.csv',[{'country':'国家甲','league_short':'简称'}],['country','league_short'])
            man=inspect([str(data)],root/'work')
            self.assertEqual({(r['league'],r['company']) for r in man['leagues']},{('真实甲组','皇冠'),('真实甲组','平博'),('真实乙组','皇冠')})
            self.assertTrue(all(r['countries']==['国家甲'] for r in man['leagues']))
            self.assertEqual(len(man['files']),1);self.assertEqual(man['scan']['quote_rows'],3)
            ev,labels,scale,audit=load_inputs(man,'真实甲组','皇冠')
            self.assertEqual(len(ev),1);self.assertEqual(ev[0]['line'],-3);self.assertEqual(ev[0]['minute'],-2)
            self.assertEqual(scale,100);self.assertEqual(labels['one']['label_source'],'joined_index_columns')
            self.assertTrue(labels['one']['eligible']);self.assertEqual(labels['one']['final'],[2,1])
            self.assertFalse({'比赛状态','让球val','大小val','final'}&set(ev[0]))
    def test_source_quality_scope_policy_and_originals_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);data=root/'selected';data.mkdir()
            p=data/'完整指数_甲_2024.csv'
            write_csv(p,[quote('hard'),quote('isolated'),quote('note'),quote('hard',company='平博')])
            quality=root/'_build'/'handoff_current'/'exclude_sids.csv'
            write_csv(quality,[{'sId':sid,'reason':reason,'scope':'all_crown','source':'source'} for sid,reason in [('hard','pending_score_verify'),('isolated','quarantine_rows_isolated'),('note','remaining_suspicious')]],['sId','reason','scope','source'])
            before=p.read_bytes();man=inspect([str(data)],root/'work')
            ev,labels,_,audit=load_inputs(man,'真实甲组','皇冠')
            self.assertEqual(len(ev),3);self.assertFalse(labels['hard']['eligible']);self.assertFalse(labels['isolated']['eligible']);self.assertTrue(labels['note']['eligible'])
            self.assertEqual(audit['counts']['source_quality_excluded_matches'],2)
            _,pinbo,_,_=load_inputs(man,'真实甲组','平博');self.assertTrue(pinbo['hard']['eligible'])
            self.assertEqual(p.read_bytes(),before)
            # Explicit copies in a handoff still retain quality when their parent tree is absent.
            copied=root/'copied';copied.mkdir();(copied/'hash_exclude_sids.csv').write_bytes(quality.read_bytes());(copied/'quotes.csv').write_bytes(before)
            replay=inspect([str(copied)],root/'replay');self.assertEqual(sum(r['kind']=='quality' for r in replay['files']),1)
    def test_cache_reuse_and_changed_content_rescan(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);p=root/'q.csv';write_csv(p,[quote()]);inspect([str(p)],root/'work')
            with patch('lab.catalog.scan_file',side_effect=AssertionError('should reuse hash-bound scan')):
                cached=inspect([str(p)],root/'work');self.assertEqual(cached['scan']['cached_files'],1)
            write_csv(p,[quote(),quote('two')]);changed=inspect([str(p)],root/'work')
            self.assertEqual(changed['scan']['quote_rows'],2);self.assertEqual(changed['leagues'][0]['matches'],2)
    def test_cancel_and_bad_column_count_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);p=root/'q.csv';write_csv(p,[quote()])
            with self.assertRaises(InspectionCancelled):inspect([str(p)],root/'work',should_cancel=lambda:True)
            with p.open('a',encoding='utf-8') as f:f.write('broken,row\n')
            with self.assertRaisesRegex(ValueError,'列数不符'):inspect([str(p)],root/'work')
    def test_store_keeps_only_selected_league_files_and_quality(self):
        with tempfile.TemporaryDirectory() as td:
            st=Store(td);man={'files':[{'path':'a.csv','kind':'quotes','targets':[['A','皇冠']]},{'path':'b.csv','kind':'quotes','targets':[['B','皇冠']]},{'path':'exclude.csv','kind':'quality'}]}
            jid=st.create('A',DEFAULT,man)
            self.assertEqual([r['path'] for r in st.get(jid)['manifest']['files']],['a.csv','exclude.csv'])

class ResultStatus(unittest.TestCase):
    def test_legacy_done_partial_is_exposed_as_partial(self):
        with tempfile.TemporaryDirectory() as td:
            st=Store(td);jid=st.create('A',DEFAULT,{'files':[]});st.update(jid,status='DONE')
            summary={'selected':0,'backup_selected':0,'state':'PARTIAL_RESULT','v3_status':'PARTIAL_CORE','selection_status':'PARTIAL_SELECTION_BUDGET','coverage':{'candidates':1,'planned_expressions':100,'evaluated':16,'proven_zero':4,'remaining':80}}
            atomic_json(st.root/jid/'summary.json',summary);view=job_view(st,st.get(jid))
            self.assertEqual(view['status'],'PARTIAL_RESULT');self.assertEqual(view['summary']['coverage']['remaining'],80)
            summary['state']='FINITE_WORKFLOW_DONE';atomic_json(st.root/jid/'summary.json',summary)
            self.assertEqual(job_view(st,st.get(jid))['status'],'DONE')

class MappingLifetime(unittest.TestCase):
    def inputs(self):
        arrays={'eid':np.arange(3),'mid':np.arange(3),'pnl':np.ones(3,dtype=np.int64),'x':np.arange(3)}
        atoms=[{'feature':'x','op':'eq','value':i} for i in range(3)]
        ma,nz,off,_,_=pack_masks(arrays,atoms)
        return make_plan_spec(atoms,[0,1,2],[0,1,2],'routine'),ma,nz,off
    def test_closed_plan_releases_graph_and_rejects_further_use(self):
        with tempfile.TemporaryDirectory() as td:
            with prepare_sparse_plan(td,*self.inputs(),DEFAULT,lambda **kw:None,lambda:False) as plan:
                expected=list(plan.iter_from())
            with prepare_sparse_plan(td,*self.inputs(),DEFAULT,lambda **kw:None,lambda:False) as restored:self.assertEqual(list(restored.iter_from()),expected)
            p=Path(td)/'nohit_graph.npy';p.rename(p.with_suffix('.moved'));plan.close()
            with self.assertRaises(ValueError):list(plan.iter_from())
    def test_interrupted_graph_creation_releases_mapping(self):
        with tempfile.TemporaryDirectory() as td:
            def fail(**kw):raise Paused()
            with self.assertRaises(Paused):prepare_sparse_plan(td,*self.inputs(),DEFAULT,fail,lambda:False)
            p=Path(td)/'nohit_graph.tmp.npy';p.rename(p.with_suffix('.moved'))
            with prepare_sparse_plan(td,*self.inputs(),DEFAULT,lambda **kw:None,lambda:False) as plan:self.assertGreater(plan.total,0)

@unittest.skipUnless(common.os.name=='nt','Windows sharing semantics')
class AtomicReplacement(unittest.TestCase):
    def test_transient_reader_lock_retries_without_partial_json(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'state.json';atomic_json(p,{'old':True});replace=common.os.replace;calls=[]
            def sometimes(src,dst):
                calls.append(1)
                if len(calls)<3:raise PermissionError('reader holds destination')
                return replace(src,dst)
            with patch.object(common.os,'replace',side_effect=sometimes),patch.object(common.time,'sleep'):
                atomic_json(p,{'new':'完整中文状态'})
            self.assertEqual(json.loads(p.read_text(encoding='utf-8')),{'new':'完整中文状态'});self.assertEqual(len(calls),3)
            self.assertEqual(list(Path(td).glob('*.tmp')),[])
    def test_persistent_lock_keeps_previous_state_and_stops(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'state.json';atomic_json(p,{'old':True})
            with patch.object(common.os,'replace',side_effect=PermissionError('permanent denial')) as replace,patch.object(common.time,'sleep'),patch.object(common.time,'monotonic',side_effect=[0,0,5,10,15]),self.assertRaises(PermissionError):
                atomic_json(p,{'new':True})
            self.assertGreater(replace.call_count,1);self.assertLess(replace.call_count,10);self.assertEqual(json.loads(p.read_text(encoding='utf-8')),{'old':True})
            self.assertEqual(list(Path(td).glob('*.tmp')),[])

if __name__=='__main__':unittest.main()
