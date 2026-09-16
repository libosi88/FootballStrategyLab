import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent/'tests'))
from lab.common import DEFAULT, read_json, atomic_json, digest, source_fingerprint, VERSION
from lab.standard_mining import ordered_standard_events
from lab.mining import build_direction
from lab.standard_columns import StandardColumns
from lab.standard_atoms import AtomCatalog, W, C
from lab.standard_masks import MaskStore
from lab.standard_search import run_class_search
from lab.standard_review import select_standard
from test_core import event

rows=[];labels={}
for mid in range(90):
    sid=f'r{mid:03d}';year=2024+mid//30
    labels[sid]={'eligible':True,'final':[0,0] if mid%5==0 else [2,1],'year':year,'date':f'{year}-01-01','kickoff':mid*10}
    for step in range(3):
        e=event(len(rows),line=4,w0=90+step*5,ts=mid*10+step);e.update(sid=sid,mid=mid,market=0,minute=step)
        rows.append(e)
rows=ordered_standard_events(rows);config={**DEFAULT,'profile':'standard','directions':['LIVE_OVER'],'select_budget':5}
td=tempfile.TemporaryDirectory();root=Path(td.name);mine=root/'mining/LIVE_OVER';mine.mkdir(parents=True)
atomic_json(root/'config.json',config);atomic_json(root/'labels.json',labels)
from lab.common import write_jsonl
write_jsonl(root/'events.jsonl.gz',rows)
base=build_direction(rows,labels,100,'LIVE_OVER',config)
import numpy as np
np.savez_compressed(mine/'arrays.npz',**base)
with AtomCatalog(mine/'atoms.sqlite3',True) as atoms:
    for v in (85,90,96):atoms.add({'feature':'water','op':'ge','value':v},W|C,100)
    atoms.commit()
    with StandardColumns(base,rows,'LIVE_OVER') as columns,MaskStore(mine,columns,atoms,config,'review_fixture') as masks:
        masks.prepare(lambda **kw:None,lambda:False)
        run_class_search(mine,atoms,masks,{**config,'_water_scale':100},lambda **kw:None,lambda:False,'review_fixture')

def binding():
    return digest({'version':VERSION,'engine':source_fingerprint(),'config':config,'input':read_json(root/'prepared_manifest.json'),'mining':{d:read_json(root/'mining'/d/'state.json') for d in config['directions']}})

b0=binding()
print('B before call1:',b0)
print('state.json:',read_json(mine/'state.json').get('status'))
print('prepared_manifest exists:', (root/'prepared_manifest.json').exists())
result=select_standard(root,rows,labels,100,config,lambda **kw:None,lambda:False)
print('--- running verify ---')
from lab.verify import verify
report=verify(root)
print('verify status:',report['status'])
b1=binding()
print('B after call1+verify:',b1)
print('same:',b0==b1)
print('state.json after:',read_json(mine/'state.json').get('status'))
print('prepared_manifest exists after:', (root/'prepared_manifest.json').exists())
# check engine fingerprint stability
from lab.common import canonical
print('engine same:',source_fingerprint()==source_fingerprint())
print('config canonical same:',canonical(config)==canonical(config))
print('input:',read_json(root/'prepared_manifest.json'))
print('mining state status:',read_json(mine/'state.json').get('status'))
print('--- second select_standard ---')
again=select_standard(root,rows,labels,100,config,lambda **kw:None,lambda:False)
print('again selected:',again['selected'])
td.cleanup()
