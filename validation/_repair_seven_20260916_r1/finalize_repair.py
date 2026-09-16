"""Seal a compact repair handoff only after every declared check actually passed."""
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

BASE=Path(__file__).resolve().parent
ROOT=BASE.parent.parent
sys.path.insert(0,str(ROOT))
from lab.common import source_fingerprint,sha
from lab.release import release_fingerprint

report=json.loads((BASE/'repair_result.json').read_text(encoding='utf-8'))
required=['syntax_and_locked_dependencies','pip_check','targeted','frozen_regression',
          'validation_empty','validation_nonempty','historical_empty','historical_full','historical_limited']
if report['status']!='PASS_SCOPED_REPAIR' or any(report['checks'].get(k,{}).get('status')!='PASS' for k in required):
    raise SystemExit('Acceptance is unfinished or failed; no passing repair handoff is published.')
if source_fingerprint()!=report['engine_hash'] or release_fingerprint()!=report['release_hash']:
    raise SystemExit('Source changed after acceptance; do not certify the changed tree.')
out=Path(report['output_directory']).resolve()
if BASE not in out.parents:raise ValueError('Unrecognized evidence directory')
snapshot=out/'verified_source.zip'
with zipfile.ZipFile(snapshot) as z:
    if z.testzip() is not None:raise ValueError('Source snapshot CRC failed')
    for name,expected in report['source_manifest'].items():
        if hashlib.sha256(z.read(name)).hexdigest()!=expected:raise ValueError('Snapshot source identity differs: '+name)

titles={'syntax_and_locked_dependencies':'语法、锁定依赖与SBOM身份','pip_check':'依赖一致性 pip check',
        'targeted':'新增专项回归（包含在完整回归中）','frozen_regression':'完整冻结引擎回归',
        'validation_empty':'验证模式空结果整链','validation_nonempty':'验证模式非空有限整链',
        'historical_empty':'历史模式空结果整链','historical_full':'历史模式完整紧凑语法整链',
        'historical_limited':'历史模式预算受限整链（正确保留PARTIAL状态）'}
rows=[]
for name in required:
    check=report['checks'][name]
    count=check.get('tests') or (check.get('detail') or {}).get('tests')
    rows.append('| '+titles[name]+' | PASS | '+(str(count)+' 项' if count is not None else '按对应报告核对')+' |')
text='''# FootballStrategyLab 修复与验收报告（2026-09-16）

状态：**PASS_SCOPED_REPAIR**。七项已复现缺陷及相邻同刻批次问题已修复；本结论认证下列源码和测试范围，不代表所有潜在问题不存在，也不认证真实联赛盈利或原始 v3_full 搜索已全部完成。

## 源码身份

内部版本：0.6.3（2026-09-16 修订）。

引擎指纹：`ENGINE_HASH`

完整发布指纹：`RELEASE_HASH`

源码已写入原项目；`verified_source.zip` 是同一版本的可恢复快照，不是另一个独立试用分支。既有 Git 工作树改动未被自动提交或打标签。

## 修复内容

| 项目 | 修复 |
|---|---|
| S01 时间回退 | 拒单与观察时间原子保存，无信号批次也保存时间边界；恢复合并持久化最大时间，并补齐旧账遗漏时钟。 |
| S02 撤余后结算 | 已成交部分可在撤销剩余量后按原数量确认结算；不允许增加成交、无确认或重开终态订单。 |
| F01 调度退出 | 分离调度错误与错误日志写入错误；调度保持重试，并向接口、界面报告降级和恢复。 |
| F02 A/B 不同源 | 两包从同一份校验过的完整源码快照构建；内容逐文件核对，构建期间源码漂移时拒绝发布。 |
| F03 白名单失效 | 两包软件源码均按发布白名单封装，校验实际成员集合与哈希，排除未登记的无害测试.env/.pem文件。 |
| F04 旧包导入 | 缺失 research_objective 的旧包按 validation 兼容；显式模式保留，导入保持暂停并去重。 |
| F05 旧任务暂停 | 旧版本运行或排队任务保留暂停入口；不开放旧引擎断点直接续算。 |
| R08 同刻批次 | 同场同实际决策时间的积压信号合并成一个批次，统一按冻结优先级、相反方向和原本金上限处理。 |

旧账修复改为一次性逐行扫描；不前进的时钟不重复改写，空边界集合不启动事务。没有改变香港盘净水结算、历史取价、搜索范围、盈利门槛或真实执行权限，也不承诺未经基准测试的提速比例。

## 实际验收

| 检查 | 结果 | 数量或范围 |
|---|---|---|
CHECK_ROWS

新增专项的数量已包含在完整引擎回归内，不重复相加。五条整链使用合成数据，调用真实生产流程、真实ZIP打包及解压后的自检/回放。预算受限用例的测试通过，意味着它正确报告部分结果，不意味着其搜索已完成。

第一轮完整回归发现旧夹具缺少盘口、未生成交接前置资产，以及缓存版本断言自相矛盾。已修正这些测试的前置条件，未放宽生产校验。第一轮失败记录保存在 final_v1；后续通过证据使用新目录，不覆盖失败历史。

## 交付内容与恢复

本证据包包含已验证源码快照、修复前基线、针对该基线的差异补丁、验证脚本及检查报告和日志；不重复封装真实联赛数据。完整合成任务的逐笔材料保留在本机对应验收目录。

`changes_vs_repair_baseline.patch` 是相对本轮修复前工作树的补丁，不是相对旧 Git 标签。当前项目已应用修复，不应重复打补丁。恢复到另一目录时，应从 verified_source.zip 解压，并按包内启动说明使用锁定依赖。

需要复验时，将 tools 中的脚本放回源码树 validation/_repair_seven_20260916_r1/，并将 baseline 中的两个文件放在该目录。新的 --output-dir 必须使用该目录下未存在的子目录，避免覆盖旧证据。

修改源文件不会自动重启已有工作台服务。关闭旧界面服务后再启动，才会加载服务端修复；仅刷新网页不够。旧任务使用其冻结源码，或先 dry-run 再重建，不得强改引擎指纹绕过保护。

## 范围边界

本轮没有重新挖掘真实联赛，没有认证原始 v3_full 的全部搜索范围，没有新做联网漏洞扫描或干净机器安装。界面验证包括实际 JavaScript 渲染和隔离 HTTP 操作，不是全浏览器视觉验收。真实下单和第二笔保持关闭。
'''
text=text.replace('ENGINE_HASH',report['engine_hash']).replace('RELEASE_HASH',report['release_hash']).replace('CHECK_ROWS','\n'.join(rows))
md=BASE/'最终修复报告.md'
if md.exists():raise FileExistsError('Final handoff already exists; preserve it')
md.write_text(text,encoding='utf-8')
members={
    '最终修复报告.md':md,'verified_source.zip':snapshot,
    'changes_vs_repair_baseline.patch':out/'changes_vs_repair_baseline.patch',
    'repair_result.json':BASE/'repair_result.json',
    'baseline/baseline_identity.json':BASE/'baseline_identity.json',
    'baseline/baseline_source.zip':BASE/'baseline_source.zip',
    'tools/run_validation.py':BASE/'run_validation.py',
    'tools/finalize_repair.py':Path(__file__).resolve(),
}
for p in out.iterdir():
    if p.is_file() and p.suffix in ('.json','.log'):members['evidence/'+p.name]=p
for check in report['checks'].values():
    value=check.get('evidence')
    if not value:continue
    path=Path(value).resolve()
    if out not in path.parents:raise ValueError('Unexpected external check evidence')
    for p in path.parent.iterdir():
        if p.is_file() and p.suffix in ('.json','.log'):members['evidence/'+p.relative_to(out).as_posix()]=p
for previous in sorted(BASE.glob('final_v*')):
    if previous.resolve()==out or not previous.is_dir():continue
    for folder in (previous,previous/'regression'):
        if not folder.is_dir():continue
        for p in folder.iterdir():
            if p.is_file() and p.suffix in ('.json','.log'):
                members['prior_attempts/'+p.relative_to(BASE).as_posix()]=p
inventory={name:sha(path) for name,path in members.items()}
target=BASE/'FSL_修复与验收证据_20260916.zip';temporary=target.with_suffix('.zip.tmp')
if target.exists() or temporary.exists():raise FileExistsError('Do not overwrite handoff archives')
with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for name,path in members.items():z.write(path,name)
    z.writestr('bundle_manifest.json',json.dumps(inventory,ensure_ascii=False,indent=2))
with zipfile.ZipFile(temporary) as z:
    if z.testzip() is not None:raise ValueError('Repair evidence bundle CRC failed')
    for name,expected in inventory.items():
        if hashlib.sha256(z.read(name)).hexdigest()!=expected:raise ValueError('Repair evidence content changed')
os.replace(temporary,target)
certificate={'status':'PASS_SCOPED_REPAIR','engine_hash':report['engine_hash'],'release_hash':report['release_hash'],
             'bundle':str(target),'bundle_sha256':sha(target),'source_snapshot':str(snapshot),'source_sha256':sha(snapshot),
             'report':str(md),'members':len(inventory),'bytes':target.stat().st_size,'all_declared_checks_passed':True}
(BASE/'修复交付清单.json').write_text(json.dumps(certificate,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(certificate,ensure_ascii=False,indent=2))
