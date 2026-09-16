import sys as _retired_sys
if "--run-retired-historical-script" not in _retired_sys.argv:
    raise SystemExit("已退役的历史脚本（0.3.1/0.4.3口径）：会改写验收台账或把整个validation目录打包到项目上级目录；默认拒绝运行。")
"""Build and independently verify the source delivery without bundling workspace data."""
from pathlib import Path
import json,os,subprocess,sys,tempfile,zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab.common import ROOT,VERSION,sha,atomic_json,source_fingerprint

def main():
    allowed={'lab','tests','web','docs','config','demo','validation'}
    excluded={'__pycache__','release_package.json','user_task_upgrade.json'}
    files=[]
    for p in ROOT.rglob('*'):
        if not p.is_file():continue
        rel=p.relative_to(ROOT)
        if len(rel.parts)>1 and rel.parts[0] not in allowed:continue
        if any(part in excluded for part in rel.parts) or p.name=='manifest.sha256.json':continue
        if p.suffix in ('.pyc','.nbc','.nbi','.tmp'):continue
        files.append(p)
    manifest={p.relative_to(ROOT).as_posix():sha(p) for p in sorted(files)}
    atomic_json(ROOT/'manifest.sha256.json',manifest);files.append(ROOT/'manifest.sha256.json')
    output=ROOT.parent/f'FootballStrategyLab_v{VERSION}_修复交付.zip'
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in sorted(files):z.write(p,p.relative_to(ROOT))
    with tempfile.TemporaryDirectory(prefix='fsl_release_verify_') as td:
        dest=Path(td).resolve();assert dest.parent==Path(tempfile.gettempdir()).resolve()
        with zipfile.ZipFile(output) as z:z.extractall(dest)
        for name,h in manifest.items():assert sha(dest/name)==h,name
        env=os.environ.copy();env.pop('PYTHONPATH',None);env['PYTHONUTF8']='1';env['PYTHONDONTWRITEBYTECODE']='1'
        check=subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests','-v'],cwd=dest,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',timeout=300)
        if check.returncode:raise RuntimeError(check.stdout)
        result={'status':'PASS','version':VERSION,'engine_hash':source_fingerprint(),'package':str(output),'sha256':sha(output),
                'files':len(files),'source_manifest_entries':len(manifest),'fresh_extract_hashes':'PASS','fresh_extract_tests':check.stdout[-450:],
                'workspace_included':False,'python_runtime_included':False,'size_bytes':output.stat().st_size}
    atomic_json(ROOT/'validation'/'v031'/'release_package.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
