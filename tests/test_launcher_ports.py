"""Startup must coexist with other local services and only reuse its own verified UI."""
import errno,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
from lab import launcher
from lab.common import atomic_json,VERSION
from lab.server import bind_ui_server,LoopbackHTTPServer
from http.server import BaseHTTPRequestHandler

class LauncherPorts(unittest.TestCase):
    def probe(self,saved,responses,occupied=False,open_browser=False):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            if saved is not None:atomic_json(root/'workspace/ui_endpoint.json',saved)
            def fetch(url,**kw):
                value=responses.get(int(url.split(':')[2].split('/')[0]))
                if value is None:raise OSError('unavailable')
                return io.BytesIO(json.dumps(value).encode())
            with patch.object(launcher,'ROOT',root),patch.object(launcher,'source_fingerprint',return_value='engine'), \
                 patch.object(launcher,'release_fingerprint',return_value='release'),patch.object(launcher,'workspace_identity',return_value='workspace'), \
                 patch.object(launcher,'urlopen',side_effect=fetch),patch.object(launcher.socket,'create_connection',side_effect=None if occupied else OSError('free')), \
                 patch.object(launcher.webbrowser,'open') as browser:
                result=launcher.existing_instance(open_browser)
                return result,[call.args[0] for call in browser.call_args_list]

    @staticmethod
    def identity(**kw):
        return {'application':'FootballStrategyLab','version':VERSION,'workspace_id':'workspace','engine_hash':'engine','release_hash':'release',**kw}

    def test_saved_ephemeral_port_is_reused_and_opened(self):
        self.assertEqual(self.probe({'port':32123},{32123:self.identity()},True,True),(0,['http://127.0.0.1:32123']))

    def test_foreign_workspace_or_changed_source_is_never_opened(self):
        for mismatch in ({'workspace_id':'foreign'},{'application':'other'}):
            with self.subTest(mismatch=mismatch):self.assertEqual(self.probe({'port':32123},{32123:self.identity(**mismatch)},True,True),(2,[]))
        # The same workspace on another build is reported instead of starting a second scheduler.
        for mismatch in ({'release_hash':'old'},{'engine_hash':'old'},{'version':'0.0.0'}):
            with self.subTest(mismatch=mismatch):self.assertEqual(self.probe({'port':32123},{32123:self.identity(**mismatch)},True,True),(3,[]))

    def test_stale_or_invalid_saved_port_falls_back_to_verified_default(self):
        for saved in ({'port':32123},{'port':True},{'port':70000},{'port':'http://elsewhere'},[]):
            with self.subTest(saved=saved):self.assertEqual(self.probe(saved,{8765:self.identity()}),(0,[]))
        self.assertEqual(self.probe({'port':32123},{}),(1,[]))

    def test_occupied_default_gets_a_new_loopback_port(self):
        server=MagicMock()
        with patch('lab.server.LoopbackHTTPServer',side_effect=[OSError(errno.EADDRINUSE,'occupied'),server]) as factory:
            self.assertIs(bind_ui_server(8765,object),server)
            self.assertEqual([call.args[0] for call in factory.call_args_list],[('127.0.0.1',8765),('127.0.0.1',0)])

    def test_explicit_ports_and_other_bind_errors_are_not_hidden(self):
        for port,error in ((8766,errno.EADDRINUSE),(8765,errno.EACCES)):
            with self.subTest(port=port,error=error),patch('lab.server.LoopbackHTTPServer',side_effect=OSError(error,'bind failed')) as factory:
                with self.assertRaises(OSError):bind_ui_server(port,object)
                self.assertEqual(factory.call_count,1)

    def test_two_live_servers_cannot_share_a_port_on_windows(self):
        with LoopbackHTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler) as first:
            with self.assertRaises(OSError):
                with LoopbackHTTPServer(first.server_address,BaseHTTPRequestHandler):pass

if __name__=='__main__':unittest.main()
