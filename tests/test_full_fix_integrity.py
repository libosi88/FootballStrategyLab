"""Committed stop recovery from a real hot rollback journal, and loopback UI authorisation."""
import http.client,json,re,shutil,sqlite3,tempfile,threading,time,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from lab import server
from lab.common import atomic_json,canonical,read_json,sha
from lab.search_integrity import seal_search_evidence,verify_stopped_search

BINDING='full-fix-integrity'
DEFAULT_UI_PORT=8765

def checkpoint_body(status,binding=BINDING):
    return canonical({'version':1,'binding':binding,'status':status,'block':0,'anchor':0,'stack':None,'covered':1,'candidates':1,'nodes':1})

def build_search_dir(root):
    """A committed class-DFS stop whose public state is stale: the process died before publishing it."""
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(root/'search.sqlite3')) as conn:
        conn.execute('CREATE TABLE ledger(id INTEGER PRIMARY KEY,block INTEGER,anchor INTEGER,prefix TEXT,kind TEXT,weight TEXT,mask TEXT,n INTEGER,net INTEGER,upper INTEGER)')
        conn.execute('CREATE TABLE checkpoint(id INTEGER PRIMARY KEY,body TEXT)')
        conn.execute('INSERT INTO checkpoint VALUES(1,?)',(checkpoint_body('COMPLETE'),))
        # Several pages of committed ledger, so a killed transaction really spills dirty pages into the file.
        for _ in range(400):conn.execute("INSERT INTO ledger(block,anchor,prefix,kind,weight,mask,n,net,upper) VALUES(0,0,'[]','EVALUATED','1',?,2,200,200)",('m'*200,))
        conn.commit()
    atomic_json(root/'standard_plan.json',{'version':1,'binding':BINDING,'blocks':[],'total':1})
    # Stale public status: verify_stopped_search must fall through to the committed SQLite stop.
    atomic_json(root/'state.json',{'status':'RUNNING','binding':BINDING,'search_backend':'standard_class_dfs'})
    seal_search_evidence(root,'standard_class_dfs',BINDING)
    return root

def crash_copy(source,target):
    """Copy the directory while a transaction is open, so the copy is a crash image with a hot journal."""
    source=Path(source);target=Path(target)
    conn=sqlite3.connect(source/'search.sqlite3',isolation_level=None)
    try:
        conn.execute('PRAGMA cache_size=1')  # spill dirty pages into the database file, as a long run would
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('UPDATE checkpoint SET body=? WHERE id=1',(checkpoint_body('RUNNING'),))
        for _ in range(800):conn.execute("INSERT INTO ledger(block,anchor,prefix,kind,weight,mask,n,net,upper) VALUES(0,0,'[]','EVALUATED','1',?,2,-5,-5)",('u'*300,))
        shutil.copytree(source,target)
        conn.execute('ROLLBACK')
    finally:conn.close()
    return target

def committed_status(root):
    """Read the checkpoint the way the production code does: read-only, no CREATE and no writes."""
    uri=(Path(root)/'search.sqlite3').resolve().as_uri()
    with closing(sqlite3.connect(uri+'?mode=ro',uri=True)) as conn:
        return json.loads(conn.execute('SELECT body FROM checkpoint WHERE id=1').fetchone()[0])['status']

class HotJournalRecovery(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='fsl_journal_',ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.source=build_search_dir(Path(self.temp.name)/'search')
        self.clean_sha=sha(self.source/'search.sqlite3')
        self.crash=crash_copy(self.source,Path(self.temp.name)/'crash')
        self.journal=self.crash/'search.sqlite3-journal'

    def test_crash_image_really_carries_a_hot_journal_that_blocks_read_only_access(self):
        self.assertTrue(self.journal.is_file());self.assertGreater(self.journal.stat().st_size,0)
        with self.assertRaises(sqlite3.OperationalError):committed_status(self.crash)
        # The interrupted writer left the source directory clean, so it is still verifiable as sealed.
        self.assertFalse((self.source/'search.sqlite3-journal').exists())
        self.assertEqual(sha(self.source/'search.sqlite3'),self.clean_sha)
        self.assertEqual(verify_stopped_search(self.source,BINDING)['status'],'RUNNING')

    def test_hot_journal_is_rolled_back_and_the_committed_complete_stop_is_honoured(self):
        state=verify_stopped_search(self.crash,BINDING)
        self.assertFalse(self.journal.exists())
        self.assertEqual(committed_status(self.crash),'COMPLETE')
        # Rolling the journal back restores exactly the sealed bytes, so the evidence check can still pass.
        self.assertEqual(sha(self.crash/'search.sqlite3'),self.clean_sha)
        # The stale public state is returned unchanged; only the committed checkpoint decided the verdict.
        self.assertEqual(state['status'],'RUNNING')
        verify_stopped_search(self.crash,BINDING)

    def test_discarding_the_hot_journal_instead_would_expose_an_uncommitted_status(self):
        discarded=shutil.copytree(self.crash,Path(self.temp.name)/'discarded');(Path(discarded)/'search.sqlite3-journal').unlink()
        self.assertEqual(committed_status(discarded),'RUNNING')
        self.assertNotEqual(sha(Path(discarded)/'search.sqlite3'),self.clean_sha)
        verify_stopped_search(self.crash,BINDING)
        self.assertEqual(committed_status(self.crash),'COMPLETE')

    def test_sealed_evidence_is_still_checked_after_the_journal_is_recovered(self):
        atomic_json(self.crash/'standard_plan.json',{'version':1,'binding':BINDING,'blocks':[],'total':999})
        with self.assertRaisesRegex(ValueError,'standard_plan.json'):verify_stopped_search(self.crash,BINDING)
        # Recovery happened first: the evidence check ran against a rolled-back database, not a locked one.
        self.assertFalse(self.journal.exists());self.assertEqual(committed_status(self.crash),'COMPLETE')

    def test_recovered_checkpoint_binding_must_match_the_sealed_binding(self):
        with self.assertRaisesRegex(ValueError,'搜索证据绑定不一致'):verify_stopped_search(self.crash,'other-binding')
        self.assertFalse(self.journal.exists())

    def test_lost_public_state_is_refused_instead_of_recovered_from_the_journal(self):
        (self.crash/'state.json').unlink()
        with self.assertRaisesRegex(ValueError,'state.json'):verify_stopped_search(self.crash,BINDING)
        # A sealed run whose public state vanished is refused before the SQLite branch: the journal stays hot.
        self.assertTrue(self.journal.is_file())

    def test_missing_evidence_after_recovery_is_refused(self):
        (self.crash/'search_evidence.json').unlink()
        with self.assertRaisesRegex(ValueError,'完成搜索缺少证据封存清单'):verify_stopped_search(self.crash,BINDING)

    def test_unreadable_database_without_a_journal_is_not_swallowed_by_the_retry(self):
        broken=Path(shutil.copytree(self.crash,Path(self.temp.name)/'broken'))
        (broken/'search.sqlite3-journal').unlink();(broken/'search.sqlite3').write_bytes(b'not a database'*64)
        with self.assertRaises(sqlite3.DatabaseError):verify_stopped_search(broken,BINDING)

class LoopbackAuthorisation(unittest.TestCase):
    MUTATIONS=('/api/settings','/api/control','/api/create')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='fsl_auth_',ignore_cleanup_errors=True)
        self.root=Path(self.temp.name);self.errors=[];self.token=None
        def run():
            try:server.serve(self.root,port=0,open_browser=False)
            except BaseException as error:self.errors.append(error)
        # A throwaway workspace must never launch real worker processes.
        self.patch=patch('lab.server.launch_next',return_value=None);self.patch.start()
        self.thread=threading.Thread(target=run,daemon=True);self.thread.start()
        deadline=time.monotonic()+20
        while not (self.root/'ui_endpoint.json').exists():
            if self.errors:raise self.errors[0]
            if time.monotonic()>=deadline:raise AssertionError('本机界面未发布端口')
            time.sleep(.02)
        self.port=read_json(self.root/'ui_endpoint.json')['port'];self.base=f'http://127.0.0.1:{self.port}'
        with urlopen(self.base,timeout=10) as response:html=response.read().decode('utf-8')
        self.token=re.search(r'window\.LOCAL_TOKEN="([^"]+)"',html)[1]

    def tearDown(self):
        try:
            if self.token and self.thread.is_alive():self.call('/api/shutdown',{})
            self.thread.join(10)
            self.assertFalse(self.thread.is_alive(),'界面服务未停止')
            if self.errors:raise self.errors[0]
        finally:
            self.patch.stop();self.temp.cleanup()

    def call(self,route,body=None,token='valid',origin=None,method=None):
        """Request through urllib with the Host header urllib derives from the loopback URL."""
        headers={'Content-Type':'application/json'}
        if token=='valid':headers['X-Local-Token']=self.token
        elif token is not None:headers['X-Local-Token']=token
        if origin is not None:headers['Origin']=origin
        data=None if body is None else json.dumps(body).encode()
        request=Request(self.base+route,data=data,headers=headers,method=method)
        try:
            with urlopen(request,timeout=10) as response:return response.status,json.loads(response.read().decode('utf-8'))
        except HTTPError as error:
            with error:return error.code,json.loads(error.read().decode('utf-8'))
        except (ConnectionResetError,http.client.RemoteDisconnected) as error:
            # A refusal decided before the request body is read reaches the client as a reset on Windows,
            # especially under the parallel acceptance load. That is still a refusal, not an accepted call.
            return 'REFUSED',{'error':'连接在读取请求体前被关闭: '+type(error).__name__}

    def raw(self,route,host,method='POST',body=b'{}',token='valid',origin=None):
        """Request through http.client so an arbitrary Host header reaches the handler verbatim."""
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10)
        try:
            conn.putrequest(method,route,skip_host=True,skip_accept_encoding=True)
            conn.putheader('Host',host);conn.putheader('Content-Type','application/json')
            if token=='valid':conn.putheader('X-Local-Token',self.token)
            elif token is not None:conn.putheader('X-Local-Token',token)
            if origin is not None:conn.putheader('Origin',origin)
            conn.putheader('Content-Length',str(len(body)));conn.putheader('Connection','close');conn.endheaders()
            if body:conn.send(body)
            response=conn.getresponse();return response.status,response.read()
        except (ConnectionResetError,http.client.RemoteDisconnected) as error:
            return 'REFUSED',('refused before body: '+type(error).__name__).encode()
        finally:conn.close()

    def test_ephemeral_port_is_published_and_is_never_the_default_workbench_port(self):
        self.assertNotEqual(self.port,DEFAULT_UI_PORT);self.assertGreater(self.port,0)
        self.assertEqual(read_json(self.root/'ui_endpoint.json')['port'],self.port)

    def test_mutation_routes_refuse_missing_and_wrong_tokens_without_touching_the_workspace(self):
        before=read_json(self.root/'ui_preferences.json')
        for route in self.MUTATIONS:
            for token in (None,'wrong-token','',self.token[:-1]+('x' if self.token[-1]!='x' else 'y')):
                with self.subTest(route=route,token=token):
                    status,payload=self.call(route,{'max_parallel_jobs':2,'job':'x','action':'pause'},token=token)
                    self.assertIn(status,(403,'REFUSED'))
                    if status==403:self.assertIn('令牌',payload['error'])
        # A reset must mean "refused early", never "the service died": it still answers a valid request.
        self.assertEqual(self.call('/api/identity',method='GET')[0],200)
        self.assertEqual(read_json(self.root/'ui_preferences.json'),before)

    def test_foreign_host_header_is_refused_on_read_and_mutation_routes(self):
        for host in ('untrusted.example',f'untrusted.example:{self.port}','127.0.0.1',f'127.0.0.1:{self.port+1}','localhost',f'localhost:{DEFAULT_UI_PORT}'):
            for route,method in (('/api/settings','POST'),('/api/identity','GET'),('/','GET')):
                with self.subTest(host=host,route=route):
                    status,body=self.raw(route,host,method=method,body=b'{}' if method=='POST' else b'')
                    self.assertIn(status,(403,'REFUSED'))
                    if status==403:self.assertIn('非法Host',body.decode('utf-8'))

    def test_untrusted_origin_is_refused_even_with_the_correct_token(self):
        for origin in ('https://untrusted.example','http://127.0.0.1','http://localhost',f'http://evil.example:{self.port}',f'https://127.0.0.1:{self.port}','null'):
            with self.subTest(origin=origin):
                status,payload=self.call('/api/settings',{'max_parallel_jobs':2},origin=origin)
                self.assertIn(status,(403,'REFUSED'))
                if status==403:self.assertIn('跨站请求拒绝',payload['error'])
                status,payload=self.call('/api/identity',origin=origin)
                self.assertIn(status,(403,'REFUSED'))

    def test_correct_token_and_loopback_host_are_accepted_with_and_without_an_origin(self):
        for origin in (None,f'http://127.0.0.1:{self.port}',f'http://localhost:{self.port}'):
            with self.subTest(origin=origin):
                status,payload=self.call('/api/settings',{'max_parallel_jobs':2},origin=origin)
                self.assertEqual(status,200);self.assertTrue(payload['saved'])
        for route,body in (('/api/control',{'job':'missing','action':'pause'}),('/api/create',{'manifest_id':'missing'})):
            with self.subTest(route=route):
                status,payload=self.call(route,body)
                self.assertNotEqual(status,403);self.assertIn('error',payload)
        for host in (f'127.0.0.1:{self.port}',f'localhost:{self.port}'):
            with self.subTest(host=host):
                status,_=self.raw('/api/settings',host,body=json.dumps({'max_parallel_jobs':1}).encode())
                self.assertEqual(status,200)

if __name__=='__main__':unittest.main()
