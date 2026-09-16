"""Read-only source inventory and isolated delivery-boundary fault injection."""
from __future__ import annotations
import ast
import importlib.metadata
import json
import platform
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from lab.common import VERSION, atomic_json, sha, source_fingerprint
from lab.release import release_files, release_fingerprint
from lab import packaging
from test_packaging_publication import PackagingPublication

def manifest():
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in release_files(ROOT)}

def fixture():
    case = PackagingPublication("test_both_archives_and_certificate_are_published_before_root_completion")
    case.setUp()
    return case

def source_drift_probe():
    case = fixture()
    try:
        target = packaging.ROOT / "docs" / "fixture.txt"
        before = target.read_bytes()
        mutated = False
        def isolated_check(command, **kwargs):
            nonlocal mutated
            result = case.fake_check(command, **kwargs)
            if not mutated:
                target.write_bytes(b"AUDIT_ONLY_NON_SEMANTIC_REVISION_B")
                mutated = True
            return result
        with patch.object(packaging.subprocess, "run", side_effect=isolated_check):
            packages, state = case.run_package()
        with zipfile.ZipFile(case.root / "packages" / packages["developer"]) as a:
            a_value = a.read("docs/fixture.txt")
        with zipfile.ZipFile(case.root / "packages" / packages["audit"]) as b:
            b_value = b.read("software/docs/fixture.txt")
        return {"id": "DELIVERY-SOURCE-DRIFT",
                "finding_reproduced": a_value != b_value and a_value == before,
                "returned_state": state["state"],
                "developer_source": a_value.decode(), "audit_source": b_value.decode(),
                "scope": "Real package_run and real ZIP I/O on isolated publication fixtures. Expensive child validations and semantic hash are stubbed by the existing fixture. Only a non-semantic documentation file changes; no project file changes."}
    finally:
        case.doCleanups()

def extra_file_probe():
    case = fixture()
    try:
        target = packaging.ROOT / "config" / "private_audit_marker.env"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("AUDIT_SENTINEL_ONLY=not_a_secret", encoding="utf-8")
        approved = {p.relative_to(packaging.ROOT).as_posix() for p in release_files(packaging.ROOT)}
        packages, state = case.run_package()
        with zipfile.ZipFile(case.root / "packages" / packages["developer"]) as a:
            included_a = "config/private_audit_marker.env" in a.namelist()
        with zipfile.ZipFile(case.root / "packages" / packages["audit"]) as b:
            included_b = "software/config/private_audit_marker.env" in b.namelist()
        return {"id": "DELIVERY-UNLISTED-FILE",
                "finding_reproduced": included_a and included_b and "config/private_audit_marker.env" not in approved,
                "returned_state": state["state"], "in_source_release_whitelist": False,
                "included_in_A": included_a, "included_in_B": included_b,
                "scope": "Isolated harmless sentinel, not an actual credential disclosure. Production package_run ZIP inclusion logic; expensive subprocess checks stubbed by existing fixture."}
    finally:
        case.doCleanups()

def main():
    started = time.time()
    before = manifest()
    inventory = []
    errors = []
    for relative in before:
        p = ROOT / relative
        record = {"path": relative, "sha256": before[relative], "bytes": p.stat().st_size}
        if p.suffix == ".py":
            try:
                text = p.read_text(encoding="utf-8-sig")
                tree = ast.parse(text, filename=relative)
                record.update(lines=len(text.splitlines()), ast_parse="PASS",
                    definitions=[{"name": n.name, "line": n.lineno} for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))])
            except Exception as e:
                record["ast_parse"] = "FAIL"
                errors.append({"path": relative, "error": repr(e)})
        inventory.append(record)
    from validation.check_dependencies import locked_components
    pins = [{"lock": lock, "name": name, "version": version, "hash_count": len(hashes)}
            for lock, name, version, hashes in locked_components(ROOT)]
    checks = [source_drift_probe(), extra_file_probe()]
    after = manifest()
    git = subprocess.run(["git", "-c", "core.quotepath=false", "status", "--short"],
                         cwd=ROOT, text=True, encoding="utf-8", capture_output=True, check=True)
    (OUT / "git_status_audit.txt").write_text(git.stdout, encoding="utf-8")
    report = {"version": VERSION, "engine_hash": source_fingerprint(), "release_hash": release_fingerprint(),
              "python": sys.version, "platform": platform.platform(),
              "installed_versions": {p: importlib.metadata.version(p) for p in ("numpy", "numba", "llvmlite", "pip")},
              "release_files": len(before), "python_files": sum(r["path"].endswith(".py") for r in inventory),
              "ast_errors": errors, "source_unchanged": before == after, "source_manifest": before,
              "dependency_parser_pins": pins, "probes": checks, "seconds": round(time.time() - started, 3)}
    atomic_json(OUT / "source_inventory.json", inventory)
    atomic_json(OUT / "independent_probes.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "source_manifest"}, ensure_ascii=True, indent=2))
    return 0 if not errors and before == after else 1

if __name__ == "__main__":
    raise SystemExit(main())
