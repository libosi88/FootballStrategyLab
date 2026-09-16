"""Consolidate actual audit evidence without modifying the frozen release files."""
import csv
import json
import subprocess
import sys
import zipfile
from pathlib import Path

AUDIT=Path(__file__).resolve().parent
ROOT=AUDIT.parents[1]
sys.path.insert(0,str(ROOT))
from lab.common import atomic_json,read_json,sha,source_fingerprint
from lab.release import release_files,release_fingerprint

# Each row groups one root cause rather than counting every failing assertion.
ISSUES=[
 ('F01','缺失的重复赛果被当作冲突，完整比赛被排除','lab/data.py','test_full_audit_data.py','缺失标签双向合并；真实冲突与畸形非空标签仍排除'),
 ('F02','重复CSV列名被静默覆盖','lab/common.py;lab/catalog.py;lab/data.py','test_full_audit_data.py','报价、索引、排除表统一拒绝重复表头；扫描缓存升级v6'),
 ('F03','公司、方向和关闭开关接受错误数据类型','lab/common.py','test_full_audit_data.py','先进行严格类型校验，明确返回配置错误'),
 ('F04','赛果索引或质量清单变化后复用旧多机任务','lab/fleet.py','test_full_audit_runtime.py','全部实际输入的类别与哈希参与任务身份'),
 ('F05','预处理清单删去校验项后仍复用缓存','lab/data.py;lab/pipeline.py','test_full_audit_runtime.py','必须具有完整必需文件和哈希；留出及训练分区分别校验'),
 ('F06','中场类别与数值分钟条件归一化不等价','lab/normalization.py','test_full_audit_search.py','保留负分钟类别与数值条件的真实交集'),
 ('F07','损坏的部分搜索账可被读取或重新封存','lab/search_integrity.py;lab/standard_search.py;lab/standard_mining.py','test_full_audit_search.py','恢复、读取和准备前校验状态、绑定和封存证据'),
 ('F08','零候选快捷路径绕过导出或全池审查证据','lab/standard_cli.py;lab/standard_review.py','test_full_audit_execution.py;test_full_audit_review.py','零候选、无数据和已有审查游标也先校验搜索证据'),
 ('F09','筛选缓存缺少部分输出校验仍被接受','lab/review_cache.py','test_full_audit_search.py','全部四份输出必须存在并逐一匹配哈希'),
 ('F10','默认字典读取能写库并创建缺失的空库','lab/standard_atoms.py','test_full_audit_search.py','默认以只读URI打开；显式写模式才创建；初始化失败关闭连接'),
 ('F11','已搜索方向缺失字典封存标记仍能继续','lab/standard_mining.py','test_full_audit_search.py','已停止的非空方向必须保持字典封存证据'),
 ('F12','已完成组合的日志或断点损坏后仍复用结果','lab/portfolio_search.py','test_full_audit_portfolio.py;test_search_speed_062.py','校验断点载荷及日志已提交前缀；增量哈希并保留确定性续跑'),
 ('F13','事件Schema同时要求且禁止quality_blocked','lab/handoff_assets.py','test_full_audit_execution.py','声明可选布尔属性，与正式事件校验一致'),
 ('F14','调用方能通过原报价或返回信号改变待执行状态','lab/paper_runner.py','test_full_audit_execution.py','保存独立副本，隔离外部可变对象'),
 ('F15','构造后修改名单对象能影响已冻结协调器','lab/paper_execution.py;lab/paper_runner.py','test_full_audit_execution.py','构造时冻结完整packet独立副本'),
 ('F16','部分覆盖缺少backend时未校验封存','lab/workflow_gates.py','test_full_audit_execution.py','缺省后端也必须通过部分搜索证据检查'),
 ('F17','B包失败提前改写原任务最终结论','lab/packaging.py','test_full_audit_execution.py','结论在私有构建区准备，成功发布A/B后才替换原文'),
 ('F18','Windows下A包、B包和根目录结论字节不一致','lab/packaging.py','test_full_audit_execution.py','统一写入UTF-8字节，消除平台换行转换差异'),
 ('F19','交接Schema、执行配置或接入清单缺失仍验收通过','lab/handoff_assets.py','test_full_audit_execution.py','按冻结packet重算三份资产及绑定哈希并核对'),
 ('F20','引擎测试缓存未校验本次绑定','lab/workflow_journal.py','test_full_audit_execution.py','复用须同时匹配引擎、测试绑定、运行环境、源码未变和日志哈希'),
 ('F21','历史空名单完成被界面显示为失败','web/jobs.js','test_full_audit_ui.py','空名单作为研究结果说明，真正失败门禁仍显示错误'),
 ('F22','无效设置先改并发数且接受小数或布尔值','lab/server.py','test_full_audit_ui.py','校验完整研究设置和整数范围后再写入'),
 ('F23','空白CSV或JSONL记录破坏结果分页','lab/server.py','test_full_audit_ui.py','逐记录使用实际索引位置，保留引号内原始换行'),
 ('F24','调度停止后仍可能启动排队任务','lab/server.py','test_full_audit_ui.py','入口、取得锁及创建进程前检查停止标记'),
 ('F25','服务启动异常遗漏套接字清理','lab/server.py','test_full_audit_ui.py','端点发布和浏览器启动纳入资源清理范围；浏览器失败不终止服务'),
 ('F26','已核验实例因浏览器失败被误判端口冲突','lab/launcher.py','test_full_audit_ui.py','保留实例身份核验结果，浏览器错误单独提示'),
 ('F27','端口冲突分支跳过Python和依赖核对','启动工作台.bat','additional_checks.py','统一经过环境检查，再由服务选择空闲端口；仅静态控制流验证'),
 ('F28','清空任务选择后仍显示旧任务指标和翻页按钮','web/app.js','additional_checks.py','清空全部结果区域、页码和翻页状态，并取消旧请求'),
 ('F29','整场上限24时最终报告遗漏组合共同指标','lab/reporting.py','test_full_audit_review.py','识别无额外整场限制的合法政策名称')
]

LAB_NAMES='''__init__ catalog cli common contracts data diagnostics engine_selftest execution_economics feature_extensions features fleet global_bound handoff_assets historical_pricing holdout launcher locking maintenance migration mining normalization packaging paper_execution paper_runner paper_verification pipeline pool_diagnostics portfolio_replay portfolio_search release reporting research_standard research_validation resources review_cache rules search_integrity search_plan selection server sparse_plan standard_atoms standard_cli standard_columns standard_feature_cache standard_features standard_kernels standard_masks standard_mining standard_review standard_search standard_spec store verify walk_forward workflow_gates workflow_journal'''.split()
FULL_READ={f'lab/{name}.py' for name in LAB_NAMES}|{
 'app.py','build_source_release.py','config/default_config.json','requirements.txt','installer.lock',
 'web/app.js','web/jobs.js','web/index.html','web/style.css','启动工作台.bat','start_windows.bat',
 'start_existing_python.bat','start_linux.sh','setup_windows.ps1','demo/generate_demo.py',
 'README_先读.md','本版实测与未完成项.md','docs/核心指导思想与产品目标.md',
 'docs/标准流程与模拟接口.md','docs/原始数据结构说明.md','docs/当前交付范围.md',
 'validation/README.md','validation/frozen_regression.py','validation/check_dependencies.py','validation/update_sbom.py'}
PROOFS={
 'engine':'final/frozen_regression.json',
 'historical_empty':'final/historical_empty/acceptance.json',
 'historical_full':'final/historical_full/acceptance.json',
 'historical_limited':'final/historical_limited/acceptance.json',
 'legacy_empty':'final/legacy_validation/standard_empty_acceptance.json',
 'legacy_nonempty':'final/legacy_validation/standard_nonempty_acceptance.json',
 'dependencies':'final/dependencies.json',
 'additional':'final/additional_checks.json',
 'browser':'final/ui/ui_validation.json'}

ISSUES.append(('F30','手工传入执行时间早于首信号仍预留模拟资金','lab/paper_execution.py','test_full_audit_execution_clock.py','首信号时间必须为整数，执行时间不得更早；整批校验先于任何预留'))
PROOFS={name:(path.replace('final/','final_clock/',1) if name!='browser' else 'final/ui_attempt_4/ui_validation.json') for name,path in PROOFS.items()}

def main():
    current={p.relative_to(ROOT).as_posix():sha(p) for p in release_files()}
    with zipfile.ZipFile(AUDIT/'baseline/source.zip') as archive:
        baseline=json.loads(archive.read('release_manifest.json'))['files']
    engine=source_fingerprint();release=release_fingerprint()
    proofs={name:read_json(AUDIT/path,{'status':'NOT_RUN'}) for name,path in PROOFS.items()}
    final_engine=proofs['engine']
    frozen_matches=(engine==final_engine.get('engine_hash') and release==final_engine.get('release_hash')
                    and current==final_engine.get('source_manifest'))
    statuses={name:p.get('status','NOT_RECORDED') for name,p in proofs.items()}
    bindings={name:p.get('engine_hash')==engine and p.get('release_hash')==release for name,p in proofs.items() if name!='dependencies'}
    locks=proofs['dependencies'].get('locks',{})
    bindings['dependencies']=bool(locks) and all((ROOT/name).is_file() and sha(ROOT/name)==value for name,value in locks.items())
    verified=all(value=='PASS' for value in statuses.values()) and frozen_matches and all(bindings.values())
    status='PASS' if verified else 'PARTIAL'
    evidence={name:{'path':path,'status':statuses[name],'binding_matches':bindings[name],
                        'sha256':sha(AUDIT/path) if (AUDIT/path).is_file() else None} for name,path in PROOFS.items()}
    changes={'changed':[p for p in baseline if p in current and baseline[p]!=current[p]],
             'added':[p for p in current if p not in baseline],'removed':[p for p in baseline if p not in current],
             'baseline_snapshot_sha256':sha(AUDIT/'baseline/source.zip'),
             'current_matches_frozen_manifest':frozen_matches}
    atomic_json(AUDIT/'本轮源码差异.json',changes)
    actual_lab={p for p in current if p.startswith('lab/') and p.endswith('.py')}
    unread_lab=sorted(actual_lab-FULL_READ)
    with (AUDIT/'逐文件审查覆盖.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['文件','审查范围','本轮变化','当前SHA256'])
        for path in sorted(current):
            level='当前源码全文阅读及相关调用审查' if path in FULL_READ else '回归实际执行；不声称所有测试源码逐行审阅' if path.startswith('tests/') else '哈希完整性或历史参考；非逐行审查范围'
            change='新增' if path not in baseline else '已修改' if baseline[path]!=current[path] else '与本轮开始相同'
            writer.writerow([path,level,change,current[path]])
    issues=[{'id':i,'problem':p,'files':f.split(';'),'tests':t.split(';'),'repair':r,
             'status':'FIXED_STATIC_CHECK' if i=='F27' else 'FIXED_VERIFIED' if statuses['engine']=='PASS' else 'FIXED_PENDING_FINAL_CHECK'} for i,p,f,t,r in ISSUES]
    atomic_json(AUDIT/'问题关闭清单.json',issues)
    git=subprocess.run(['git','-c','core.quotepath=false','status','--short','--branch'],cwd=ROOT,capture_output=True,check=True)
    (AUDIT/'最终Git状态.txt').write_bytes(git.stdout)
    state={'status':status,'scope':'CURRENT_SOURCE_AUDIT_NOT_FORMAL_RELEASE_OR_REAL_LEAGUE_CERTIFICATE',
           'engine_hash':engine,'release_hash':release,'core_modules':len(actual_lab),'unread_core_modules':unread_lab,
           'issue_groups':len(ISSUES),'engine_tests':final_engine.get('tests'),'evidence':evidence,
           'current_matches_frozen_manifest':frozen_matches,'evidence_bindings_match':all(bindings.values()),'real_orders_sent':0,'changes':changes}
    atomic_json(AUDIT/'审查状态.json',state)
    lines=['# 全面代码审查与修复验收报告','',f'状态：**{status}**。这是当前工作区的软件审查记录，不是正式发布标签或真实联赛策略证书。','',
           f'按根因合并记录 {len(ISSUES)} 项问题。核心 Python 模块 {len(actual_lab)} 个，当前源码全文审查未覆盖项：{unread_lab or "无"}。',
           '网页、入口和当前启动安装脚本已阅读；相关测试执行与源码阅读分别记录。旧冻结版本、历史输出、大型原始数据和第三方依赖源码没有逐行复审。',
           '', '## 实际证据','', '| 验收 | 状态 | 文件 |','|---|---|---|']
    lines.extend(f'| {name} | {value["status"]} | {value["path"]} |' for name,value in evidence.items())
    lines += ['',f'隔离引擎回归实际项数：{final_engine.get("tests")}。不把另外的整链测试混入该数。',
              '完整非空用例使用90场合成比赛、三年、16方向、compact_v1全部冻结表达。limited用例刻意设置每方向10节点，正确返回PARTIAL才算软件验收通过。',
              '五种计价情景中s0—s3进行流式协调器核对；s4为进球前拒单的档案计价及持久化模拟账核对，不冒称实时提前知道进球。',
              '', '## 逐项修复','']
    lines.extend(f'### {i} {problem}\n\n{repair}。\n\n源码：{files}。验证：{tests}。\n' for i,problem,files,tests,repair in ISSUES)
    lines += ['## 源码与原工作保留','',f'当前引擎：{engine}',f'当前发布文件集合：{release}',
              f'本轮基线之后修改 {len(changes["changed"])} 个发布文件，新增 {len(changes["added"])} 个，删除 {len(changes["removed"])} 个。逐项见本轮源码差异.json。',
              f'当前源码与最终冻结验收清单一致：{frozen_matches}。旧工作区已有改动保留，未reset、clean、提交、打标签或推送。',
              '', '## 边界与后续使用','',
              '不宣称不存在尚未发现的缺陷。实际完成范围由逐文件覆盖和上述可复现验收限定。未验证全新Windows安装、Linux实机、多台电脑实际部署、数百联赛容量及第三方行情/成交/账户接入。',
              '尚未重跑真实联赛，未量化这些修复对旧策略名单和净胜的影响。原有真实任务与数据没有改变，真实下单和第二笔保持关闭。',
              '当前仍为0.6.3工作区未发布修订。旧任务缓存和断点不能混入新引擎；需要用旧冻结源码继续，或按迁移入口建立新任务。',
              '启动脚本端口分支经过静态控制流检查，未实际执行重装依赖。依赖公告检查仅反映检查时官方数据，不是永无漏洞保证。',
              '外部模拟时间边界已实际复现并列为F30：signal_ts=10、execution_ts=9时旧版仍预留1单位。补齐时间类型及先后检查后重新验收；第一次512项及五条链路证据保存在final，最终证据位于final_clock。',
              '', '## 文件导航','',
              '审查状态.json：机器可读最终状态和证据哈希。问题关闭清单.json：按根因合并的修复清单。逐文件审查覆盖.csv：源码、测试与历史参考的审查范围。最终Git状态.txt：交付时本地改动状态。',
              'final_clock/：最后修复后的实际回归、各整链和依赖证据。final/：前一轮证据及独立浏览器记录。baseline/source.zip：修改前源码快照。run_acceptance.py和additional_checks.py：额外验收入口。']
    (AUDIT/'审查报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({k:state[k] for k in ('status','core_modules','unread_core_modules','issue_groups','engine_tests','current_matches_frozen_manifest')},ensure_ascii=False))

if __name__=='__main__':main()
