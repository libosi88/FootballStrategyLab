/* Explicit research policies. No remote service or order interface. */
const historicalPolicyFields = [
  ['segment_basis','研究段划分','select','calendar_year',[
    ['calendar_year','固定日历年（默认；不足全年仍单独披露）'],
    ['fixed_365','固定锚点，每365天一段'],['end_anchored_365','最后比赛日锚点（旧口径，增量数据可能改变边界）']]],
  ['segment_anchor_date','固定研究段锚点','date',''],
  ['major_segment_share','主要段可用比赛占比','number','0.15'],
  ['minor_segment_loss_policy','小段已发生亏损','select','block',[
    ['block','不进入历史稳定主名单（默认）'],['disclose','只披露；允许观察，不宣称所有段盈利']]],
  ['segment_evidence_min_matches','小段证据提示最少场次（不是盈利保证）','number','5'],
  ['max_best_segment_profit_share','最好段正净胜占比上限（1仅披露）','number','1'],
  ['portfolio_segment_checks','检查共享名额执行后的组合分段稳定性','checkbox',true],
  ['missing_result_status','有终场比分但完场状态缺失','select','exclude',[
    ['exclude','隔离并披露（默认）'],['trial','仅标签试算，明确标注'],['archive_contract','有明确文件级完场契约']]],
  ['archive_result_contract','文件级完场契约的来源说明','text',''],
  ['complementarity_research','另做互补组合研究（不改主名单，不开放执行）','checkbox',false],
  ['complementarity_pair_budget','互补研究逐对比较预算（0不限）','number','10000']
];

function readHistoricalPolicyControls(){
  const result={};
  for(const [id,,type] of historicalPolicyFields){
    const el=document.getElementById(id);if(!el)throw Error('缺少研究设置：'+id);
    result[id]=type==='checkbox'?el.checked:type==='number'?el.value:el.value.trim();
  }
  return result;
}

function showHistoricalPolicyScope(){
  const root=document.getElementById('historical-policy-controls');if(!root)return;
  root.hidden=document.getElementById('research_objective').value!=='historical';
  const anchor=document.getElementById('segment_anchor_date');
  anchor.closest('label').hidden=document.getElementById('segment_basis').value!=='fixed_365';
  document.getElementById('archive_result_contract').closest('label').hidden=
    document.getElementById('missing_result_status').value!=='archive_contract';
  const warnings=[];
  if(!document.getElementById('portfolio_segment_checks').checked)warnings.push('组合分段门禁已关闭，本任务不能声称最终组合各段均达标');
  if(document.getElementById('minor_segment_loss_policy').value==='disclose')warnings.push('允许小段亏损，结果必须保留该限定');
  if(document.getElementById('missing_result_status').value==='trial')warnings.push('包含完场状态未确认的标签试算，不是已核实赛果');
  const notice=document.getElementById('historical-policy-warning');
  notice.textContent=warnings.join('；');notice.hidden=!warnings.length;
}

(function installHistoricalPolicyControls(){
  const host=document.getElementById('profile')?.closest('.panel');if(!host)return;
  const root=document.createElement('details');root.id='historical-policy-controls';
  const title=document.createElement('summary');title.textContent='历史稳定性、数据证据与互补研究';root.append(title);
  const grid=document.createElement('div');grid.className='grid2';root.append(grid);
  for(const [id,label,type,value,options] of historicalPolicyFields){
    const wrapper=document.createElement('label');wrapper.textContent=label;
    const el=document.createElement(type==='select'?'select':'input');el.id=id;
    if(type==='select')for(const [key,text] of options)el.append(new Option(text,key));
    else el.type=type;
    if(type==='checkbox')el.checked=value;else el.value=value;
    if(type==='number'){el.min='0';el.step=['major_segment_share','max_best_segment_profit_share'].includes(id)?'0.01':'1';}
    el.addEventListener('change',showHistoricalPolicyScope);wrapper.append(el);grid.append(wrapper);
  }
  const note=document.createElement('p');note.className='hint';
  note.textContent='第一阶段盈利池不受这些稳定性条件裁剪。条件在任务创建前冻结；小段样本不足不等于没有亏损。互补结果单独保存，未经独立触发交接验收不可代替主名单。';root.append(note);
  const warning=document.createElement('p');warning.id='historical-policy-warning';warning.className='notice';warning.hidden=true;root.append(warning);
  host.insertBefore(root,document.getElementById('settingsstatus'));
})();
