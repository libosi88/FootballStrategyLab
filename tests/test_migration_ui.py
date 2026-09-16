"""Actual jobs.js lifecycle rendering in Node VM; not a browser acceptance."""
import json,shutil,subprocess,unittest
from lab.common import ROOT

class LegacyStageDisplay(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node is required')
    def test_old_inactive_stages_are_history_and_active_stages_remain_running(self):
        script=r'''
const fs=require('fs'),vm=require('vm');
class E {
 constructor(tag){Object.assign(this,{tagName:tag.toUpperCase(),children:[],dataset:{},className:'',_text:'',value:'',open:false});}
 append(...x){this.children.push(...x);} replaceChildren(...x){this.children=[];this.append(...x);}
 set textContent(x){this._text=String(x);this.children=[];} get textContent(){return this._text+this.children.map(x=>x.textContent).join('');}
 get options(){return this.children.filter(x=>x.tagName==='OPTION');} contains(x){return this===x||this.children.some(c=>c.contains(x));} querySelectorAll(){return [];}
}
class Option extends E {constructor(label,value){super('option');this.textContent=label;this.value=value;}}
const els={};for(const id of ['app-version','footer-version','workspace','count','jobs','jobselect'])els[id]=new E(id==='jobselect'?'select':'div');
const c={$:id=>els[id]||null,Option,lastJobs:[],states:{PAUSED:'已暂停',INTERRUPTED:'已中断',RUNNING:'实际计算中'},document:{activeElement:null,createElement:tag=>new E(tag)},text:(tag,value,cls)=>{const e=new E(tag);e.textContent=value;e.className=cls||'';return e;},download:(id,n)=>'/download/'+id+'/'+n};
vm.createContext(c);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),c);
const results=[];
for(const [old,status,pid] of [[true,'PAUSED',0],[true,'INTERRUPTED',0],[true,'RUNNING',123],[false,'RUNNING',123]]){
 const j={id:'test-'+results.length,league:'synthetic',profile:'standard',research_objective:'validation',status,pid,previous_version:old,created_version:'0.4.8',stages:{S1:{status:'RUNNING'},S7:{status:'FAIL'}},progress:{stage:'S1'}};
 const before=JSON.stringify(j);c.renderJobs({jobs:[j],version:'0.6.3',workspace:'temporary'});results.push({text:els.jobs.textContent,unchanged:before===JSON.stringify(j)});
}
console.log(JSON.stringify(results));
'''
        p=subprocess.run([shutil.which('node'),'-e',script,str(ROOT/'web/jobs.js')],capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(p.returncode,0,p.stderr);rows=json.loads(p.stdout)
        for r in rows[:2]:
            self.assertIn('上次阶段记录',r['text']);self.assertIn('上次停留于此（未完成）',r['text'])
            self.assertIn('当前未登记计算进程',r['text']);self.assertNotIn('S1 规格与测试：执行中',r['text'])
            self.assertIn('S7 交接包：失败',r['text'])
        for r in rows[2:]:
            self.assertIn('S1 规格与测试：执行中',r['text']);self.assertNotIn('当前未登记计算进程',r['text'])
        self.assertTrue(all(r['unchanged'] for r in rows))

if __name__=='__main__':unittest.main()
