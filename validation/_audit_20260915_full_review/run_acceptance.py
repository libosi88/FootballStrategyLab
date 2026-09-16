"""Synthetic historical S0-S7 acceptance through the production pipeline.

Run only after the main agent freezes the source. Each invocation requires a NEW
output directory, and runs exactly one case: empty (3 matches, complete search),
full (90 matches, complete compact_v1 search), or limited (90 matches, 10 DFS
nodes per direction, expected PARTIAL_RESULT). No test or verifier is mocked.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import time
import traceback
import zipfile
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))

from lab.common import (DEFAULT, DIRECTIONS, ROOT, VERSION, atomic_json,
                        canonical, digest, read_json, read_jsonl, sha,
                        source_fingerprint)
from lab.data import inspect
from lab.pipeline import run
from lab.release import release_files, release_fingerprint
from lab.search_integrity import verify_search_evidence
from lab.selection import PREGOAL_REJECTION_POLICY
from lab.store import Store
from lab.verify import verification_passed
from test_pipeline_standard import nonempty_rows
from test_v031 import write_csv

CASES = {
    "empty": {"matches": 3, "nodes": 0, "state": "HISTORICAL_RESEARCH_COMPLETE"},
    "full": {"matches": 90, "nodes": 0, "state": "HISTORICAL_RESEARCH_COMPLETE"},
    "limited": {"matches": 90, "nodes": 10, "state": "PARTIAL_RESULT"},
}
NOTES = ("results/最终决定.md", "results/缺口说明.md")
SCENARIOS = {str(i) for i in range(5)}


def require(condition: Any, message: str) -> None:
    # Unlike a bare assert, acceptance checks are not disabled by Python -O.
    if not condition:
        raise AssertionError(message)


def object_file(path: Path) -> dict[str, Any]:
    value = read_json(path)
    require(isinstance(value, dict), f"Missing or invalid JSON object: {path}")
    return value


def member_path(root: Path, name: str) -> Path:
    relative = Path(name)
    require(not relative.is_absolute(), f"Expected a relative evidence path: {name}")
    target = (root / relative).resolve()
    require(root.resolve() in target.parents, f"Evidence path escapes its root: {name}")
    return target


def source_manifest() -> dict[str, str]:
    return {p.relative_to(PROJECT).as_posix(): sha(p) for p in release_files(PROJECT)}


def zip_sha(archive: zipfile.ZipFile, name: str) -> str:
    result = hashlib.sha256()
    with archive.open(name) as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def verify_config(config: dict[str, Any], case: str, engine: str) -> None:
    required = {
        "profile": "standard", "research_objective": "historical",
        "search_grammar": "compact_v1", "holdout_months": 0,
        "standard_node_budget": CASES[case]["nodes"], "standard_review_budget": 0,
        "max_run_minutes": 0, "max_output_mb": 0,
        "package_handoff": True, "live_enabled": False, "second_slot_enabled": False,
    }
    for name, value in required.items():
        require(type(config.get(name)) is type(value) and config[name] == value,
                f"Frozen config mismatch: {name}")
    require(Decimal(str(config["min_profit"])) == Decimal("10"), "min_profit must be 10.")
    require(len(config["directions"]) == 16 and set(config["directions"]) == set(DIRECTIONS),
            "Frozen config must contain each of the 16 directions exactly once.")
    require(not config.get("research_partition"), "Acceptance must use the full selected history.")
    require(config.get("engine_hash") == engine, "Job is bound to another source revision.")


def verify_coverage(job: Path, result: dict[str, Any], case: str) -> dict[str, Any]:
    coverage = object_file(job / "coverage.json")
    require(canonical(coverage) == canonical(result["coverage"]), "Returned coverage differs from disk.")
    directions = coverage["directions"]
    require(len(directions) == 16 and set(directions) == set(DIRECTIONS), "Missing/extra search directions.")
    require(coverage["planned_expressions"] > 0, "Search has no declared expressions.")
    accounted = sum(coverage[k] for k in ("evaluated", "equivalent_occurrences",
                                          "proven_zero", "proven_nonprofitable"))
    require(coverage["remaining"] == coverage["planned_expressions"] - accounted,
            "Search coverage accounting does not balance.")
    spec = object_file(job / "search_spec.json")
    require(spec["grammar"] == "compact_v1" and spec["all_16_directions"] is True,
            "The frozen grammar is not the full 16-direction compact_v1 scope.")
    require(set(spec["requested_directions"]) == set(DIRECTIONS)
            and set(spec["directions"]) == set(DIRECTIONS), "Search spec omits a direction.")
    states = {}
    for direction, scope in directions.items():
        folder = job / "mining" / direction
        state = object_file(folder / "state.json")
        require(state["status"] == scope["status"], f"{direction}: stale coverage state.")
        status = state["status"]
        backend = state.get("search_backend", "standard_class_dfs")
        if status == "NO_DATA":
            proof_backend = "standard_empty_scope"
        elif status == "COMPLETE":
            proof_backend = backend
        else:
            require(case == "limited" and status == "BUDGET_STOP"
                    and backend == "standard_class_dfs", f"{direction}: unexpected stop {status}.")
            proof_backend = "standard_class_dfs_partial"
        verify_search_evidence(folder, proof_backend, state.get("binding"))
        if case == "limited" and status != "NO_DATA" and backend == "standard_class_dfs":
            require(type(state.get("nodes")) is int and 0 <= state["nodes"] <= 10,
                    f"{direction}: exceeded the 10-node budget.")
            if status == "BUDGET_STOP":
                require(state["nodes"] == 10, f"{direction}: stopped before its node budget.")
        states[direction] = {"status": status, "backend": backend,
                             "nodes": state.get("nodes"), "candidates": state.get("candidates", 0)}
    modules = object_file(job / "coverage_modules.json")
    require(modules["evidence_complete"] is True and not modules["evidence_gaps"]
            and not modules["blocked_directions"], "Coverage evidence is incomplete.")
    complete = case != "limited"
    require(coverage["standard_search_complete"] is complete
            and coverage["local_search_complete"] is complete
            and modules["declared_standard_search_complete"] is complete,
            "Search completion does not match the case.")
    require((coverage["remaining"] == 0) if complete else coverage["remaining"] > 0,
            "Incorrect remaining search count.")
    if not complete:
        require(any(s["status"] == "BUDGET_STOP" for s in states.values()),
                "Limited case did not exercise a budget stop.")
    frozen = object_file(job / "candidate_pool_freeze.json")
    require(frozen["coverage_sha256"] == sha(job / "coverage.json"), "Pool coverage hash changed.")
    require(Decimal(str(frozen["profit_threshold"])) == Decimal("10"), "Pool threshold is not 10.")
    require(frozen["candidate_occurrences"] == coverage["candidates"]
            and frozen["complete"] is complete and frozen["standard_complete"] is complete,
            "Pool freeze disagrees with search evidence.")
    return states


def verify_rosters(job: Path, result: dict[str, Any], config: dict[str, Any],
                   case: str) -> tuple[dict[str, Any], list[str]]:
    rosters = {}
    evidence = ["config.json", "labels.json", "events.jsonl.gz", "coverage.json",
                "coverage_modules.json", "engine_validation.json", "engine_tests.log", "search_spec.json"]
    for key, relative in (("main", "results"), ("backup", "results/lower_risk")):
        folder = job / relative
        packet = object_file(folder / "rules.json")
        trigger = object_file(folder / "trigger_verification.json")
        count = result["selected" if key == "main" else "backup_selected"]
        require(len(packet["rules"]) == count and trigger["rules"] == count,
                f"{key}: roster count does not match the result.")
        require(count == 0 if case == "empty" else count > 0, f"{key}: unexpected empty/nonempty roster.")
        require(trigger["status"] == ("EMPTY_ROSTER" if case == "empty" else "PASS")
                and verification_passed(trigger), f"{key}: trigger verification did not pass.")
        require(trigger["execution_economics"]["status"] == "PASS"
                and trigger["rule_cases"]["status"] == "PASS", f"{key}: missing required verification.")
        require(trigger["missing_signals"] == 0 and trigger["extra_or_changed_signals"] == 0,
                f"{key}: replay signal mismatch.")
        require(set(trigger["order_differences"]) == SCENARIOS
                and all(v == 0 for v in trigger["order_differences"].values()),
                f"{key}: five-scenario order comparison is missing or failed.")
        for section in ("persistent_paper_execution", "streaming_paper_execution"):
            report = trigger[section]
            scenarios = report if section == "persistent_paper_execution" else report["scenarios"]
            require(set(scenarios) == SCENARIOS
                    and all(scenarios[i]["status"] == "PASS" for i in SCENARIOS),
                    f"{key}: incomplete {section}.")
            require(all(scenarios[i].get("real_orders_sent") == 0 for i in SCENARIOS),
                    f"{key}: simulation evidence does not declare zero real orders.")
        policy = packet["execution_policy"]
        require(policy["research_config_hash"] == digest(config)
                and policy["quote_mapping"] == "minute_close_latest_v3"
                and policy["pregoal_rejection"] == PREGOAL_REJECTION_POLICY,
                f"{key}: execution policy differs from the historical research contract.")
        require(policy["live_enabled"] is False and policy["second_slot_enabled"] is False,
                f"{key}: execution switches must remain disabled.")
        require(packet["roster_hash"] == digest({k: packet[k] for k in ("rules", "contract", "execution_policy")}),
                f"{key}: roster hash mismatch.")
        signals = sum(1 for _ in read_jsonl(folder / "golden_signals.jsonl.gz"))
        require(signals == trigger["golden_signals"] == trigger["actual_signals"],
                f"{key}: golden signal count differs from verification.")
        require(signals == 0 if case == "empty" else signals > 0, f"{key}: missing nonempty trigger exercise.")
        integration = object_file(folder / "integration_manifest.json")
        require(integration["roster_hash"] == packet["roster_hash"], f"{key}: stale integration manifest.")
        for name, expected in integration["binding_files"].items():
            require(sha(member_path(folder, name)) == expected, f"{key}: changed handoff asset {name}.")
        names = ["rules.json", "trigger_verification.json", "golden_signals.jsonl.gz",
                 "event.schema.json", "execution_config.json", "integration_manifest.json",
                 "boundary_cases.json", "rule_replay_cases.jsonl.gz"]
        names += [f"{prefix}_s{i}.jsonl.gz" for prefix in ("orders", "minute_close_orders") for i in range(5)]
        evidence.extend(f"{relative}/{name}" for name in names)
        rosters[key] = {"rules": count, "golden_signals": signals,
                        "status": trigger["status"], "roster_hash": packet["roster_hash"]}
    return rosters, evidence


def verify_packages(job: Path, result: dict[str, Any], workflow: dict[str, Any],
                    journal: dict[str, Any], sources: dict[str, str], inputs: list[dict[str, Any]],
                    evidence: list[str]) -> dict[str, Any]:
    packaging = object_file(job / "packaging_verification.json")
    require(packaging["fresh_zip_extract"] is True
            and packaging["final_zip_fresh_extract_integrity"] == "PASS"
            and packaging["audit_zip_crc"] == "PASS", "Missing actual A/B package verification.")
    commands = packaging["commands"]
    require([c["command"] for c in commands] == [
        ["-B", "-m", "lab.engine_selftest"], ["-B", "verify_handoff.py"]],
        "Fresh-directory acceptance did not run both production commands.")
    for command in commands:
        require(command["exit_code"] == 0 and sha(member_path(job, command["log"])) == command["sha256"],
                "Package command failed or its log changed.")
    require(verification_passed(packaging["signal_verification"]), "Fresh A trigger verification failed.")
    packages = result["packages"]
    paths = {key: member_path(job / "packages", packages[key]) for key in ("developer", "audit")}
    require(paths["developer"].parent == paths["audit"].parent, "A/B packages were not published together.")
    certificate = object_file(paths["developer"].parent / "交接验收证书.json")
    require(certificate["packages"] == packages and certificate["workflow"] == workflow,
            "Published certificate differs from root metadata.")
    require(certificate["final_zip_integrity"] == "PASS" and certificate["audit_zip_crc"] == "PASS"
            and certificate["engine_tests_in_fresh_directory"] is True, "Certificate lacks acceptance evidence.")
    evidence_hashes = {name: sha(member_path(job, name)) for name in evidence}
    notes = {name: member_path(job, name).read_bytes() for name in NOTES}
    checked = {}
    for key, path in paths.items():
        require(sha(path) == packages[key + "_sha256"], f"{key}: final archive hash mismatch.")
        with zipfile.ZipFile(path) as archive:
            names = [i.filename for i in archive.infolist() if not i.is_dir()]
            require(len(names) == len(set(names)), f"{key}: duplicate archive members.")
            require(archive.testzip() is None, f"{key}: CRC failure.")
            require(json.loads(archive.read("workflow_status.json")) == workflow
                    and json.loads(archive.read("workflow_stages.json")) == journal,
                    f"{key}: completion/stage metadata differs from the root.")
            for name, expected in evidence_hashes.items():
                require(zip_sha(archive, name) == expected, f"{key}: changed research/trigger evidence {name}.")
            for name, expected in notes.items():
                require(archive.read(name) == expected, f"{key}: root/A/B conclusion bytes differ: {name}.")
            prefix = "" if key == "developer" else "software/"
            for name, expected in sources.items():
                require(zip_sha(archive, prefix + name) == expected, f"{key}: changed frozen source {name}.")
            if key == "developer":
                manifest = json.loads(archive.read("manifest.json"))
                members = {name for name in names if Path(name).name != "manifest.json"
                           and "__pycache__" not in Path(name).parts}
                require(set(manifest) == members, "A manifest omits or invents files.")
                for name, expected in manifest.items():
                    require(zip_sha(archive, name) == expected, f"A manifest mismatch: {name}.")
            else:
                require(json.loads(archive.read("input_manifest.json"))["files"] == inputs,
                        "B input manifest differs from the job.")
                for record in inputs:
                    name = "original_inputs/" + record["sha256"][:12] + "_" + Path(record["path"]).name
                    require(zip_sha(archive, name) == record["sha256"], "B original input hash mismatch.")
                summary = json.loads(archive.read("summary.json"))
                require(summary["state"] == result["state"] and summary["coverage"] == result["coverage"]
                        and summary["selected"] == result["selected"]
                        and summary["backup_selected"] == result["backup_selected"], "B summary mismatch.")
                # B cannot contain its own final hash; the adjacent certificate binds it.
                require("audit_sha256" not in summary["packages"], "B contains a self-referential hash.")
            checked[key] = {"path": str(path), "sha256": packages[key + "_sha256"],
                            "members": len(names), "source_files_verified": len(sources),
                            "research_files_verified": len(evidence_hashes), "crc": "PASS",
                            "root_conclusion_bytes_equal": True}
    return {"archives": checked, "commands": commands, "certificate_sha256": sha(
        paths["developer"].parent / "交接验收证书.json")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=tuple(CASES), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-engine-hash", help="Optional source hash supplied by the main-agent freeze.")
    parser.add_argument("--expected-release-hash", help="Optional release hash supplied by the main-agent freeze.")
    args = parser.parse_args()
    require(ROOT.resolve() == PROJECT, "Imported lab is not this script's source project.")
    output = args.output_dir.resolve()
    require(PROJECT in output.parents, "Evidence must remain inside the source project.")
    output.mkdir(parents=True, exist_ok=False)  # Never overwrite or resume an older acceptance run.
    started = time.time()
    report: dict[str, Any] = {"status": "RUNNING", "case": args.case, "synthetic": True,
        "version": VERSION, "expected_state": CASES[args.case]["state"],
        "expected_matches": CASES[args.case]["matches"], "research_objective": "historical",
        "search_grammar": "compact_v1", "search_node_budget_per_direction": CASES[args.case]["nodes"],
        "min_profit": "10", "holdout_months": 0, "real_orders_sent": 0,
        "scope": "Synthetic production-pipeline acceptance of the declared compact_v1 grammar; not real-league or live execution acceptance."}
    atomic_json(output / "acceptance.json", report)
    try:
        engine, release = source_fingerprint(), release_fingerprint()
        sources, helper_hash = source_manifest(), sha(Path(__file__))
        if args.expected_engine_hash is not None:
            require(engine == args.expected_engine_hash, "Source does not match the main-agent engine freeze.")
        if args.expected_release_hash is not None:
            require(release == args.expected_release_hash, "Source does not match the main-agent release freeze.")
        report.update(engine_hash=engine, release_hash=release, helper_sha256=helper_hash,
                      command=sys.argv[:], python=sys.version)
        atomic_json(output / "source_before.json", {"engine_hash": engine, "release_hash": release,
                    "helper_sha256": helper_hash, "files": sources})
        league, rows = nonempty_rows()
        if args.case == "empty":
            chosen = {f"synthetic-{year}-0" for year in (2022, 2023, 2024)}
            rows = [row for row in rows if row["sId"] in chosen]
        match_ids = {row["sId"] for row in rows}
        require(len(match_ids) == CASES[args.case]["matches"], "Synthetic fixture match count changed.")
        source = output / "input" / "synthetic.csv"
        write_csv(source, rows)
        original = sha(source)
        config = {**copy.deepcopy(DEFAULT), "profile": "standard", "research_objective": "historical",
                  "search_grammar": "compact_v1", "directions": list(DIRECTIONS), "min_profit": "10",
                  "holdout_months": 0, "standard_node_budget": CASES[args.case]["nodes"],
                  "standard_review_budget": 0, "max_run_minutes": 0, "max_output_mb": 0,
                  "package_handoff": True, "live_enabled": False, "second_slot_enabled": False,
                  "acceptance_scope": "SYNTHETIC_HISTORICAL_FULL_REVIEW_" + args.case.upper()}
        store = Store(output / "workspace")
        jid = store.create(league, config, inspect([str(source)], store.root / "import_cache"))
        job = store.root / jid
        frozen_config = object_file(job / "config.json")
        verify_config(frozen_config, args.case, engine)
        frozen_inputs = object_file(job / "input_manifest.json")
        require(len(frozen_inputs["files"]) == 1
                and Path(frozen_inputs["files"][0]["path"]).resolve() == source.resolve()
                and frozen_inputs["files"][0]["sha256"] == original, "Unexpected input scope/hash.")
        config_hash, input_hash = sha(job / "config.json"), sha(job / "input_manifest.json")
        report.update(jobdir=str(job), job_id=jid, input_sha256=original,
                      config_sha256=config_hash, input_manifest_sha256=input_hash, rows=len(rows))
        atomic_json(output / "acceptance.json", report)
        print("ACCEPTANCE_" + args.case.upper() + "_JOB=" + str(job), flush=True)
        result = run(store.root, jid)
        require(result is not None, "Production pipeline did not finish: " + str(store.get(jid)["status"]))
        report.update(state=result["state"], selected=result["selected"],
                      backup_selected=result["backup_selected"], remaining=result["coverage"]["remaining"])
        atomic_json(output / "acceptance.json", report)
        require(result["state"] == CASES[args.case]["state"], "Unexpected pipeline completion state.")
        require(store.get(jid)["status"] == ("PARTIAL_RESULT" if args.case == "limited" else "DONE"),
                "Durable job status differs from the expected result.")
        require(sha(source) == original and sha(job / "config.json") == config_hash
                and sha(job / "input_manifest.json") == input_hash, "Frozen input/configuration changed.")
        verify_config(object_file(job / "config.json"), args.case, engine)
        labels = object_file(job / "labels.json")
        require(set(labels) == match_ids and all(label["eligible"] is True for label in labels.values()),
                "A selected synthetic match disappeared or became ineligible.")
        years = Counter(label["year"] for label in labels.values())
        require(years == {year: (1 if args.case == "empty" else 30) for year in (2022, 2023, 2024)},
                "Selected historical years were split or omitted.")
        require(object_file(job / "holdout_plan.json")["status"] != "SPLIT",
                "Historical acceptance unexpectedly held out data.")
        require(result["standard_review"]["complete"] is True
                and result["selection_status"] == "FINITE_LOCAL_SEARCH_COMPLETE", "Downstream review/selection incomplete.")
        states = verify_coverage(job, result, args.case)
        workflow = object_file(job / "workflow_status.json")
        require(workflow["state"] == result["state"] and workflow["gates"] == result["gates"],
                "Root workflow disagrees with the returned result.")
        require(canonical(object_file(job / "summary.json")) == canonical(result), "Root summary differs from result.")
        required_gates = {"full_standard_search", "engine_tests", "all_pool_review", "selection",
                          "trigger", "nonempty_roster", "rule_cases", "persistent_paper",
                          "streaming_paper", "execution_economics", "packaging"}
        require(required_gates <= result["gates"].keys(), "Missing completion gates.")
        expected_false = {"nonempty_roster"} if args.case == "empty" else {"full_standard_search"} if args.case == "limited" else set()
        require({name for name, passed in result["gates"].items() if passed is not True} == expected_false,
                "Unexpected failed/missing completion gates.")
        require(result["empty_roster_chain_verified"] is (args.case == "empty"), "Incorrect empty-roster completion claim.")
        journal = object_file(job / "workflow_stages.json")
        require(journal["binding"] == digest({"config": frozen_config, "manifest": frozen_inputs}),
                "Stage journal is not bound to the frozen job.")
        stages = journal["stages"]
        expected_stages = {f"S{i}": ("PARTIAL" if args.case == "limited" and i in (2, 3) else "PASS") for i in range(8)}
        require({name: stage["status"] for name, stage in stages.items()} == expected_stages,
                "S0-S7 does not match the complete/limited case.")
        engine_report = object_file(job / "engine_validation.json")
        require(engine_report["status"] == "PASS" and engine_report["engine_hash"] == engine
                and engine_report["source_and_tests_unchanged"] is True
                and engine_report["log_sha256"] == sha(job / "engine_tests.log"),
                "Engine acceptance lacks source-bound log evidence.")
        rosters, evidence = verify_rosters(job, result, frozen_config, args.case)
        packages = verify_packages(job, result, workflow, journal, sources, frozen_inputs["files"], evidence)
        require(source_manifest() == sources and source_fingerprint() == engine
                and release_fingerprint() == release and sha(Path(__file__)) == helper_hash,
                "Source or acceptance helper changed during the run.")
        require(sha(source) == original, "Input changed during final archive checks.")
        report.update(status="PASS", matches=len(labels), years=dict(years), directions=16,
                      input_unchanged=True, source_unchanged=True, gates=result["gates"],
                      stages=expected_stages, direction_states=states, rosters=rosters,
                      packages=result["packages"], archive_verification=packages,
                      root_and_both_packages_conclusion_bytes_equal=True)
    except BaseException as error:
        report.update(status="FAIL", error=str(error), traceback=traceback.format_exc())
        raise
    finally:
        report["seconds"] = round(time.time() - started, 3)
        atomic_json(output / "acceptance.json", report)
        print(json.dumps({k: report.get(k) for k in
              ("status", "case", "state", "matches", "selected", "remaining", "seconds")},
              ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

