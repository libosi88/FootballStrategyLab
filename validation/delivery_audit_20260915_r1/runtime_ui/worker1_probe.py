"""Isolated delivery-audit probes. Never launch research workers or touch production jobs."""
from pathlib import Path
import copy
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]
SCRATCH = BASE / "temp"
SCRATCH.mkdir(exist_ok=True)
tempfile.tempdir = str(SCRATCH)
os.environ.update(TEMP=str(SCRATCH), TMP=str(SCRATCH), PYTHONDONTWRITEBYTECODE="1",
                  NUMBA_CACHE_DIR=str(BASE / "numba_cache"))
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from lab.common import VALIDATION_DEFAULT, canonical, sha, read_json
from lab.store import Store
from lab.migration import import_audit_bundle
from lab import server
from test_v031 import quote, write_csv
from test_full_audit_ui import isolated_ui

PRODUCTION_FILES = ["lab/" + name + ".py" for name in
                    ("server", "store", "migration", "fleet", "launcher", "cli")]
PRODUCTION_FILES += ["web/" + name for name in ("app.js", "jobs.js", "index.html", "style.css")]
def hashes():
    return {name: sha(ROOT / name) for name in PRODUCTION_FILES}

def archive_fixture(folder, config):
    folder.mkdir(parents=True)
    source = folder / "quotes.csv"
    write_csv(source, [quote("audit-isolated-match")])
    digest = sha(source)
    manifest = {"files": [{"path": "Z:/unavailable/quotes.csv", "sha256": digest, "kind": "quotes"}]}
    archive = folder / "audit.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("input_manifest.json", json.dumps(manifest))
        z.writestr("config.json", json.dumps(config))
        z.writestr("data_audit.json", json.dumps({"league": "真实甲组", "company": "皇冠"}))
        z.write(source, "original_inputs/" + digest[:12] + "_quotes.csv")
    return archive

def legacy_import(run):
    current = copy.deepcopy(VALIDATION_DEFAULT)
    legacy = copy.deepcopy(current)
    legacy.pop("research_objective", None)
    legacy.update(created_version="0.5.0", engine_hash="legacy-engine")
    good_archive = archive_fixture(run / "explicit_validation", current)
    legacy_archive = archive_fixture(run / "legacy_validation", legacy)
    good = import_audit_bundle(good_archive, run / "good_workspace")
    good_job = Store(run / "good_workspace").get(good["job"])
    again = import_audit_bundle(good_archive, run / "good_workspace")
    old_workspace = run / "legacy_workspace"
    error = None
    try:
        imported = import_audit_bundle(legacy_archive, old_workspace)
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        imported = None
    command = [sys.executable, "-B", "-m", "lab.cli", "import-audit", str(legacy_archive),
               "--workspace", str(run / "legacy_cli_workspace")]
    cli = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=30)
    return {"defect_reproduced": error is not None and "留出期" in error["message"],
            "explicit_validation_control": {"status": good_job["status"],
                "objective": good_job["config"]["research_objective"],
                "holdout_months": good_job["config"]["holdout_months"],
                "repeat_same_job": good["job"] == again["job"]},
            "legacy_input": {"profile": legacy["profile"], "holdout_months": legacy["holdout_months"],
                              "research_objective_present": "research_objective" in legacy},
            "legacy_error": error, "legacy_result": imported,
            "legacy_jobs_created": len(Store(old_workspace).jobs()),
            "cli": {"exit_code": cli.returncode, "stdout": cli.stdout, "stderr": cli.stderr},
            "fixture": str(legacy_archive)}

def active_legacy_pause_and_http(run):
    with isolated_ui() as (workspace, request):
        store = Store(workspace)
        current_id = store.create("isolated-current", VALIDATION_DEFAULT, {"files": []}, True)
        old_id = store.create("isolated-old", VALIDATION_DEFAULT, {"files": []}, True)
        old_config = store.get(old_id)["config"]
        old_config.update(engine_hash="legacy-engine", created_version="0.5.0")
        with store.conn() as db:
            db.execute("UPDATE jobs SET config=? WHERE id=?", (canonical(old_config), old_id))
        for jid in (current_id, old_id):
            store.update(jid, status="RUNNING", pause=0, pid=os.getpid())
        jobs = [server.job_view(store, store.get(jid)) for jid in (current_id, old_id)]
        fixture = run / "jobs_fixture.json"
        fixture.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
        node = subprocess.run([shutil.which("node"), str(BASE / "worker1_jobs_probe.js"),
                               str(ROOT / "web/jobs.js"), str(fixture)],
                              cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=15)
        if node.returncode:
            raise AssertionError(node.stderr)
        rendered = json.loads(node.stdout)
        pause_status, pause_reply = request("/api/control", {"job": old_id, "action": "pause"})
        pause_state = store.get(old_id)["status"]
        sample = store.root / current_id / "audit_sample.txt"
        sample.write_text("isolated audit fixture", encoding="utf-8")
        ticket_status, ticket = request("/api/download-ticket", {"job": current_id, "path": sample.name})
        url = "http://127.0.0.1:" + str(read_json(store.root / "ui_endpoint.json")["port"])
        with urlopen(url + ticket["url"], timeout=5) as response:
            download = {"status": response.status, "content": response.read().decode()}
        try:
            urlopen(url + ticket["url"], timeout=5)
            repeat_status = 200
        except HTTPError as exc:
            repeat_status = exc.code
            exc.close()
        (store.root / "outside_job.txt").write_text("isolated", encoding="utf-8")
        traversal_status, _ = request("/api/download-ticket", {"job": current_id, "path": "../outside_job.txt"})
        try:
            urlopen(url + "/api/state", timeout=5)
            no_token = 200
        except HTTPError as exc:
            no_token = exc.code
            exc.close()
        return {"defect_reproduced": "暂停" in rendered[0]["buttons"] and
                    "暂停" not in rendered[1]["buttons"] and pause_status == 200 and pause_state == "PAUSING",
                "rendered_jobs": rendered, "legacy_pause_api": {"status": pause_status,
                    "reply": pause_reply, "resulting_job_status": pause_state},
                "http_controls": {"no_token_state": no_token, "download_ticket_status": ticket_status,
                    "first_download": download, "reused_ticket_status": repeat_status,
                    "traversal_ticket_status": traversal_status}}

def scheduler_error_handler():
    failures = []
    with isolated_ui() as (workspace, request):
        original = Path.write_text
        def no_error_log(path, *args, **kwargs):
            if path == workspace / "scheduler_error.log":
                raise OSError(errno.ENOSPC, "isolated simulated full disk")
            return original(path, *args, **kwargs)
        def record_failure(args):
            failures.append({"thread": args.thread.name, "type": args.exc_type.__name__,
                             "message": str(args.exc_value)})
        with patch.object(server, "launch_next", side_effect=OSError(errno.ENOSPC, "isolated launch failure")), \
             patch.object(Path, "write_text", new=no_error_log), \
             patch.object(threading, "excepthook", side_effect=record_failure):
            deadline = time.monotonic() + 3
            while not failures and time.monotonic() < deadline:
                time.sleep(.02)
        status, state = request("/api/state")
        return {"defect_reproduced": bool(failures) and status == 200,
                "uncaught_thread_errors": failures, "state_http_status_after_thread_died": status,
                "reported_scheduler_state": state.get("scheduler"),
                "injection": "Only this disposable server: launch_next and scheduler_error.log writes raise ENOSPC.",
                "limitation": "No physical disk filling; validates scheduler's exception-handling path."}

def main():
    run = Path(tempfile.mkdtemp(prefix="evidence_", dir=BASE))
    before = hashes()
    report = {"status": "COMPLETE_FOR_PROBES", "run_directory": str(run), "tests": {},
              "production_hashes_before": before, "scope": "synthetic inputs; no real research runs"}
    for name, test in (("legacy_audit_import", lambda: legacy_import(run)),
                       ("legacy_running_ui_pause_and_http", lambda: active_legacy_pause_and_http(run)),
                       ("scheduler_logging_failure", scheduler_error_handler)):
        try:
            report["tests"][name] = test()
        except Exception as exc:
            import traceback
            report["tests"][name] = {"probe_error": str(exc), "traceback": traceback.format_exc()}
            report["status"] = "PARTIAL"
    report["production_hashes_after"] = hashes()
    report["production_files_unchanged"] = report["production_hashes_after"] == before
    report_path = run / "probe_results.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report_path": str(report_path), **report}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
