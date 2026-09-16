"""Isolated 0.6.2 UI server. Does not touch port 8765 or start research workers."""
import shutil,sys,time
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from lab.common import atomic_json,read_json
from lab.server import serve

PORT=18772
SOURCE=Path(r'C:\Users\Administrator\AppData\Local\Temp\claude\C--Users-Administrator\792753f3-3e47-425f-99fe-44780edf5046\scratchpad\hist062_nonempty\workspace')
WORK=Path(__file__).resolve().parent/'ui_workspace'

def prepare():
    if not SOURCE.is_dir():raise FileNotFoundError('historical nonempty workspace missing: '+str(SOURCE))
    if WORK.exists():shutil.rmtree(WORK)
    shutil.copytree(SOURCE,WORK,ignore=shutil.ignore_patterns('mining','packages','*.sqlite3-wal','*.sqlite3-shm'))
    prefs=read_json(WORK/'ui_preferences.json',{})
    settings=dict(prefs.get('research_settings') or {})
    settings.update(research_objective='historical',select_budget=12,selection_eval_budget=200000,min_history_years=2)
    atomic_json(WORK/'ui_preferences.json',{'research_settings':settings,'input_paths':[str(ROOT/'demo')]})
    (WORK/'ui_endpoint.json').unlink(missing_ok=True)
    print(f'UI workspace ready: {WORK}',flush=True)

if __name__=='__main__':
    prepare()
    with patch('lab.server.launch_next',return_value=None):
        serve(WORK,PORT,False)
