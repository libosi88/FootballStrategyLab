"""Human cards generated from the exact executable rule tree; not a second rule set."""
from .common import canonical, DIRECTIONS,historical_objective
from .rules import label

FEATURE_NOTES={
 'line':'主队让球为正、主队受让为负；大小球是全场总盘口。',
 'line_init':'当前有符号盘口减去本阶段首条有效报价盘口；不是初盘值本身，也不是窗口首盘。',
 'samewater':'当前实际买入侧水位减去该盘口数值本阶段首次出现时的同侧水位。',
 'other_samewater':'当前对侧水位减去该盘口数值本阶段首次出现时的对侧水位。',
 'water_init':'当前实际买入侧水位减去本阶段首条有效报价的该侧水位。',
 'role_diff':'只适用于让球；由本事件实际下注球队映射当时主客比分，不把大球称为球队。',
 'water':'当前报价中实际买入侧的水位（香港盘净水；机器值=水位×water_scale）。',
 'otherwater':'当前报价中对侧（未买入侧）的水位。',
 'absline':'有符号盘口的绝对值，只表示盘口深浅，不区分主让或客让。',
 'goal_diff':'当时比分主队进球减客队进球；无当时比分时为缺失。',
 'abs_diff':'当时比分分差的绝对值。',
 'total_goals':'当时比分主客进球之和。',
 'score_code':'当时比分编码：主队进球×100+客队进球，例如101表示1-1。',
 'status':'源报价状态：0=早盘，1=即时盘，2=滚球。',
 'other_water_init':'对侧水位减本阶段首条有效报价的对侧水位。',
 'other_water_prev_keep':'对侧水位减同盘口紧邻此前有效报价的对侧水位；跨封盘保留。',
 'other_water_prev_reset':'对侧水位减同盘口紧邻此前有效报价的对侧水位；遇封盘/无效报价断开。',
 'other_water_prev_any_keep':'对侧水位减紧邻此前有效报价的对侧水位（允许变盘）；跨封盘保留。',
 'other_water_prev_any_reset':'对侧水位减紧邻此前有效报价的对侧水位（允许变盘）；遇封盘/无效报价断开。',
 'water_from_current_min':'当前水位减去截至当前事件（含本事件）的本阶段该侧最小水位。',
 'minute':'源数字比赛分钟；整数-2仅表示文字中场，缺分钟不是0。≤、≥和区间只比较数字分钟（≥0），文字中场只能用“=−2”单独表达。',
}

def feature_note(feature):
    if feature.startswith('pre_'):return '本市场从首条赛前报价起增量累计，在本场首次观察到滚球时冻结；此后赛前补发不得改写。last_path使用盘口优先投影，默认keep；reset后缀表示末次封盘后路径；closed保留赛前末次状态。当前实际买入侧决定摘要水位侧。'
    if feature.startswith('cross_'):return '另一市场在当前墙钟分钟之前已完成的最后报价状态；同分钟报价不可用，最新封盘不可回退到旧有效报价。阶段、年龄和历史基准一并冻结。年龄超过执行配置即缺失：滚球按cross_stale_minutes（默认5分钟）；赛前按cross_stale_minutes_prematch（默认1440分钟），因为赛前只在报价变化时记一行，未变化的报价仍是当前价。'
    if feature.startswith('role_water_'):return '逐历史事件按当时盘口正负映射让球/受让角色，再累计该角色水位基准；与固定当前实际球队回看历史水位不同。盘口为0时角色缺失，不把平手混作让球方。'
    if feature.startswith('stoppage_'):return '只解析明确45+N或90+N；保留所属半场、追加分钟和累计分钟，不能用普通数字47推断45+2。'
    if feature=='line_same' or feature.startswith('line_return_'):return '当前盘口减去该盘口数值本身，按定义恒为0；保留为可审计基准，不冒充独立盘差信息。'
    if feature.startswith('window_'):return '固定绑定的数字分钟半开窗口内第一条有效报价为基准；先于比分或其他条件过滤保存；窗口之外为缺失；封盘保留此基准。不是阶段初盘，也不是第一条满足其余策略条件的报价。'
    if feature in FEATURE_NOTES:return FEATURE_NOTES[feature]
    if feature.startswith('linepath'):return '仅盘口变动计步，1升盘、2降盘；水位变动不插入盘口路径。keep跨封盘保留，reset遇无效或封盘清空。'
    if feature.startswith('path'):
        return '普通条件为盘口优先投影：1升盘、2降盘、3同盘升水、4同盘降水。event_model=composite时一条报价可为13/14/23/24单一步双属性，模式由pattern声明。无变化不计步；sequence明确连续或可插入且末步最新。keep跨封盘保留，reset在无效/封盘清空；pulse只在新末步事件，state持续；跨度和适用步幅按每个所选步骤核对。'
    if feature.startswith(('returnwater','other_returnwater')):
        return '当前盘口本次重新进入的第一条有效报价为基准；重返不是该盘首次。reset在无效/封盘后重新起算。'
    if feature.startswith('water_prev_'):
        return ('紧邻此前有效报价水差（允许变盘）；' if '_any_' in feature else '同盘口的紧邻此前有效报价水差；')+'不是该盘首次水位。reset遇封盘/无效报价断开，keep保留。'
    if 'from_current_min' in feature or 'from_current_max' in feature:return '同阶段极值包含当前事件；当前值减去该极值。首条有效事件差值为0。'
    if 'from_min' in feature or 'from_max' in feature:
        return '同阶段截至上一有效事件的极值作基准；首条有效报价基准取自身，差值为0。'
    return '精确定义见包内lab/features.py、机器规则与数据契约；不可自行补加或省去条件。'


def write_cards(folder,packet):
    text=['# 本版逐条触发执行卡','',
          '**范围：本版冻结名单的自动模拟与开发；搜索完整状态见workflow_status.json。真实下注和第二笔关闭。**','',
          '项目契约：`'+canonical(packet['contract'])+'`','',
          '统一政策：`'+canonical(packet['execution_policy'])+'`','',
          '阈值为精确整数刻度。水位除以water_scale、盘口除以4后才是显示值。未来触发不得读取终场比分。','']
    for n,r in enumerate(packet['rules'],1):
        text += [f"## {n}. {r['id']}｜{DIRECTIONS[r['direction']]}", '',
                 f"内容哈希：`{r['content_hash']}`；固定优先级：{r['priority']}。",'',
                 '全部条件同时成立，取该策略本场首个合法事件；条件判断与订单许可是两件事。','']
        for a in r['conditions']:
            text += ['- '+label(a,packet['water_scale']),
                     '  - 机器定义：`'+canonical({k:v for k,v in a.items() if k!='label'})+'`',
                     '  - 基准说明：'+feature_note(a['feature'])]
        if not r['conditions']:text += ['无附加条件，仍须满足阶段/市场/方向及有效报价。']
        if r.get('execution_metrics'):
            e=r['execution_metrics'];text += [f"{'分钟末历史净胜（剔除进球前拒单，入选依据）' if e.get('price_basis')=='historical_minute_close_pregoal_rejected' else '完整分钟末执行原价（入选依据）'}：{e['n']} 笔；净收益 {e['net']:.3f} 单位；比赛排序回撤 {e['drawdown']:.3f}；忽略走盘最大连亏 {e['streak']} 笔。不是实际成交。"]
        if r.get('stress'):
            # Stress prices keep pre-goal rejected fills, so the historical denominator is the order count before rejection.
            e=r.get('execution_metrics') or r['metrics'];rejected=(r.get('pregoal_rejection') or {}).get('rejected_n') or 0
            base_n=max(1,e['n']+(rejected if e.get('price_basis')=='historical_minute_close_pregoal_rejected' else 0))
            text += [f"三种冻结取价压力情景：最低净胜 {min(m['net'] for m in r['stress']):.3f} 单位；最大回撤 {max(m['drawdown'] for m in r['stress']):.3f} 单位；最低订单保留率 {min(m['n'] for m in r['stress'])/base_n:.1%}。"]
        if r.get('pregoal_rejection'):
            p=r['pregoal_rejection'];pm=p['metrics'];text += [f"进球前代理拒单情景（后续1分钟同市场明确封盘且比分改变；不是实际回执，不补单）：假定拒单 {p['rejected_n']} 笔，移除净胜 {p['rejected_net']:.3f} 单位；代理剔除后净胜 {pm['net']:.3f}、回撤 {pm['drawdown']:.3f}。"]
        text += ['',f"触发瞬间研究指标（非执行口径，仅作对照）：{r['metrics']['n']} 场；净胜 {r['metrics']['net']:.3f} 单位；忽略走盘最大连亏 {r['metrics']['streak']} 笔。",'',
         '缺本阶段历史/初盘/路径时，不得将当前行情冒充初盘；先回补可靠历史。新数据源、单位或规则版本不同会拒绝沿用旧状态。', '']
    if not packet['rules']:text+=['当前名单为空。无策略不是软件故障，不能编造黄金信号。']
    (folder/'完整触发卡.md').write_text('\n'.join(text)+'\n',encoding='utf-8')


GATE_NAMES={'full_standard_search':'完整标准搜索','engine_tests':'引擎测试','all_pool_review':'全池复核','selection':'组合比较','trigger':'触发核对',
    'nonempty_roster':'非空主名单','rule_cases':'规则样例','persistent_paper':'持久化模拟','streaming_paper':'流式模拟',
    'execution_economics':'实际执行收益与风险','packaging':'空目录打包','standard_research_thresholds':'标准研究门槛未改动','holdout_evaluation':'样本外评测已执行'}
RECOMMENDATION_NAMES={'RECOMMENDED_FOR_PAPER_TRADING':'推荐进入自动模拟（不是实盘批准）','OBSERVATION_ONLY':'仅观察，不推荐进入模拟'}
RECOMMENDATION_NAMES.update(HISTORICAL_SELECTION_READY='历史稳定高净胜名单已生成',HISTORICAL_SEARCH_EMPTY='历史搜索完成，无符合本次条件的策略',PARTIAL_HISTORICAL_RESULT='历史研究部分结果，尚未完成')
VERDICT_NAMES={'CONFIRMED':'确认（净胜为正）','FAILED':'未通过','INSUFFICIENT_SAMPLE':'订单不足，无法确认','EMPTY_ROSTER':'名单为空',
    'UNAVAILABLE_SHORT_HISTORY':'数据太短，未留样本外','UNAVAILABLE_NO_RECENT_MATCHES':'最近12个月无比赛','DISABLED':'已关闭','NOT_RECORDED':'无留出记录','NOT_RUN':'未执行'}

def count_csv_rows(path):
    if not path.is_file():return 0
    import csv
    with path.open(encoding='utf-8-sig',newline='') as f:return max(0,sum(1 for _ in csv.reader(f))-1)

def roster_lines(summary,config):
    """Joint replay of each roster on the historical basis at the frozen match cap; single nets are never added up."""
    cap=str(config.get('match_cap'));out=[]
    policies={f'整场最多{cap}单位'}
    if cap=='24':policies.update(('每核心方向首单，无额外整场约束','每命名方向首单，无额外整场约束','每方向首单，无额外整场约束'))
    for name,label in (('默认','主名单'),('较低风险','较稳定备选')):
        row=next((x for x in summary.get('comparisons',[]) if x.get('名单')==name and str(x.get('整场上限'))==cap and str(x.get('情景'))=='4' and x.get('政策') in policies),None)
        if row:out.append(f"{label}组合（共同回放，整场最多{cap}单位，历史口径）：{row.get('n')} 笔，净胜 {_number(row.get('net'))} 单位，ROI {_number(row.get('roi'),2,True)}，按场最大回撤 {_number(row.get('drawdown_match'))} 单位。")
    return out

def historical_decision_markdown(root,summary,coverage,trigger,completion,validation):
    from pathlib import Path
    from .common import read_json,VERSION
    root=Path(root);config=read_json(root/'config.json',{});audit=read_json(root/'data_audit.json',{})
    labels=read_json(root/'labels.json',{});dates=sorted(l['date'] for l in labels.values() if l.get('eligible') and l.get('date'))
    rules=read_json(root/'results/rules.json',{'rules':[]})['rules']
    status=(completion.get('recommendation') or {}).get('status')
    evidence=audit.get('result_evidence',{})
    risk=read_json(root/'results/主备实际风险比较.json',{})
    segments=read_json(root/'results/组合分段稳定性.json',{})
    lines=['# 历史研究最终结论','',f"版本 {VERSION}；{audit.get('league','')} / {config.get('company','')}。",'',
           f"目标：在全部选定历史和冻结规则范围内，先收集净胜>{config.get('min_profit','10')}单位的规则，再比较多年稳定性与高净胜；不推断未来。",
           f"历史区间：{dates[0] if dates else '—'} 至 {dates[-1] if dates else '—'}；使用全部选定历史，未扣除最近一年。",
           f"结论：{RECOMMENDATION_NAMES.get(status,status)}；流程状态 {completion.get('state')}。",
           f"搜索语法：{config.get('search_grammar')}；冻结表达 {coverage.get('planned_expressions')}，待评价 {coverage.get('remaining')}，盈利候选发生数 {coverage.get('candidates')}。",
           f"主名单 {len(rules)} 条；较高收益回撤比备选 {summary.get('backup_selected',0)} 条。实际风险是否更低见比较表，两份名单不叠加。",
           f"研究段划分：{config.get('segment_basis','end_anchored_365')}；固定锚点：{config.get('segment_anchor_date') or '不适用'}。数据最后一年可能未结束，不把日历年标签当成完整赛季。",
           f"完场证据政策：{evidence.get('policy','旧版未记录')}；仅标签试算纳入 {evidence.get('label_only_trial_included','未知')} 场。所有标签仍未经独立外部核验。",
           f"组合分段门禁：{config.get('portfolio_segment_checks',False)}；小段亏损政策：{config.get('minor_segment_loss_policy','disclose')}；主备实际风险比较：{risk.get('status','未记录')}。",
           '组合仍存在小样本研究段，详见组合分段稳定性.json；不能把样本不足写成已充分证明稳定。' if segments.get('has_small_sample_segments') else '组合分段与浓集度证据见组合分段稳定性.json。',
           *roster_lines(summary,config),
           f"历史筛选：盈利池净胜>{config.get('min_profit','10')}；至少覆盖 {config.get('min_history_years',2)} 年（按365天/年），主要研究段净胜为正；场次按方向可用比赛数折算，至少{config.get('min_matches')}、至多要求{config.get('min_matches_ceiling')}；历史收益回撤比≥{config.get('min_return_drawdown_ratio')}。",
           '历史净胜按分钟末报价及冻结的代理成交政策计算：后续1分钟同市场明确封盘且比分改变时假定拒单。没有真实回执，不声称这些报价实际必然无法成交；首次信号记零，不补单。',
           '压力、扰动和显著性仅为附加对照，不决定本次历史名单资格；数据质量与触发一致性检查仍然必需。',
           f"触发核对 {trigger.get('status')}；订单差异 {sum((trigger.get('order_differences') or {}).values())}；空目录交接校验 {validation.get('status')}。",'',
           '| 方向 | 规则 | 历史净胜 | 场次 | 最大回撤 | 最大连亏 | 覆盖天数 |','|---|---|---:|---:|---:|---:|---:|']
    for r in rules:
        m=r.get('execution_metrics') or r['metrics']
        lines.append(f"| {DIRECTIONS.get(r['direction'],r['direction'])} | {r['id']} | {m.get('net')} | {m.get('n')} | {m.get('drawdown')} | {m.get('streak')} | {m.get('history_days',0)} |")
    lines+=['','原始盈利池由可逆家族账保存，筛选落选不删除盈利表达。完整只指上述冻结语法，不代表所有可能规则或数学全局最优组合。',
            '后续按联赛将触发代码接入复盘系统；本包没有替换该系统的旧策略，也没有连接账户或发送真实订单。']
    return '\n'.join(lines)+'\n'

def _number(value,digits=3,percent=False):
    if value is None:return '—'
    return f'{value:.{digits}%}' if percent else f'{value:.{digits}f}'

def final_decision_markdown(root,summary,coverage,trigger,prospective,validation):
    """The single plain-language conclusion of this research version, written after S6-S7 verification."""
    from pathlib import Path
    from .common import read_json,VERSION
    if historical_objective(read_json(Path(root)/'config.json',{})):
        return historical_decision_markdown(root,summary,coverage,trigger,prospective,validation)
    root=Path(root);config=read_json(root/'config.json',{});audit=read_json(root/'data_audit.json',{})
    labels=read_json(root/'labels.json',{});dates=sorted(l['date'] for l in labels.values() if l.get('eligible') and l.get('date'))
    packet=read_json(root/'results/rules.json',{'rules':[]});rules=packet.get('rules',[])
    backup=read_json(root/'results/lower_risk/rules.json',{'rules':[]}).get('rules',[])
    modules=read_json(root/'coverage_modules.json',{}).get('modules',{})
    passed=sum(1 for row in modules.values() if row.get('status')=='PASS')
    unfinished='、'.join(k for k,v in sorted(modules.items()) if v.get('status')!='PASS')
    # Legacy finite profiles have no standard module ledger; say so instead of reporting 0/9 as all passed.
    module_text=f"标准模块 {passed}/9 通过（{unfinished+'未完成' if unfinished else '全部通过'}）" if modules else '标准模块账不适用（非标准档位）'
    missing=[name for name,value in (prospective.get('gates') or {}).items() if not value]
    orders=sum((trigger.get('order_differences') or {}).values())
    holdout=read_json(root/'results/样本外评测.json',{});plan=holdout.get('plan') or {};rosters=holdout.get('rosters') or {}
    testing=holdout.get('multiple_testing') or {};rec=prospective.get('recommendation') or {}
    def roster_line(name,title):
        x=rosters.get(name)
        if not x:return f"- 样本外（{title}）：{VERDICT_NAMES.get(holdout.get('status'),holdout.get('status') or '未执行')}。"
        m=x['report']['scenarios'][0] if x['report']['scenarios'] else {}
        worst=min((s['net'] for s in x['report']['scenarios']),default=None)
        return f"- 样本外（{title}）：{VERDICT_NAMES.get(x['verdict'],x['verdict'])}；订单 {m.get('n',0)} 笔，净胜 {_number(m.get('net'))} 单位，ROI {_number(m.get('roi'),2,True)}，z {_number(m.get('z'),2)}；各情景最低净胜 {_number(worst)}。"
    lines=[f"# 最终决定：{audit.get('league','')} / {config.get('company','')}（软件{VERSION}）",'',
        f"**结论：{RECOMMENDATION_NAMES.get(rec.get('status'),'仅观察，不推荐进入模拟')}。**"+(' 原因：'+'；'.join(rec.get('reasons',[]))+'。' if rec.get('reasons') else ''),'',
        f"流程状态 {prospective.get('state')}。",'',
        f"- 数据：发现与选择使用 {dates[0] if dates else '—'} 至 {dates[-1] if dates else '—'}，合格比赛 {sum(1 for l in labels.values() if l.get('eligible'))} 场；"
        +(f"最近12个月（{plan.get('cutoff')} 之后）{plan.get('holdout_matches')} 场留作样本外，发现和选择从未使用。" if plan.get('status')=='SPLIT' else f"样本外留出：{VERDICT_NAMES.get(plan.get('status'),plan.get('status') or '无记录')}。"),
        f"- 搜索语法 {config.get('search_grammar','—')}：{module_text}；冻结表达覆盖 {_number(prospective.get('coverage_ratio'),4,True)}。",
        f"- 冻结表达 {coverage.get('planned_expressions')}；实际评价代表 {coverage.get('evaluated')}；历史等价 {coverage.get('equivalent_occurrences')}；零命中证明 {coverage.get('proven_zero')}；安全上界排除 {coverage.get('proven_nonprofitable')}；待评价 {coverage.get('remaining')}；净胜>{config.get('min_profit','10')}原始候选发生数 {coverage.get('candidates')}。",
        f"- 检验次数：实际评价 {testing.get('evaluated_signal_classes',coverage.get('evaluated'))} 个不同历史信号类；入选规则中按次数（Bonferroni）校正后仍显著的 {testing.get('significant_after_correction','—')}/{len(rules)} 条。样本内盈利大多可由大量尝试解释，样本外结果更关键。",
        roster_line('main','主名单'),roster_line('lower_risk','较低风险备选'),
        f"- 名单：开发/自动模拟主名单 {len(rules)} 条（赛前 {sum(r['direction'].startswith('PRE') for r in rules)}，滚球 {sum(r['direction'].startswith('LIVE') for r in rules)}）；较低风险备选 {len(backup)} 条，不可叠加；高风险备选 {count_csv_rows(root/'results/高风险备选.csv')} 条，低样本观察 {count_csv_rows(root/'results/低样本观察.csv')} 条；真实实盘批准 0 条。",
        f"- 研究标准：场次≥max({config.get('min_matches')}，方向可用场次×{config.get('min_matches_share')}) 且至多要求 {config.get('min_matches_ceiling')} 场；各研究段覆盖≥期望的 {config.get('min_segment_share')}；收益回撤比≥{config.get('min_return_drawdown_ratio')}（含压力与拒单情景）；压力净胜>{config.get('stress_min_profit')}；组合收益回撤比 {config.get('portfolio_min_return_drawdown_ratio')}／较低风险 {config.get('conservative_portfolio_min_return_drawdown_ratio')}。连亏只作报表列。",
        f"- 执行政策：每核心方向首单，整场模拟上限 {config.get('match_cap')} 单位，第二笔关闭；分钟末报价、三个减水/延迟压力情景与进球前报价拒单情景；赛前{'只在即时盘触发' if config.get('prematch_trigger_status')=='即' else '早盘与即时盘均可触发'}；跨市场陈旧度滚球 {config.get('cross_stale_minutes')} 分钟、赛前 {config.get('cross_stale_minutes_prematch')} 分钟。",
        f"- 触发验收：{trigger.get('status')}；漏信号 {trigger.get('missing_signals')}，多/错信号 {trigger.get('extra_or_changed_signals')}，订单差异 {orders}；空目录A包验收：{validation.get('status')}。",
        f"- 未通过门禁：{'、'.join(GATE_NAMES.get(g,g) for g in missing) or '无'}。",'',
        '| 方向 | 可用场次 | 所需场次 | 主名单 | 较低风险备选 | 第二阶段资格 | 说明 |','|---|---:|---:|---:|---:|---:|---|']
    for row in summary.get('directions',[]):
        reason='' if row.get('全局保留') else '无规则通过第二阶段门槛' if not row.get('资格数') else '有资格规则，但组合比较未入选'
        lines.append(f"| {row.get('方向')} | {row.get('方向可用场次','—')} | {row.get('所需场次','—')} | {row.get('全局保留',0)} | {row.get('低风险备选',0)} | {row.get('资格数',0)} | {reason} |")
    lines += ['','只有搜索完整、主名单非空且留出的最近12个月样本外确认为正，才推荐进入自动模拟；任何结论都不是实盘批准。滚动与真正未来数据评测另行记录。']
    return '\n'.join(lines)+'\n'

def gap_notes_markdown(prospective,coverage):
    if prospective.get('research_objective')=='historical':
        missing=[GATE_NAMES.get(k,k) for k,v in prospective.get('gates',{}).items() if not v and k!='nonempty_roster']
        return '\n'.join(['# 历史研究缺口说明','',f"状态：{prospective.get('state')}。",'未完成项：'+('、'.join(missing) or '无')+'。',
            f"剩余待评价表达：{coverage.get('remaining')}；规定语法覆盖率：{_number(prospective.get('coverage_ratio'),4,True)}。",
            '名单情况：'+('已找到符合本次历史稳定性条件的策略。' if prospective.get('gates',{}).get('nonempty_roster') else '尚无符合本次历史稳定性条件的策略；不因此伪造名单。'),
            '使用全部选定历史；样本外、显著性与未来判断不属于本次完成门槛。','复盘系统策略注册与实际执行接入属于后续迁移，当前交付包未替换其旧策略。'])+'\n'
    missing=[GATE_NAMES.get(g,g) for g,value in (prospective.get('gates') or {}).items() if not value]
    rec=prospective.get('recommendation') or {}
    lines=['# 缺口说明','',f"流程状态：{prospective.get('state')}；推荐：{RECOMMENDATION_NAMES.get(rec.get('status'),rec.get('status') or '—')}。",'',
        '不推荐进入模拟的原因：'+('；'.join(rec.get('reasons',[])) if rec.get('reasons') else '无')+'。',
        '未通过门禁：'+('、'.join(missing) if missing else '无')+'。',
        f"搜索剩余待评价表达发生数：{coverage.get('remaining')}；冻结语法覆盖 {_number(prospective.get('coverage_ratio'),4,True)}。",
        f"样本外留出评测：{VERDICT_NAMES.get(prospective.get('historical_holdout'),prospective.get('historical_holdout'))}。",'',
        '始终未包含：真实行情/成交接口、账户风控、第二笔执行、未来独立验证、紧凑语法之外的v3完整语法扩展。',
        '样本外留出只评测一次；据此修改门槛或重选名单必须新建研究版本，并换用此前未用过的数据评测。']
    return '\n'.join(lines)+'\n'
