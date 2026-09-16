"""Isolated scheduler, legacy migration and real JavaScript-renderer regressions."""
import copy
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from lab import server
from lab.common import ROOT, DEFAULT, VALIDATION_DEFAULT, canonical, sha
from lab.migration import import_audit_bundle
from lab.store import Store
from test_full_audit_ui import isolated_ui
from test_v031 import quote, write_csv

NODE=shutil.which('node')
RENDER_SCRIPT=r'''
const fs=require('fs'),vm=require('vm');
class Element {
 constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.className='';this._text='';this.value='';this.open=false;this.hidden=false;}
 append(...items){for(const item of items){this.children.push(item);item.parent=this;}}
 replaceChildren(...items){this.children=[];this.append(...items);}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 get options(){return this.children.filter(c=>c.tagName==='OPTION');}
 contains(node){return this===node||this.children.some(c=>c.contains(node));}
 querySelectorAll(){return [];}
}
class Option extends Element{constructor(label,value){super('option');this.textContent=label;this.value=value;}}
const els={};
for(const id of ['app-version','footer-version','workspace','count','jobs','jobselect','scheduler-health'])els[id]=new Element(id==='jobselect'?'select':'div');
const context={$:id=>els[id]||null,Option,lastJobs:[],states:{RUNNING:'实际计算中',PAUSING:'保存断点',PAUSED:'暂停',QUEUED:'排队'},document:{activeElement:null,createElement:tag=>new Element(tag)},text:(tag,value,cls)=>{const el=new Element(tag);el.textContent=value;el.className=cls||'';return el;},download:(id,path)=>'/download/'+id+'/'+path,displayValue:(key,value)=>String(value)};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const result=[];
for(const state of JSON.parse(fs.readFileSync(0,'utf8'))){
 context.renderJobs({version:'audit',workspace:'isolated',...state});
 const buttons=[];function walk(el){if(el.tagName==='BUTTON')buttons.push(el.textContent);for(const child of el.children)walk(child);}walk(els.jobs);
 result.push({buttons,health:els['scheduler-health'].textContent,health_hidden:els['scheduler-health'].hidden});
}
process.stdout.write(JSON.stringify(result));
'''


def render(states):
    run=subprocess.run([NODE,'-e',RENDER_SCRIPT,str(ROOT/'web/jobs.js')],input=json.dumps(states,ensure_ascii=False),
                       capture_output=True,text=True,encoding='utf-8',timeout=20)
    if run.returncode:raise AssertionError(run.stderr)
    return json.loads(run.stdout)


def archive_fixture(folder,config):
    folder.mkdir(parents=True)
    source=folder/'quotes.csv';write_csv(source,[quote('repair-synthetic-match')]);digest=sha(source)
    archive=folder/'audit.zip'
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('input_manifest.json',json.dumps({'files':[{'path':'Z:/unavailable/quotes.csv','sha256':digest,'kind':'quotes'}]}))
        z.writestr('config.json',json.dumps(config))
        z.writestr('data_audit.json',json.dumps({'league':'真实甲组','company':'皇冠'}))
        z.write(source,'original_inputs/'+digest[:12]+'_quotes.csv')
    return archive


class SchedulerRecoveryRepair(unittest.TestCase):
    def test_scheduler_survives_double_failure_and_reports_recovery(self):
        errors=[]
        with isolated_ui() as (workspace,request):
            original=Path.write_text
            def fail_log(path,*args,**kwargs):
                if path==workspace/'scheduler_error.log':raise OSError(errno.ENOSPC,'synthetic log failure')
                return original(path,*args,**kwargs)
            with patch.object(server,'launch_next',side_effect=OSError(errno.ENOSPC,'synthetic launch failure')),patch.object(Path,'write_text',new=fail_log),patch.object(threading,'excepthook',side_effect=lambda args:errors.append(str(args.exc_value))):
                deadline=time.monotonic()+5
                while True:
                    status,state=request('/api/state');health=state['scheduler']
                    if health['state']=='DEGRADED':break
                    if time.monotonic()>deadline:self.fail('scheduler did not report the injected failure')
                    time.sleep(.03)
                self.assertEqual(status,200);self.assertTrue(health['alive'])
                self.assertIn('synthetic launch failure',health['last_error'])
                self.assertIn('synthetic log failure',health['last_log_error'])
                self.assertGreaterEqual(health['consecutive_failures'],1)
                self.assertEqual(errors,[])
            deadline=time.monotonic()+5
            while True:
                _,state=request('/api/state');health=state['scheduler']
                if health['state']=='HEALTHY':break
                if time.monotonic()>deadline:self.fail('scheduler did not recover')
                time.sleep(.03)
            self.assertTrue(health['alive']);self.assertEqual(health['consecutive_failures'],0)
            self.assertIsNotNone(health['recovered_at']);self.assertIsNotNone(health['last_success_at'])


class LegacyBundleRepair(unittest.TestCase):
    def test_missing_mode_imports_as_paused_validation_in_api_and_cli(self):
        config=copy.deepcopy(VALIDATION_DEFAULT);config.pop('research_objective')
        config.update(created_version='0.5.0',engine_hash='legacy-engine')
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);archive=archive_fixture(base/'input',config);workspace=base/'api'
            first=import_audit_bundle(archive,workspace);again=import_audit_bundle(archive,workspace)
            self.assertEqual(first['job'],again['job']);self.assertEqual(first['status'],'PAUSED')
            job=Store(workspace).get(first['job'])
            self.assertEqual(job['config']['research_objective'],'validation')
            self.assertEqual(job['config']['holdout_months'],12)
            self.assertTrue(first['legacy_objective_defaulted']);self.assertFalse(first['automatic_research_started'])
            run=subprocess.run([sys.executable,'-B','-m','lab.cli','import-audit',str(archive),'--workspace',str(base/'cli')],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=30)
            self.assertEqual(run.returncode,0,run.stderr)
            result=json.loads(run.stdout);self.assertEqual(result['status'],'PAUSED')
            self.assertEqual(result['research_objective'],'validation')

    def test_explicit_historical_mode_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);archive=archive_fixture(base/'input',copy.deepcopy(DEFAULT))
            result=import_audit_bundle(archive,base/'workspace')
            job=Store(base/'workspace').get(result['job'])
            self.assertEqual(job['config']['research_objective'],'historical')
            self.assertEqual(job['config']['holdout_months'],0)
            self.assertFalse(result['legacy_objective_defaulted']);self.assertEqual(job['status'],'PAUSED')


@unittest.skipUnless(NODE,'Node is required only for isolated JavaScript UI checks')
class LegacyTaskUIRepair(unittest.TestCase):
    def test_old_running_task_has_pause_and_api_still_accepts_it(self):
        with isolated_ui() as (workspace,request):
            store=Store(workspace);jid=store.create('isolated-legacy',VALIDATION_DEFAULT,{'files':[]},True)
            config=store.get(jid)['config'];config.update(engine_hash='legacy-engine',created_version='0.5.0')
            with store.conn() as db:db.execute('UPDATE jobs SET config=? WHERE id=?',(canonical(config),jid))
            store.update(jid,status='RUNNING',pause=0,pid=os.getpid())
            job=server.job_view(store,store.get(jid))
            rendered=render([{'jobs':[job]},{'jobs':[{**job,'status':'PAUSED'}]},{'jobs':[{**job,'status':'QUEUED'}]}])
            self.assertIn('暂停',rendered[0]['buttons']);self.assertIn('暂停',rendered[2]['buttons'])
            self.assertNotIn('从断点继续',rendered[1]['buttons'])
            status,_=request('/api/control',{'job':jid,'action':'pause'})
            self.assertEqual(status,200);self.assertEqual(store.get(jid)['status'],'PAUSING')

    def test_health_notice_updates_even_when_job_list_has_not_changed(self):
        rendered=render([{'jobs':[],'scheduler':{'state':'DEGRADED','last_error':'launch failed','last_log_error':'disk full'}},
                         {'jobs':[],'scheduler':{'state':'HEALTHY'}},
                         {'jobs':[],'scheduler':{'state':'STOPPED'}}])
        self.assertIn('launch failed',rendered[0]['health']);self.assertFalse(rendered[0]['health_hidden'])
        self.assertTrue(rendered[1]['health_hidden']);self.assertEqual(rendered[1]['health'],'')
        self.assertIn('调度已停止',rendered[2]['health']);self.assertFalse(rendered[2]['health_hidden'])

    def test_jobs_javascript_passes_node_syntax_check(self):
        run=subprocess.run([NODE,'--check',str(ROOT/'web/jobs.js')],capture_output=True,text=True,timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)


if __name__=='__main__':unittest.main()
