"""Test a frozen source tree; explicit empty pipeline uses only synthetic data."""
import sys,subprocess,tempfile,shutil,time,re,json,argparse,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab.common import ROOT,VERSION,sha,atomic_json,source_fingerprint
from lab.release import release_files,release_fingerprint
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output-dir',type=Path,default=ROOT/'validation')
parser.add_argument('--standard-empty',action='store_true',help='Explicit synthetic full-16-branch empty-result pipeline; never real-league jobs')
parser.add_argument('--standard-nonempty',action='store_true',help='Explicit synthetic 90-match, all-16, ten-node-per-direction nonempty pipeline')
args=parser.parse_args();output=args.output_dir.resolve();output.mkdir(parents=True,exist_ok=True)
if args.standard_empty and args.standard_nonempty:parser.error('Select one acceptance scope per invocation')
stem='standard_empty_acceptance' if args.standard_empty else 'standard_nonempty_acceptance' if args.standard_nonempty else 'frozen_regression'
before={p.relative_to(ROOT).as_posix():sha(p) for p in release_files()}
started=time.time();engine=source_fingerprint();release=release_fingerprint()
log=output/(stem+'.log')
with tempfile.TemporaryDirectory(prefix='fsl_frozen_regression_') as td:
    frozen=Path(td)
    for name in before:
        target=frozen/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/name,target)
    copied={name:sha(frozen/name) for name in before}
    if copied!=before:raise RuntimeError('Source changed while freezing')
    with log.open('w',encoding='utf-8') as out:
        command="""import sys,threading,traceback
main_thread=threading.get_ident()
done=threading.Event()
def watch():
    while not done.wait(45):
        frame=sys._current_frames().get(main_thread)
        if frame is not None:print('\\nREGRESSION_WAIT_STACK\\n'+''.join(traceback.format_stack(frame)),file=sys.stderr,flush=True)
threading.Thread(target=watch,daemon=True).start()
from lab.engine_selftest import main
result=main()
done.set()
raise SystemExit(result)
"""
        env=os.environ.copy();env.pop('PYTHONPATH',None);env['PYTHONUTF8']='1';env['PYTHONDONTWRITEBYTECODE']='1'
        argv=[sys.executable,'-B','-c',command]
        if args.standard_empty or args.standard_nonempty:
            env.pop('FSL_RUN_STANDARD_PIPELINE',None);env.pop('FSL_RUN_NONEMPTY_PIPELINE',None)
            env['FSL_RUN_STANDARD_PIPELINE' if args.standard_empty else 'FSL_RUN_NONEMPTY_PIPELINE']='1'
            env['FSL_VALIDATION_ROOT']=str(output/('synthetic_full16_empty' if args.standard_empty else 'synthetic_full16_nonempty'))
            argv=[sys.executable,'-B','-m','unittest','discover','-s','tests','-p','test_pipeline_standard.py','-v']
        run=subprocess.run(argv,cwd=frozen,env=env,stdout=out,stderr=subprocess.STDOUT,timeout=3600 if args.standard_empty or args.standard_nonempty else 1800)
    after={p.relative_to(ROOT).as_posix():sha(p) for p in release_files()}
    frozen_after={name:sha(frozen/name) for name in before}
    text=log.read_text(encoding='utf-8');count=re.search(r'Ran (\d+) tests',text)
    result={'version':VERSION,'status':'PASS' if run.returncode==0 and before==after==frozen_after else 'FAIL','engine_hash':engine,'release_hash':release,'tests':int(count[1]) if count else None,'exit_code':run.returncode,'source_unchanged':before==after,'frozen_copy_unchanged':before==frozen_after,'seconds':round(time.time()-started,3),'scope':'synthetic tiny full-16 empty-result pipeline, not nonempty or real research' if args.standard_empty else 'synthetic full-16 nonempty ten-node-per-direction pipeline, not full search or real research' if args.standard_nonempty else 'isolated engine regressions; no pipeline acceptance or real league trial','source_manifest':before}
    atomic_json(output/(stem+'.json'),result)
    print(json.dumps({k:v for k,v in result.items() if k!='source_manifest'},ensure_ascii=False))
    raise SystemExit(0 if result['status']=='PASS' else 1)
