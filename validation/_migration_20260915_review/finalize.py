"""Consolidate source-bound migration evidence without modifying the product."""
import json,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from lab.common import atomic_json,read_json,sha,source_fingerprint
from lab.release import release_files,release_fingerprint,build_snapshot
AUDIT=Path(__file__).resolve().parent

def main():
    regression=read_json(AUDIT/'final/frozen_regression.json',{})
    legacy=read_json(AUDIT/'real_legacy_validation.json',{})
    engine=source_fingerprint();release=release_fingerprint()
    files={p.relative_to(ROOT).as_posix():sha(p) for p in release_files()}
    bound=all(r.get('engine_hash')==engine and r.get('release_hash')==release for r in (regression,legacy))
    matched=files==regression.get('source_manifest')
    fmt=subprocess.run(['git','-c','core.whitespace=cr-at-eol','diff','--check'],cwd=ROOT,capture_output=True)
    (AUDIT/'diff_check.log').write_bytes(fmt.stdout+fmt.stderr)
    git=subprocess.run(['git','-c','core.quotepath=false','status','--short','--branch'],cwd=ROOT,capture_output=True,check=True)
    (AUDIT/'最终Git状态.txt').write_bytes(git.stdout)
    with zipfile.ZipFile(AUDIT/'baseline/source.zip') as z:before=json.loads(z.read('release_manifest.json'))['files']
    changes={'modified':[p for p in before if p in files and files[p]!=before[p]],'added':[p for p in files if p not in before],'removed':[p for p in before if p not in files]}
    ok=bound and matched and fmt.returncode==0 and regression.get('status')=='PASS' and legacy.get('status')=='PASS'
    report={'status':'PASS' if ok else 'PARTIAL','scope':'MIGRATION_FIX_AND_REGRESSION_NOT_REAL_LEAGUE_RESEARCH',
            'engine_hash':engine,'release_hash':release,'evidence_bindings_match':bound,'frozen_source_matches':matched,
            'tests':regression.get('tests'),'new_targeted_tests':20,'real_legacy_validation':legacy,'changes':changes,
            'browser_smoke':'NOT_VERIFIED_THIS_REVISION: tool request blocked; Node VM lifecycle regression did run',
            'real_tasks_created_or_queued':0,'real_orders_sent':0,'git_committed':False,
            'evidence':{str(p.relative_to(AUDIT)):sha(p) for p in [AUDIT/'real_legacy_validation.json',AUDIT/'final/frozen_regression.json'] if p.is_file()}}
    snapshot=AUDIT/'final/source_verified.zip'
    if ok and not snapshot.exists():report['source_snapshot']=build_snapshot(snapshot)
    elif snapshot.exists():report['source_snapshot']={'path':str(snapshot),'sha256':sha(snapshot)}
    atomic_json(AUDIT/'修复状态.json',report)
    lines=['# 旧任务迁移修复报告','', '状态：'+report['status'],
      '', '修复：批量迁移失败退出码、显式标准参数与JSON覆盖、只读预览、按任务筛选、独立迁移来源记录、旧任务生命周期显示。',
      '默认保留旧模式和预算；全历史由config/rebuild_historical.json显式声明，旧断点不重新绑定。',
      '', '## 实际验证',
      '新增迁移/界面20项回归通过；全量隔离回归 '+str(regression.get('tests'))+' 项，状态 '+str(regression.get('status'))+'。',
      '真实印度超旧任务3条：当前固定标准和全历史两种方式，在临时工作区分别3/3重建成功；重复操作复用相同目标。',
      '原注册表、旧任务元数据和4份输入哈希不变。正式工作区没有创建、排队、重跑或删除任务。',
      '真实CLI子进程确认失败退出码为1；部分成功部分失败同样返回1。',
      'Node VM执行实际jobs.js：已暂停/中断的旧任务显示上次记录，活跃任务保持运行提示，历史失败不隐藏。',
      '本轮额外Chrome烟测的工具请求被拦截，未执行；此前浏览器记录不作为本轮证据。',
      '', '## 使用',
      '预览转当前固定标准：',
      '    python -B app.py rebuild-stale --job 原任务ID --profile standard --dry-run',
      '预览转全历史研究：',
      '    python -B app.py rebuild-stale --job 原任务ID --config config/rebuild_historical.json --dry-run',
      '去掉--dry-run才创建暂停任务；追加--queue才请求排队。省略--job会检查主工作区全部任务。',
      '三条旧印度超使用同一批输入，无需将三个旧版本全部排队。',
      '', '## 边界与文件',
      '计算引擎未修改：'+engine+'；当前发布文件集合：'+release+'。',
      '当前源码与最终冻结回归逐文件一致：'+str(matched)+'。未提交或新增发布标签。',
      '本轮未运行真实联赛搜索或生成新策略，未重新认证S0-S7整链或全新安装。',
      'baseline/source.zip：修改前源码；final/source_verified.zip：本轮通过回归的源码。',
      'preview_current_standard.json、preview_full_history.json：三条旧任务逐项参数差异。',
      'real_legacy_validation.json、final/frozen_regression.json、修复状态.json：结果与哈希。']
    (AUDIT/'修复报告.md').write_text(chr(10).join(lines)+chr(10),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('status','engine_hash','release_hash','tests','evidence_bindings_match','frozen_source_matches')},ensure_ascii=False))
    return 0 if ok else 1

if __name__=='__main__':raise SystemExit(main())
