"""Isolated UI, HTTP settings, preview and startup audit regressions."""
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import gzip
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from lab import launcher, server
from lab.common import DEFAULT, ROOT, VERSION, read_json
from lab.store import Store


@contextmanager
def isolated_ui():
    """Start only a disposable HTTP workspace; its scheduler cannot launch jobs."""
    with tempfile.TemporaryDirectory(prefix='fsl_ui_audit_') as td:
        root = Path(td)
        errors = []
        def run():
            try:
                server.serve(root, port=0, open_browser=False)
            except BaseException as error:
                errors.append(error)
        with patch('lab.server.launch_next', return_value=None):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            url = token = None
            try:
                deadline = time.monotonic() + 8
                while not (root / 'ui_endpoint.json').exists():
                    if errors:
                        raise errors[0]
                    if time.monotonic() >= deadline:
                        raise AssertionError('isolated UI did not publish its endpoint')
                    time.sleep(.02)
                port = read_json(root / 'ui_endpoint.json')['port']
                url = f'http://127.0.0.1:{port}'
                with urlopen(url, timeout=5) as response:
                    html = response.read().decode('utf-8')
                token = re.search(r'window.LOCAL_TOKEN="([^"]+)"', html)[1]
                def request(route, body=None):
                    data = None if body is None else json.dumps(body).encode()
                    req = Request(url + route, data=data, headers={
                        'X-Local-Token': token, 'Content-Type': 'application/json'})
                    try:
                        response = urlopen(req, timeout=5)
                    except HTTPError as error:
                        response = error
                    with response:
                        return response.status, json.load(response)
                yield root, request
            finally:
                if token is not None and thread.is_alive():
                    request('/api/shutdown', {})
                thread.join(5)
                if thread.is_alive():
                    raise AssertionError('isolated UI did not stop')
                if errors:
                    raise errors[0]


class FullAuditUI(unittest.TestCase):
    def test_parallel_setting_rejects_fraction_and_boolean(self):
        with isolated_ui() as (root, request):
            for value in (2.5, True):
                with self.subTest(value=value):
                    status, _ = request('/api/settings', {'max_parallel_jobs': value})
                    self.assertEqual(status, 400)
                    self.assertEqual(Store(root).parallel_limit(), 1)

    def test_invalid_research_settings_do_not_change_parallel_limit(self):
        with isolated_ui() as (root, request):
            before = read_json(root / 'ui_preferences.json')
            status, _ = request('/api/settings', {'max_parallel_jobs': '2', 'min_profit': '-1'})
            self.assertEqual(status, 400)
            self.assertEqual(Store(root).parallel_limit(), 1)
            self.assertEqual(read_json(root / 'ui_preferences.json'), before)

    def test_valid_parallel_and_research_settings_persist_together(self):
        with isolated_ui() as (root, request):
            status, _ = request('/api/settings', {'max_parallel_jobs': '2', 'min_profit': '12'})
            self.assertEqual(status, 200)
            self.assertEqual(Store(root).parallel_limit(), 2)
            self.assertEqual(read_json(root / 'ui_preferences.json')['research_settings']['min_profit'], '12')

    def test_preview_csv_blank_records_preserve_rows_and_paging(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'rows.csv'
            path.write_bytes('name,note\none,"first\nline"\n\ntwo,last\n\nthree,finish\n'.encode('utf-8'))
            rows, more = server.preview_page(path, 0, 2)
            self.assertEqual(rows, [{'name': 'one', 'note': 'first\nline'}, {'name': 'two', 'note': 'last'}])
            self.assertTrue(more)
            self.assertEqual(server.preview_page(path, 2, 2), ([{'name': 'three', 'note': 'finish'}], False))

    def test_preview_jsonl_blank_records_preserve_rows_and_paging(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'rows.jsonl.gz'
            with gzip.open(path, 'wt', encoding='utf-8') as stream:
                stream.write('{"n":1}\n\n{"n":2}\n\n{"n":3}\n')
            self.assertEqual(server.preview_page(path, 0, 2), ([{'n': 1}, {'n': 2}], True))
            self.assertEqual(server.preview_page(path, 2, 2), ([{'n': 3}], False))

    def test_verified_instance_remains_reusable_if_browser_open_fails(self):
        identity = {'application': 'FootballStrategyLab', 'version': VERSION,
                    'workspace_id': 'isolated', 'engine_hash': 'engine', 'release_hash': 'release'}
        with tempfile.TemporaryDirectory() as td, \
                patch.object(launcher, 'ROOT', Path(td)), \
                patch.object(launcher, 'workspace_identity', return_value='isolated'), \
                patch.object(launcher, 'source_fingerprint', return_value='engine'), \
                patch.object(launcher, 'release_fingerprint', return_value='release'), \
                patch.object(launcher, 'urlopen', side_effect=lambda *a, **k: BytesIO(json.dumps(identity).encode())), \
                patch.object(launcher.webbrowser, 'open', side_effect=OSError('no browser')), \
                patch.object(launcher.socket, 'create_connection') as connect:
            self.assertEqual(launcher.existing_instance(open_browser=True), 0)
            connect.assert_not_called()

    def test_stopped_scheduler_does_not_launch_a_queued_worker(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(td)
            jid = store.create('isolated UI test', DEFAULT, {'files': []})
            stop = threading.Event()
            stop.set()
            with patch('lab.server.source_fingerprint', return_value=store.get(jid)['config']['engine_hash']), \
                    patch('lab.server.shutil.disk_usage', return_value=SimpleNamespace(free=20 * 1024**3)), \
                    patch('lab.server.subprocess.Popen', return_value=SimpleNamespace(poll=lambda: 0)) as popen:
                self.assertIsNone(server.launch_next(store, stop))
                popen.assert_not_called()
            self.assertEqual(store.get(jid)['status'], 'QUEUED')

    def test_endpoint_publication_failure_closes_bound_socket(self):
        fake = SimpleNamespace(server_port=12345, serve_forever=Mock(), server_close=Mock())
        with tempfile.TemporaryDirectory() as td, \
                patch('lab.server.bind_ui_server', return_value=fake), \
                patch('lab.server.atomic_json', side_effect=OSError('endpoint disk failure')):
            with self.assertRaisesRegex(OSError, 'endpoint disk failure'):
                server.serve(td, port=0, open_browser=False)
            fake.server_close.assert_called_once()

    def test_browser_open_failure_still_serves_and_closes_socket(self):
        fake = SimpleNamespace(server_port=12345, serve_forever=Mock(), server_close=Mock())
        with tempfile.TemporaryDirectory() as td, \
                patch('lab.server.bind_ui_server', return_value=fake), \
                patch('lab.server.threading.Thread'), \
                patch('lab.server.webbrowser.open', side_effect=OSError('no browser')):
            server.serve(td, port=0, open_browser=True)
            fake.serve_forever.assert_called_once()
            fake.server_close.assert_called_once()

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for the JavaScript rendering regression')
    def test_empty_historical_roster_is_information_while_real_failed_gates_stay_red(self):
        script = r'''
const fs=require('fs'),vm=require('vm');
class Element {
 constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.className='';this._text='';this.value='';this.open=false;}
 append(...items){for(const item of items){this.children.push(item);item.parent=this;}}
 replaceChildren(...items){this.children=[];this.append(...items);}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 get options(){return this.children.filter(c=>c.tagName==='OPTION');}
 contains(node){return this===node||this.children.some(c=>c.contains(node));}
 querySelectorAll(){return [];}
}
class Option extends Element {constructor(label,value){super('option');this.textContent=label;this.value=value;}}
const els={};for(const id of ['app-version','footer-version','workspace','count','jobs','jobselect'])els[id]=new Element(id==='jobselect'?'select':'div');
const context={$:id=>els[id]||null,Option,lastJobs:[],states:{DONE:'完成',PARTIAL_RESULT:'未完成'},
 document:{activeElement:null,createElement:tag=>new Element(tag)},
 text:(tag,value,cls)=>{const el=new Element(tag);el.textContent=value;el.className=cls||'';return el;},
 download:(id,path)=>'/download/'+id+'/'+path,displayValue:(key,value)=>String(value)};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
function run(objective,state,gates){
 const summary={state,selected:0,backup_selected:0,coverage:{candidates:0},gates,empty_roster_chain_verified:true};
 const job={id:'isolated',league:'合成界面测试',company:'皇冠',profile:'standard',research_objective:objective,
  status:state==='PARTIAL_RESULT'?'PARTIAL_RESULT':'DONE',previous_version:false,created_version:'test',summary};
 context.renderJobs({jobs:[job],version:'test',workspace:'isolated'});
 const errors=[];function walk(el){if(el.className==='error')errors.push(el.textContent);for(const child of el.children)walk(child);}walk(els.jobs);
 return {errors,text:els.jobs.textContent};
}
console.log(JSON.stringify([run('historical','HISTORICAL_RESEARCH_COMPLETE',{nonempty_roster:false,trigger:true}),
 run('historical','HISTORICAL_RESEARCH_COMPLETE_NO_HANDOFF',{nonempty_roster:false,trigger:true}),
 run('historical','PARTIAL_RESULT',{nonempty_roster:false,trigger:false}),
 run('validation','PARTIAL_RESULT',{nonempty_roster:false,trigger:true})]));
'''
        result = subprocess.run([shutil.which('node'), '-e', script, str(ROOT / 'web/jobs.js')],
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        historical, no_handoff, partial, validation = json.loads(result.stdout)
        self.assertEqual(historical['errors'], [])
        self.assertEqual(no_handoff['errors'], [])
        self.assertIn('触发核对', ''.join(partial['errors']))
        self.assertNotIn('非空主名单', ''.join(partial['errors']))
        self.assertIn('非空主名单', ''.join(validation['errors']))
        self.assertNotIn('不能认证非空交接链路', historical['text'])


if __name__ == '__main__':
    unittest.main()
