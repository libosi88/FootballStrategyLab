import argparse,json,sys
from .common import *
from .data import inspect
from .store import Store

def rebuild_options(parser):
 parser.add_argument('--profile',choices=['smoke','routine','expanded','standard'],help='显式选择规格并记录标准参数的调整')
 parser.add_argument('--config',help='显式覆盖新任务的配置JSON，默认保留旧模式和预算')
 parser.add_argument('--dry-run',action='store_true',help='核对输入和参数差异，不创建或排队任务')

def rebuild_overrides(path):
 config=read_json(path) if path else None
 if path and not isinstance(config,dict):raise ValueError('迁移配置文件必须存在且为JSON对象')
 return config

def main():
 p=argparse.ArgumentParser(description=f'足球策略研究工作台 {VERSION}（有限范围、本地、模拟）')
 sub=p.add_subparsers(dest='cmd',required=True)
 q=sub.add_parser('inspect');q.add_argument('inputs',nargs='+');q.add_argument('--workspace',default=str(ROOT/'workspace'))
 q=sub.add_parser('fleet-plan');q.add_argument('inputs',nargs='+');q.add_argument('--nodes',type=int,required=True);q.add_argument('--company');q.add_argument('--config');q.add_argument('--output',required=True)
 q=sub.add_parser('fleet-import');q.add_argument('plan');q.add_argument('--data-root',required=True);q.add_argument('--workspace',default=str(ROOT/'workspace'));q.add_argument('--queue',action='store_true')
 q=sub.add_parser('run');q.add_argument('inputs',nargs='+');q.add_argument('--league',required=True);q.add_argument('--company');q.add_argument('--profile',choices=['smoke','routine','expanded','standard']);q.add_argument('--config');q.add_argument('--unbounded',action='store_true',help='不使用命令行默认安全上限（每方向200000节点、输出20000MiB、单次时长不限）；界面新建任务本来就不设这些上限');q.add_argument('--workspace',default=str(ROOT/'workspace'))
 q=sub.add_parser('worker');q.add_argument('--workspace',required=True);q.add_argument('--job',required=True)
 q=sub.add_parser('import-audit');q.add_argument('archive');q.add_argument('--workspace',default=str(ROOT/'workspace'))
 q=sub.add_parser('rebuild-job');q.add_argument('--workspace',default=str(ROOT/'workspace'));q.add_argument('--job',required=True);rebuild_options(q)
 q=sub.add_parser('rebuild-stale');q.add_argument('--workspace',default=str(ROOT/'workspace'));q.add_argument('--queue',action='store_true');q.add_argument('--job',action='append',help='仅处理指定任务，可重复；省略则检查全部任务');rebuild_options(q)
 q=sub.add_parser('resume-jobs');q.add_argument('--workspace',default=str(ROOT/'workspace'))
 q=sub.add_parser('cleanup-workspace');q.add_argument('--workspace',default=str(ROOT/'workspace'));q.add_argument('--apply',action='store_true')
 q=sub.add_parser('verify');q.add_argument('directory')
 q=sub.add_parser('serve');q.add_argument('--port',type=int,default=8765);q.add_argument('--workspace',default=str(ROOT/'workspace'));q.add_argument('--no-browser',action='store_true')
 q=sub.add_parser('export-candidates');q.add_argument('directory');q.add_argument('--output',required=True)
 q=sub.add_parser('evaluate-frozen');q.add_argument('--rules',required=True);q.add_argument('inputs',nargs='+');q.add_argument('--output',required=True)
 q=sub.add_parser('walk-forward');q.add_argument('--workspace',default=str(ROOT/'workspace'));q.add_argument('--job',required=True);q.add_argument('--cutoff',action='append',required=True);q.add_argument('--test-end',action='append',required=True);q.add_argument('--wait-seconds',type=int,default=0)
 q=sub.add_parser('paper-stream');q.add_argument('--rules',required=True);q.add_argument('--state',required=True);q.add_argument('--input',default='-');q.add_argument('--output');q.add_argument('--archive-history',action='store_true');q.add_argument('--auto-fill',action='store_true');q.add_argument('--checkpoint-every',type=int,default=1,help='每N条消息持久化一次协调器状态；大于1时崩溃后须重发最后检查点之后的报价')
 a=p.parse_args()
 if a.cmd=='inspect':print(json.dumps(inspect(a.inputs,a.workspace),ensure_ascii=False,indent=2))
 elif a.cmd=='fleet-plan':
  from .fleet import make_plans
  config=read_json(a.config) if a.config else {}
  if not isinstance(config,dict):raise ValueError('配置文件必须存在且为JSON对象')
  print(json.dumps(make_plans(a.inputs,a.output,a.nodes,a.company,config),ensure_ascii=False,indent=2))
 elif a.cmd=='fleet-import':
  from .fleet import import_plan
  print(json.dumps(import_plan(a.plan,a.data_root,a.workspace,a.queue),ensure_ascii=False,indent=2))
 elif a.cmd=='rebuild-job':
  from .migration import rebuild_job
  print(json.dumps(rebuild_job(a.workspace,a.job,a.profile,config_overrides=rebuild_overrides(a.config),dry_run=a.dry_run),ensure_ascii=False,indent=2))
 elif a.cmd=='rebuild-stale':
  from .migration import rebuild_stale_jobs
  result=rebuild_stale_jobs(a.workspace,a.queue,a.profile,config_overrides=rebuild_overrides(a.config),job_ids=a.job,dry_run=a.dry_run)
  print(json.dumps(result,ensure_ascii=False,indent=2))
  return 1 if result['failed'] else 0
 elif a.cmd=='resume-jobs':
  print(json.dumps({'resumed':Store(a.workspace).resume_all()},ensure_ascii=False,indent=2))
 elif a.cmd=='cleanup-workspace':
  from .maintenance import cleanup_workspace
  print(json.dumps(cleanup_workspace(a.workspace,a.apply),ensure_ascii=False,indent=2))
 elif a.cmd=='run':
  from .pipeline import run
  config=read_json(a.config) if a.config else {}
  if not isinstance(config,dict):raise ValueError('配置文件必须存在，且内容为JSON对象')
  config=dict(config)
  if a.profile is not None:config['profile']=a.profile
  if a.company is not None:config['company']=a.company
  if not a.unbounded:
   # Command-line runs keep finite trial budgets unless the config file sets them; the workstation UI defaults to unlimited.
   for key,value in TRIAL_BUDGETS.items():config.setdefault(key,value)
  config=check_config(config)
  store=Store(a.workspace);man=inspect(a.inputs,a.workspace);jid=store.create(a.league,config,man);print('任务：'+jid,flush=True)
  r=run(a.workspace,jid)
  if r is None:
   if store.get(jid)['status']=='PAUSED':
    print('任务已按预算或暂停请求保存断点并暂停: '+jid+'；可在工作台或用 resume-jobs 续跑',file=sys.stderr);return 2
   print('任务已登记但本进程未能领取执行（工作区占用或任务已被其他进程领取）: '+jid,file=sys.stderr);return 1
  print(json.dumps({k:r[k] for k in ('selected','state','v3_status')},ensure_ascii=False))
 elif a.cmd=='worker':
  from .pipeline import run
  if run(a.workspace,a.job) is None:
   # Unclaimed (slot busy, parallel limit, or another worker took it): exit 75 so the
   # scheduler keeps a still-queued job instead of marking a healthy wait as ERROR.
   try:
    if Store(a.workspace).get(a.job)['status']=='QUEUED':return 75
   except ValueError:return 75
 elif a.cmd=='import-audit':
  from .migration import import_audit_bundle
  print(json.dumps(import_audit_bundle(a.archive,a.workspace),ensure_ascii=False,indent=2))
 elif a.cmd=='verify':
  from .verify import verify,verification_passed
  r=verify(a.directory);print(json.dumps(r,ensure_ascii=False,indent=2));return 0 if verification_passed(r) else 1
 elif a.cmd=='export-candidates':
  from .standard_cli import export_candidates
  print(json.dumps(export_candidates(a.directory,a.output),ensure_ascii=False))
 elif a.cmd=='evaluate-frozen':
  from .standard_cli import evaluate_packet
  print(json.dumps(evaluate_packet(a.rules,a.inputs,a.output),ensure_ascii=False,indent=2))
 elif a.cmd=='walk-forward':
  from .walk_forward import run_walk_forward
  if len(a.cutoff)!=len(a.test_end):raise ValueError('每个训练截止日必须对应一个测试结束日')
  print(json.dumps(run_walk_forward(a.workspace,a.job,[{'cutoff':c,'test_end':e} for c,e in zip(a.cutoff,a.test_end)],wait_seconds=a.wait_seconds),ensure_ascii=False,indent=2))
 elif a.cmd=='paper-stream':
  from .standard_cli import paper_stream
  result=paper_stream(a.rules,a.state,a.input,a.output,a.archive_history,a.auto_fill,a.checkpoint_every)
  if result['rejected_messages']:print(f"已拒绝并记录 {result['rejected_messages']} 条异常消息；详见输出中的 MESSAGE_REJECTED 与模拟账 alarms",file=sys.stderr)
 else:
  from .server import serve
  serve(a.workspace,a.port,not a.no_browser)
 return 0
if __name__=='__main__':raise SystemExit(main())
