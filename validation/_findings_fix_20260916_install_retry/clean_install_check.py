"""Run unchanged setup in a fresh disposable source copy and virtual environment."""
import json, os, re, shutil, subprocess, sys, tempfile, time, traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from lab.common import atomic_json,sha,source_fingerprint
from lab.release import release_files,release_fingerprint

def manifest():
    return {p.relative_to(ROOT).as_posix():sha(p) for p in release_files(ROOT)}

def main():
    dest=OUT/"clean_install.json"
    if dest.exists():raise FileExistsError("Installation audit already exists")
    started=time.time();before=manifest()
    report={"status":"RUNNING","engine_hash":source_fingerprint(),"release_hash":release_fingerprint(),
        "scope":"Fresh temporary virtual environment via unchanged Windows setup. Uses installed base Python; not a clean OS or the Python-installer download branch.",
        "production_environment_changed":False,"real_jobs_started":False}
    atomic_json(dest,report)
    try:
        with tempfile.TemporaryDirectory(prefix="fsl_install_audit_") as directory:
            fresh=Path(directory)
            for rel in before:
                target=fresh/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/rel,target)
            if {rel:sha(fresh/rel) for rel in before}!=before:raise RuntimeError("Source changed during copy")
            env=os.environ.copy()
            for key in ("PYTHONPATH","FSL_RUN_STANDARD_PIPELINE","FSL_RUN_NONEMPTY_PIPELINE","FSL_VALIDATION_ROOT"):env.pop(key,None)
            base=Path(sys._base_executable).resolve();env["PATH"]=str(base.parent)+os.pathsep+env.get("PATH","")
            env["PYTHONUTF8"]="1";env["PYTHONDONTWRITEBYTECODE"]="1"
            command=["powershell.exe","-NoProfile","-File",str(fresh/"setup_windows.ps1"),"-SetupOnly","-ExistingPythonOnly"]
            report.update(command=command,base_python=str(base),temporary_source=str(fresh));atomic_json(dest,report)
            log=OUT/"clean_install.log"
            with log.open("x",encoding="utf-8") as stream:
                proc=subprocess.run(command,cwd=fresh,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=1800)
            report.update(exit_code=proc.returncode,log=log.name,log_sha256=sha(log))
            content=log.read_text(encoding="utf-8",errors="replace")
            count=re.search(r"Ran (\d+) tests",content);skip=re.search(r"OK \(skipped=(\d+)\)",content)
            report.update(tests=int(count[1]) if count else None,skipped=int(skip[1]) if skip else 0)
            report["status"]="PASS" if proc.returncode==0 and count else "FAIL"
            if proc.returncode==0:
                check=subprocess.run([str(fresh/".venv/Scripts/python.exe"),"-B","-m","pip","check"],cwd=fresh,env=env,text=True,encoding="utf-8",capture_output=True,timeout=30)
                report["pip_check"]={"exit_code":check.returncode,"stdout":check.stdout,"stderr":check.stderr}
                if check.returncode:report["status"]="FAIL"
            report["frozen_source_unchanged"]={rel:sha(fresh/rel) for rel in before}==before
            if not report["frozen_source_unchanged"]:report["status"]="FAIL"
        report["temporary_removed"]=not fresh.exists()
    except BaseException as error:
        report.update(status="FAIL",error=repr(error),traceback=traceback.format_exc())
    finally:
        report["source_unchanged"]=manifest()==before
        if not report["source_unchanged"]:report["status"]="FAIL"
        report["seconds"]=round(time.time()-started,3);atomic_json(dest,report)
        print(json.dumps(report,ensure_ascii=True))
    return 0 if report["status"]=="PASS" else 1

if __name__=="__main__":raise SystemExit(main())
