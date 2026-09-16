"""Read-only independent settlement and actual JavaScript handler checks."""
import json
import math
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from lab.common import atomic_json,settlement,source_fingerprint
from lab.release import release_fingerprint

count=0
for quarter_line in range(-40,41):
    line=Fraction(quarter_line,4)
    contracts=(Fraction(math.floor(2*line),2),Fraction(math.ceil(2*line),2))
    for margin in range(-15,16):
        for water in (50,85,94,100,125):
            for side in (0,1):
                expected=Fraction(0)
                for contract in contracts:
                    result=(Fraction(margin)-contract)*(1 if side==0 else -1)
                    expected+=(Fraction(water,100) if result>0 else Fraction(-1) if result<0 else Fraction(0))/2
                actual=Fraction(settlement(quarter_line,water,margin,side,100),200)
                if actual!=expected:raise AssertionError((quarter_line,margin,water,side,actual,expected))
                count+=1

script=r'''
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const line=source.split(/\r?\n/).find(x=>x.startsWith("for(const name of ['jobselect','file','variant'])"));
if(!line)throw Error('selection handler missing');
const els={};for(const id of ['jobselect','file','variant','table','resultcontext','resultmetrics','pageinfo','prevpage','nextpage','csvdownload']){
 els[id]={value:'',textContent:'previous result',disabled:false,hidden:false,replaceChildren(){this.textContent='';}};
}
let aborted=0;
const context={$:id=>els[id],syncFileOptions(){},previewOffset:200,previewRequest:4,
 previewController:{abort(){aborted++;}},previewResult(){throw Error('empty selection requested preview');}};
vm.createContext(context);vm.runInContext(line,context);els.jobselect.onchange();
console.log(JSON.stringify({cleared:['table','resultcontext','resultmetrics','pageinfo'].every(id=>els[id].textContent===''),
 disabled:els.prevpage.disabled&&els.nextpage.disabled,hidden:els.csvdownload.hidden,
 aborted,offset:context.previewOffset,request:context.previewRequest}));
'''
node=shutil.which('node')
if not node:raise RuntimeError('Node missing; JavaScript handler has not been verified')
result=subprocess.run([node,'-e',script,str(ROOT/'web/app.js')],capture_output=True,text=True,encoding='utf-8',timeout=20)
if result.returncode:raise RuntimeError(result.stderr)
actual=json.loads(result.stdout)
expected={'cleared':True,'disabled':True,'hidden':True,'aborted':1,'offset':0,'request':5}
if actual!=expected:raise AssertionError(actual)

batch=(ROOT/'启动工作台.bat').read_text(encoding='utf-8-sig')
if 'goto conflict' in batch or ':conflict' in batch:raise AssertionError('Startup still bypasses the environment check on a port conflict')
if batch.index('assert numpy.__version__')>batch.index('-B app.py serve'):raise AssertionError('Dependency check occurs after starting the server')
report={'status':'PASS','engine_hash':source_fingerprint(),'release_hash':release_fingerprint(),
        'independent_settlement_cases':count,'javascript_clear_selection':actual,
        'windows_startup_branch':'STATIC_CONTROL_FLOW_CHECK_PASS_NOT_INSTALL_EXECUTION',
        'real_orders_sent':0}
target=Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).parent/'final/additional_checks.json'
atomic_json(target,report)
print(json.dumps(report,ensure_ascii=False))
