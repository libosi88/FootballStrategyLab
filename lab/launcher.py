"""Validate a loopback instance before reusing its window; never stop another app."""
import json,socket,webbrowser
from urllib.request import urlopen
from .common import ROOT,VERSION,source_fingerprint,read_json
from .release import release_fingerprint,workspace_identity

def existing_instance(open_browser=False):
    """Reuse only a verified instance of this exact source and workspace, on any saved local port."""
    ports=[8765]
    try:
        saved=read_json(ROOT/'workspace'/'ui_endpoint.json',{})
        port=saved.get('port') if isinstance(saved,dict) else None
        if type(port) is int and 1<=port<=65535:ports=list(dict.fromkeys([port,*ports]))
    except (OSError,ValueError):pass # An interrupted/stale endpoint record cannot block startup.
    occupied=stale=False;engine=source_fingerprint();release=release_fingerprint()
    for port in ports:
        url=f'http://127.0.0.1:{port}'
        try:
            with urlopen(url+'/api/identity',timeout=2) as response:value=json.load(response)
            occupied=True
            ours=isinstance(value,dict) and value.get('application')=='FootballStrategyLab' and value.get('workspace_id')==workspace_identity(ROOT/'workspace')
            same=ours and value.get('version')==VERSION and value.get('engine_hash')==engine and value.get('release_hash')==release
            if same:
                if open_browser:
                    try:webbrowser.open(url)
                    except Exception as error:print('浏览器未能自动打开，请手动访问 '+url+'：'+str(error))
                return 0
            # This workspace already has a workstation of another build; a second scheduler must not start.
            stale=stale or ours
        except Exception:
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=1):occupied=True
            except OSError:pass
    return 3 if stale else 2 if occupied else 1

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--open',action='store_true',help='Open the verified existing workstation in a browser')
    raise SystemExit(existing_instance(parser.parse_args().open))
