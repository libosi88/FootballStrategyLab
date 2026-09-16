"""Consolidate source-bound evidence for the review-findings fix round without modifying the product."""
import json,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from lab.common import atomic_json,read_json,sha,source_fingerprint
from lab.release import release_files,release_fingerprint,build_snapshot
AUDIT=Path(__file__).resolve().parent
EVIDENCE={'frozen_regression':('final/frozen_regression.json','隔离冻结回归'),
          'validation_empty_pipeline':('final/standard_empty_acceptance.json','验证模式16方向空结果整链'),
          'validation_nonempty_pipeline':('final/standard_nonempty_acceptance.json','验证模式16方向非空有限整链'),
          'historical_empty':('historical_empty/acceptance.json','历史模式空结果整链'),
          'historical_full':('historical_full/acceptance.json','历史模式完整搜索整链'),
          'historical_limited':('historical_limited/acceptance.json','历史模式每方向10节点有限整链'),
          'additional_checks':('final/additional_checks.json','独立结算与界面处理器核对')}
INSTALLER_FILES=('setup_windows.ps1','requirements.txt','requirements.lock','installer.lock')

def detail(report):
    parts=[str(report.get('status'))]
    if report.get('tests') is not None:parts.append(f"{report['tests']} 项测试"+(f"，{report['skipped']} 项跳过" if report.get('skipped') else ''))
    if report.get('state'):parts.append(f"{report['state']}，主名单 {report.get('selected')} 条")
    if report.get('independent_settlement_cases'):parts.append(f"{report['independent_settlement_cases']} 个结算用例")
    if report.get('seconds') is not None:parts.append(f"{report['seconds']:.0f} 秒")
    return '；'.join(parts)

def main():
    reports={key:read_json(AUDIT/path,{}) for key,(path,_) in EVIDENCE.items()}
    install=read_json(AUDIT/'clean_install.json',{})
    install_log=AUDIT/'clean_install.log'
    offline=install.get('status')!='PASS' and install_log.is_file() and 'getaddrinfo failed' in install_log.read_text(encoding='utf-8',errors='replace')
    baseline=read_json(AUDIT/'baseline/source_identity.json')
    engine=source_fingerprint();release=release_fingerprint()
    files={p.relative_to(ROOT).as_posix():sha(p) for p in release_files()}
    bound={key:r.get('engine_hash')==engine and r.get('release_hash')==release for key,r in reports.items()}
    manifests=[reports[key].get('source_manifest') for key in ('frozen_regression','validation_empty_pipeline','validation_nonempty_pipeline')]
    manifests+=[read_json(AUDIT/key/'source_before.json',{}).get('files') for key in ('historical_empty','historical_full','historical_limited')]
    matched=all(m==files for m in manifests)
    fmt=subprocess.run(['git','-c','core.whitespace=cr-at-eol','diff','--check'],cwd=ROOT,capture_output=True)
    (AUDIT/'diff_check.log').write_bytes(fmt.stdout+fmt.stderr)
    git=subprocess.run(['git','-c','core.quotepath=false','status','--short','--branch'],cwd=ROOT,capture_output=True,check=True)
    (AUDIT/'最终Git状态.txt').write_bytes(git.stdout)
    with zipfile.ZipFile(AUDIT/'baseline/source.zip') as z:before=json.loads(z.read('release_manifest.json'))['files']
    changes={'modified':[p for p in before if p in files and files[p]!=before[p]],'added':[p for p in files if p not in before],'removed':[p for p in before if p not in files]}
    ok=all(bound.values()) and all(r.get('status')=='PASS' for r in reports.values()) and matched and fmt.returncode==0
    report={'status':'PASS' if ok else 'PARTIAL','scope':'REVIEW_FINDINGS_FIX_AND_ACCEPTANCE_NOT_REAL_LEAGUE_RESEARCH',
            'engine_hash':engine,'release_hash':release,'baseline':baseline,
            'evidence_status':{key:r.get('status') for key,r in reports.items()},'evidence_bindings_match':bound,
            'frozen_source_matches':matched,'diff_check_exit_code':fmt.returncode,'changes':changes,
            'clean_install':{'status':install.get('status'),'reason':'DNS_UNAVAILABLE_PYPI_NOT_RESOLVED' if offline else None,
                             'bound':install.get('engine_hash')==engine and install.get('release_hash')==release,'required_for_status':False,
                             'installer_files_changed':[p for p in INSTALLER_FILES if p in changes['modified'] or p in changes['removed']]},
            'not_rerun':['browser UI smoke: web/ unchanged; server.py only lost an unused import',
                         'dependency advisories: lock files unchanged; see validation/delivery_audit_20260915_r1/dependencies.json'],
            'real_tasks_created_or_queued':0,'real_orders_sent':0,'git_committed':False,
            'evidence':{path:sha(AUDIT/path) for path in [*(p for p,_ in EVIDENCE.values()),'clean_install.json','clean_install.log'] if (AUDIT/path).is_file()}}
    snapshot=AUDIT/'final/source_verified.zip'
    if ok and not snapshot.exists():report['source_snapshot']=build_snapshot(snapshot)
    elif snapshot.exists():report['source_snapshot']={'path':str(snapshot),'sha256':sha(snapshot)}
    atomic_json(AUDIT/'修复状态.json',report)
    install_line=detail(install) if install.get('status')=='PASS' or not offline else '已用修正后的脚本重新运行，但本机当前无法解析 pypi.org（getaddrinfo 11001），pip 无法下载，检查未完成。安装脚本与锁文件本轮未改动；validation/delivery_audit_20260915_r1/clean_install.log 显示同一套安装文件的安装与 538 项测试通过，其 FAIL 为正则误判。网络恢复后可将修正脚本放入新的 validation 子目录重跑。'
    lines=['# 审查问题核对修复报告','','状态：'+report['status']+('' if install.get('status')=='PASS' else '（不含全新安装检查，该项未完成，见实际验证）'),'',
      '## 修复',
      '- 清理 lab/ 中未使用的导入和 common.py 一处未使用的赋值；features.py 的 LABEL_FIELDS 被测试从该模块导入，保留。计算逻辑不变，引擎指纹随之改变，sbom.cdx.json 已重新生成。',
      '- 删除与 docs/v3_冻结需求原文.md 字节相同的 docs/原始研究标准v3_本版尚未完整实现.md（已经用户确认）。',
      '- 更正 本版实测与未完成项.md 中 0.6.2 的冻结与标签记录；README 注明使用 .venv 的 Python、补充两条端到端流水线的运行方式并指向本目录；两份 docs 补充当前测试数量。',
      '- tests/test_v031.py 捕获命令行测试的标准错误输出，不再打印 MagicMock。',
      '- 修正全新安装检查脚本的正则（validation/delivery_audit_20260915_r1/clean_install.json 的 FAIL 为误判），在本目录重新运行。',
      '','## 核对后未修改',
      '- 同场同市场跨分钟反向成交：符合 v3 冻结政策（冻结需求第186行），改动须另立政策版本。',
      '- 槽位口径、memmap 物化、比赛状态残留、SBOM 含 pip、审查后发布指纹漂移：经核实不成立。',
      '- 清单文件排除计算与表头重复检测的复杂度：实际规模下可忽略。',
      '','## 实际验证',
      *[f'- {label}：{detail(reports[key])}' for key,(_,label) in EVIDENCE.items()],
      '- 全新虚拟环境安装与默认单元测试：'+install_line,
      '- 各项证据绑定当前引擎与发布文件集合：'+str(all(bound.values()))+'；当前源码与回归、整链验收的源码清单逐文件一致：'+str(matched)+'；git diff --check 退出码 '+str(fmt.returncode)+'。',
      '','## 边界与文件',
      '修改前引擎：'+baseline['engine_hash']+'；修改前发布文件集合：'+baseline['release_hash']+'。',
      '当前引擎：'+engine+'；当前发布文件集合：'+release+'。',
      '未重跑浏览器界面烟测（web/ 未改动，server.py 只删除未使用的导入）；未重跑依赖漏洞核对（锁文件未改动，最近结果见 validation/delivery_audit_20260915_r1/dependencies.json）。',
      '未运行真实联赛搜索，未创建或排队任务，未提交 Git 或新增标签。',
      'baseline/source.zip：修改前源码；final/source_verified.zip：本轮通过验收的源码；修复状态.json：逐项结果、哈希与改动文件清单。']
    (AUDIT/'修复报告.md').write_text(chr(10).join(lines)+chr(10),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('status','engine_hash','release_hash','evidence_status','frozen_source_matches','diff_check_exit_code','clean_install')},ensure_ascii=False))
    return 0 if ok else 1

if __name__=='__main__':raise SystemExit(main())
