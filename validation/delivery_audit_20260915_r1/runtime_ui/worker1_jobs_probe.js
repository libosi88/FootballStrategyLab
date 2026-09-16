// Execute the real jobs renderer against minimal disposable DOM objects.
const fs = require('fs'), vm = require('vm');
class Element {
  constructor(tag) { this.tagName=tag.toUpperCase(); this.children=[]; this.dataset={}; this.className=''; this._text=''; this.value=''; this.open=false; }
  append(...items) { for(const item of items) { this.children.push(item); item.parent=this; } }
  replaceChildren(...items) { this.children=[]; this.append(...items); }
  set textContent(value) { this._text=String(value); this.children=[]; }
  get textContent() { return this._text+this.children.map(c=>c.textContent).join(''); }
  get options() { return this.children.filter(c=>c.tagName==='OPTION'); }
  contains(node) { return this===node||this.children.some(c=>c.contains(node)); }
  querySelectorAll() { return []; }
}
class Option extends Element { constructor(label,value) { super('option');this.textContent=label;this.value=value; } }
const els={};
for(const id of ['app-version','footer-version','workspace','count','jobs','jobselect']) els[id]=new Element(id==='jobselect'?'select':'div');
const context={$:id=>els[id]||null,Option,lastJobs:[],states:{RUNNING:'实际计算中',PAUSING:'保存断点'},
  document:{activeElement:null,createElement:tag=>new Element(tag)},
  text:(tag,value,cls)=>{const el=new Element(tag);el.textContent=value;el.className=cls||'';return el;},
  download:(id,path)=>'/download/'+id+'/'+path,displayValue:(key,value)=>String(value)};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const jobs=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
const result=[];
for(const job of jobs) {
  context.renderJobs({jobs:[job],version:'audit',workspace:'isolated'});
  const buttons=[];
  function walk(el) { if(el.tagName==='BUTTON') buttons.push(el.textContent); for(const child of el.children)walk(child); }
  walk(els.jobs);
  result.push({id:job.id,previous_version:job.previous_version,status:job.status,buttons,text:els.jobs.textContent});
}
console.log(JSON.stringify(result));
