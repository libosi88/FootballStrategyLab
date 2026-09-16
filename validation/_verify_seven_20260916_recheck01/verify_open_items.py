"""Read-only source audit with disposable synthetic inputs and paper ledgers.

Run from the project root with .venv\Scripts\python.exe -B <this file>.
Only this script's audit directory receives new files. No real jobs are opened.
"""
import csv
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch

ROOT = Path.cwd().resolve()
OUT = Path(__file__).resolve().parent
if not (ROOT / "lab/paper_runner.py").is_file() or ROOT not in OUT.parents:
    raise SystemExit("Run from the FootballStrategyLab project root.")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from lab.common import VERSION, source_fingerprint
from lab.release import release_files, release_fingerprint
from lab.paper_runner import StreamingPaperRunner
from lab.paper_execution import PaperDispatcher
from lab.data import inspect, load_inputs
from test_core import event
from test_paper_execution import packet, offer
from test_v031 import quote, write_csv


def manifest():
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in release_files(ROOT)}


def clock_case(base, inject_failure):
    p = packet(["LIVE_OVER", "LIVE_GIVE"])
    path = base / "paper.sqlite3"
    evidence = {"alarm_failure_injected": inject_failure}
    with StreamingPaperRunner(path, p) as runner:
        runner.declare_new_match("a")
        first = {**event(0, line=8, ts=1), "market": 0}
        runner.feed(first)
        if inject_failure:
            with patch.object(runner.dispatcher, "alarm", side_effect=OSError(28, "AUDIT_ONLY_LOG_FAILURE")):
                try:
                    runner.flush(before=100, sid="a")
                except OSError as error:
                    evidence["injected_exception"] = str(error)
        else:
            evidence["first_flush"] = runner.flush(before=100, sid="a")
        evidence["durable_signals_before_close"] = [json.loads(r[0]) for r in
            runner.dispatcher.conn.execute("SELECT body FROM signals ORDER BY rowid")]
        evidence["ledger_clocks_before_close"] = [list(r) for r in
            runner.dispatcher.conn.execute("SELECT sid,minute FROM clocks ORDER BY sid")]
    with StreamingPaperRunner(path, p) as runner:
        evidence["restored_watermarks"] = dict(runner.watermarks)
        try:
            evidence["earlier_feed"] = runner.feed(event(1, line=2, ts=50))
            evidence["earlier_flush"] = runner.flush(before=51, sid="a")
        except ValueError as error:
            evidence["earlier_input_rejected"] = str(error)
        evidence["resulting_intents"] = [dict(r) for r in
            runner.dispatcher.conn.execute("SELECT status,filled,body FROM intents ORDER BY rowid")]
        for row in evidence["resulting_intents"]:
            row["body"] = json.loads(row["body"])
    evidence["created_intent_before_observed_100"] = any(
        row["body"]["execution_ts"] < 100 for row in evidence["resulting_intents"])
    return evidence


def partial_settlement_case(base, cancel_first):
    p = packet(["LIVE_GIVE"])
    with PaperDispatcher(base / "paper.sqlite3", p) as dispatcher:
        iid = dispatcher.submit_batch([offer(p, 0, event(0, line=2, ts=1))])[0]["intent_id"]
        dispatcher.receipt(iid, "part", "PARTIAL", "0.4")
        if cancel_first:
            dispatcher.receipt(iid, "cancel", "CANCELLED", "0.4", True)
        result = {"cancel_first": cancel_first}
        try:
            dispatcher.receipt(iid, "settle", "SETTLED", "0.4", True)
            result["settlement_accepted"] = True
        except ValueError as error:
            result.update(settlement_accepted=False, error=str(error))
        row = dispatcher.intent(iid)
        result.update(final_status=row["status"], filled_units=row["filled"] / 1_000_000,
                      reserved_units=row["reserved"] / 1_000_000)
        return result


def index_case(base, score_text):
    base.mkdir()
    quotes = base / "quotes.csv"
    write_csv(quotes, [quote()])
    index = base / "index.csv"
    with index.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["sId", "联赛", "状态", "全场比分"])
        writer.writerow(["one", "真实甲组", "完", score_text])
    events, labels, scale, audit = load_inputs(
        inspect([str(quotes), str(index)], base / "cache"), "真实甲组", "皇冠")
    return {"index_score": score_text, "label": labels["one"], "audit": audit["counts"],
            "events": len(events), "water_scale": scale}


def main():
    destination = OUT / "probe_results.json"
    if destination.exists():
        raise FileExistsError("Audit results already exist; do not overwrite evidence.")
    before = manifest()
    report = {"scope": "TARGETED_SYNTHETIC_PROBES_NOT_FULL_ACCEPTANCE", "version": VERSION,
              "engine_hash": source_fingerprint(), "release_hash": release_fingerprint(),
              "real_jobs_started": False, "production_source_modified": False, "probes": {}}
    with tempfile.TemporaryDirectory(prefix="synthetic_", dir=OUT) as directory:
        base = Path(directory).resolve()
        if OUT not in base.parents:
            raise RuntimeError("Synthetic directory escaped audit output root.")
        failed = clock_case(base / "clock_failure", True)
        control = clock_case(base / "clock_control", False)
        report["probes"]["S01"] = {"status": "BUG_REPRODUCED" if
            failed["created_intent_before_observed_100"] and not control["created_intent_before_observed_100"]
            else "NOT_REPRODUCED_REVIEW_DETAILS", "failure": failed, "control": control}
        cancelled = partial_settlement_case(base / "partial_cancel", True)
        direct = partial_settlement_case(base / "partial_direct", False)
        report["probes"]["S02"] = {"status": "BUG_REPRODUCED" if
            not cancelled["settlement_accepted"] and direct["settlement_accepted"]
            else "NOT_REPRODUCED_REVIEW_DETAILS", "cancelled": cancelled, "control": direct}
        malformed = index_case(base / "index_malformed", "not-a-score")
        blank = index_case(base / "index_blank", "")
        conflict = index_case(base / "index_conflict", "3-1")
        fixed = (not malformed["label"]["eligible"] and
                 malformed["audit"].get("malformed_index_label_rows") == 1 and
                 blank["label"]["eligible"] and not conflict["label"]["eligible"])
        report["probes"]["S03"] = {"status": "FIX_VERIFIED" if fixed else "UNEXPECTED_RESULT",
                                   "malformed": malformed, "blank_control": blank, "conflict_control": conflict}
    old_dir = ROOT / "validation/delivery_audit_20260915_r1"
    old_install = json.loads((old_dir / "clean_install.json").read_text(encoding="utf-8"))
    raw = (old_dir / "clean_install.log").read_bytes()
    text = raw.decode("utf-8", errors="replace")
    count = re.search(r"Ran (\d+) tests", text)
    skipped = re.search(r"OK \(skipped=(\d+)\)", text)
    report["historical_install_reconciliation"] = {
        "log_sha256": hashlib.sha256(raw).hexdigest(),
        "log_matches_original_hash": hashlib.sha256(raw).hexdigest() == old_install["log_sha256"],
        "tests_total": int(count[1]) if count else None,
        "tests_skipped": int(skipped[1]) if skipped else 0,
        "setup_exit_code": old_install["exit_code"],
        "recorded_engine_hash": old_install["engine_hash"],
        "rerun_performed": False, "certifies_current_source": False}
    old_inventory = json.loads((old_dir / "source_inventory.json").read_text(encoding="utf-8"))
    prior = {item["path"]: item["sha256"] for item in old_inventory}
    report["changes_since_prior_audit"] = {
        "changed": [p for p in prior if p in before and prior[p] != before[p]],
        "removed": [p for p in prior if p not in before],
        "added": [p for p in before if p not in prior]}
    report["source_manifest_unchanged_during_probes"] = before == manifest()
    report["checks"] = {"synthetic_cases_executed": 7, "full_regression_executed": False,
                         "real_league_search_executed": False}
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["source_manifest_unchanged_during_probes"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
