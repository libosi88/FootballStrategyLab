"""Recheck five old findings using disposable fixtures and current source."""
from pathlib import Path
import importlib.util
import json
import os
import tempfile
import traceback
from unittest.mock import patch

ROOT = Path.cwd().resolve()
OUT = Path(__file__).resolve().parent
if ROOT not in OUT.parents or not (ROOT / "lab/server.py").is_file():
    raise SystemExit("Run from the project root.")
DEST = OUT / "delivery_boundary_results.json"
if DEST.exists():
    raise FileExistsError("Evidence exists; do not restart completed probes.")

def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

publication = load("audit_publication", "validation/delivery_audit_20260915_r1/independent_probes.py")
runtime = load("audit_runtime", "validation/delivery_audit_20260915_r1/runtime_ui/worker1_probe.py")
before = publication.manifest()
report = {"scope": "TARGETED_BOUNDARIES_NOT_FULL_ACCEPTANCE", "version": publication.VERSION,
          "engine_hash": publication.source_fingerprint(), "release_hash": publication.release_fingerprint(),
          "real_jobs_started": False, "probes": {}}
with patch.object(tempfile, "tempdir", str(OUT)), patch.dict(os.environ, {
        "TEMP": str(OUT), "TMP": str(OUT), "NUMBA_CACHE_DIR": str(OUT / "numba_cache")}):
    with tempfile.TemporaryDirectory(prefix="boundary_", dir=OUT) as directory:
        base = Path(directory)
        actions = [
            ("F01_scheduler_logging", runtime.scheduler_error_handler),
            ("F02_publication_source_drift", publication.source_drift_probe),
            ("F03_unlisted_file_in_archives", publication.extra_file_probe),
            ("F04_legacy_audit_import", lambda: runtime.legacy_import(base)),
            ("F05_legacy_running_pause", lambda: runtime.active_legacy_pause_and_http(base)),
        ]
        for name, operation in actions:
            try:
                report["probes"][name] = operation()
            except Exception as error:
                report["probes"][name] = {"probe_error": str(error), "traceback": traceback.format_exc()}
report["source_unchanged"] = before == publication.manifest()
report["completed_probe_count"] = sum("probe_error" not in r for r in report["probes"].values())
report["reproduced_defect_count"] = sum(bool(r.get("defect_reproduced", r.get("finding_reproduced", False))) for r in report["probes"].values())
with DEST.open("x", encoding="utf-8") as stream:
    json.dump(report, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
print(json.dumps(report, ensure_ascii=True, indent=2))
raise SystemExit(0 if report["source_unchanged"] and report["completed_probe_count"] == 5 else 2)
