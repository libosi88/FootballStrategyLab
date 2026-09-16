"""Apply exact, guarded substitutions to existing long HTML/JavaScript lines."""
from pathlib import Path
import sys
import json
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from lab.common import DEFAULT

changes={
 'web/index.html':[
  ('<script src="/app.js"></script>','<script src="/research_policy.js"></script><script src="/app.js"></script>'),
  ('<option value="v3_full">v3 完整语法（算不完，只能部分覆盖）</option>',
   '<option value="balanced_v1">中等网格（核心三条件＋补充路径上下文）</option><option value="v3_full">v3 完整语法（大范围，以实际覆盖账为准）</option>'),
  ('紧凑网格（最多两条件，可完整算完）','紧凑网格（最多两条件，完整性看覆盖账）'),
  ('研究段以数据最后比赛日为锚，每12个月一段，代替按归档日切的自然年',
   '研究段默认按固定日历年，也可选择固定锚点365天；不冒充真实赛季，未满全年单独披露'),
  ('<option>方向汇总.csv</option>',
   '<option>方向汇总.csv</option><option>组合分段稳定性.json</option><option>主备实际风险比较.json</option><option>互补研究摘要.json</option><option>数据证据分层.json</option>'),
  ('较低风险备选（不可叠加）','较高收益回撤比备选（实际风险看比较表）'),
  ('紧凑语法每方向通常几万个节点就能算完，节点预算只是安全上限；',
   '不同联赛和语法的实际规模不同，启动后先冻结字典和搜索分母；')],
 'web/app.js':[
  ('return {...state.default_config,...saved};',
   'return {...(state.defaults_by_objective?.[saved.research_objective]||state.default_config),...saved};'),
  ("config.historical_diagnostics=$('historical_diagnostics').checked;",
   "config.historical_diagnostics=$('historical_diagnostics').checked;Object.assign(config,readHistoricalPolicyControls());"),
  (' showBudgetWarning();',' showBudgetWarning();showHistoricalPolicyScope();'),
  ('历史研究一般全部设为 0，任务才能完整算完。','预算为0表示不预设计算上限，不代表资源无限；达到预算应保留PARTIAL，不隐藏未完成。')],
}
for name,replacements in changes.items():
 p=ROOT/name;text=p.read_text(encoding='utf-8-sig')
 for old,new in replacements:
  if new in text:continue
  if text.count(old)!=1:raise ValueError('Unexpected source context: '+name+' '+old[:80])
  text=text.replace(old,new)
 p.write_text(text,encoding='utf-8',newline='\n')
# A new-task template is complete; a migration preset is deliberately partial.
# Switching research objectives must not overwrite the user's company, hardware
# allocation or match risk cap. Explicit old trial budgets are reset to unlimited.
preserved={'company','chunk_size','max_mask_mb','min_free_disk_mb','quote_cache_entries',
           'max_feature_cache_mb','release_direction_caches','selection_checkpoint_every',
           'match_cap','bootstrap_repetitions'}
templates={'default_config.json':DEFAULT,
           'rebuild_historical.json':{k:v for k,v in DEFAULT.items() if k not in preserved}}
for name,value in templates.items():
 p=ROOT/'config'/name
 p.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('Presentation and configuration templates synchronized.')
