"""Execution and handoff regressions using isolated quotes and sealed ledgers."""
import copy
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import zipfile
import test_packaging_publication as publication
from lab import packaging

from lab.common import atomic_json, read_json, read_jsonl, sha
from lab.contracts import EVENT_FIELDS, validate_event
from lab.handoff_assets import event_schema, write_assets, verify_scalar_cases
from lab.paper_runner import StreamingPaperRunner
from lab.paper_execution import PaperDispatcher
from lab.search_integrity import seal_search_evidence
from lab.standard_atoms import AtomCatalog, W, C
from lab.standard_cli import export_candidates
from lab.workflow_gates import module_coverage
from test_core import event, TEST_CONTRACT
from test_paper_runner import standard_packet
from test_paper_execution import packet, offer


def schema_accepts(value, schema):
    """Independent evaluator for the primitive keywords emitted by this schema."""
    kinds = {"object": dict, "array": list, "string": str,
             "integer": int, "boolean": bool}
    if "type" in schema and type(value) is not kinds[schema["type"]]:
        return False
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if "minimum" in schema and value < schema["minimum"]:
        return False
    if "minLength" in schema and len(value) < schema["minLength"]:
        return False
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not set(schema.get("required", ())) <= value.keys():
            return False
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            return False
        return all(schema_accepts(v, properties[k]) for k, v in value.items() if k in properties)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            return False
        return all(schema_accepts(v, schema.get("items", {})) for v in value)
    return True


def candidate_job(root, status="BUDGET_STOP", candidates=1):
    folder = root / "mining" / "LIVE_OVER"
    folder.mkdir(parents=True)
    atomic_json(root / "config.json", {"directions": ["LIVE_OVER"], "min_profit": 0})
    atomic_json(root / "data_audit.json", {"water_scale": 100})
    with AtomCatalog(folder / "atoms.sqlite3", True) as atoms:
        atoms.add({"feature": "water", "op": "ge", "value": 90}, W | C, 100)
        atoms.commit()
    atomic_json(folder / "dictionary_complete.json", {"database_sha256": sha(folder / "atoms.sqlite3")})
    block = {"module": "M01", "k": 0, "anchors": [{"members": [0]}], "pool": [], "sizes": []}
    atomic_json(folder / "standard_plan.json", {"binding": "audit", "blocks": [block]})
    atomic_json(folder / "search_spec.json", {"binding": "audit", "modules": {"M01": 1}})
    atomic_json(folder / "state.json", {"status": status, "search_backend": "standard_class_dfs",
                                        "binding": "audit", "candidates": candidates})
    with closing(sqlite3.connect(folder / "search.sqlite3")) as conn:
        conn.execute("CREATE TABLE ledger(id INTEGER PRIMARY KEY, block INTEGER, anchor INTEGER, prefix TEXT, kind TEXT, weight TEXT, mask TEXT, n INTEGER, net INTEGER)")
        conn.execute("INSERT INTO ledger VALUES(1,0,0,'[]','EVALUATED','1','fixture',2,200)")
        conn.commit()
    backend = "standard_class_dfs" if status == "COMPLETE" else "standard_class_dfs_partial"
    seal_search_evidence(folder, backend, "audit")
    return folder


class FullAuditExecution(unittest.TestCase):
    def test_schema_required_fields_have_declared_properties(self):
        schema = event_schema(TEST_CONTRACT)
        self.assertLessEqual(set(schema["required"]), set(schema["properties"]))
        self.assertEqual(set(schema["properties"]), EVENT_FIELDS)
        self.assertNotIn("quality_blocked", schema["required"])

    def test_schema_quality_flag_is_optional_boolean_and_keeps_unknown_fields_closed(self):
        schema = event_schema(TEST_CONTRACT)
        for quality in (None, False, True):
            quote = event(0)
            if quality is not None:
                quote.update(quality_blocked=quality, valid=not quality)
            validate_event(quote, TEST_CONTRACT)
            self.assertTrue(schema_accepts(quote, schema), quality)
        for bad in (0, 1, "false", None, [], {}):
            quote = {**event(0), "quality_blocked": bad}
            self.assertFalse(schema_accepts(quote, schema), repr(bad))
            with self.assertRaises(ValueError):
                validate_event(quote, TEST_CONTRACT)
        self.assertFalse(schema_accepts({**event(0), "unregistered": False}, schema))

    def test_candidate_export_checks_zero_state_against_its_seal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = candidate_job(root)
            state = read_json(folder / "state.json")
            atomic_json(folder / "state.json", {**state, "candidates": 0})
            target = root / "export.jsonl.gz"
            with self.assertRaises(ValueError):
                export_candidates(root, target)
            self.assertFalse(target.exists())
            self.assertFalse(list(root.glob("*.partial_*")))

    def test_candidate_export_rejects_changed_partial_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = candidate_job(root)
            with closing(sqlite3.connect(folder / "search.sqlite3")) as conn:
                conn.execute("UPDATE ledger SET net=-200")
                conn.commit()
            target = root / "export.jsonl"
            with self.assertRaises(ValueError):
                export_candidates(root, target)
            self.assertFalse(target.exists())

    def test_candidate_export_rejects_unsealed_no_data_claim(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = candidate_job(root)
            atomic_json(folder / "state.json", {"status": "NO_DATA", "candidates": 0})
            with self.assertRaises(ValueError):
                export_candidates(root, root / "export.jsonl")

    def test_valid_partial_candidates_are_still_exported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate_job(root)
            target = root / "export.jsonl.gz"
            result = export_candidates(root, target)
            rows = list(read_jsonl(target))
            self.assertEqual(result["status"], "EXPORTED")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["net_i"], 200)
            self.assertEqual(rows[0]["atom_ids"], [0])

    def test_partial_coverage_without_backend_still_requires_its_seal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = candidate_job(root)
            with closing(sqlite3.connect(folder / "search.sqlite3")) as conn:
                conn.execute("DELETE FROM ledger")
                conn.commit()
            coverage = {"units": {"water": 100}, "standard_search_complete": False,
                        "directions": {"LIVE_OVER": {"status": "BUDGET_STOP"}}}
            result = module_coverage(root, coverage)
            self.assertFalse(result["evidence_complete"])
            self.assertFalse(result["declared_standard_search_complete"])

    def test_sealed_zero_counter_cannot_hide_profitable_ledger_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate_job(root, candidates=0)
            target = root / "export.jsonl.gz"
            with self.assertRaisesRegex(ValueError, "条数"):
                export_candidates(root, target)
            self.assertFalse(target.exists())

    def test_pending_quote_cannot_be_changed_by_the_feed_caller(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as td:
                with StreamingPaperRunner(Path(td) / "paper.db", standard_packet(), True) as runner:
                    quote = {**event(0, line=8, ts=1), "market": 0}
                    runner.feed(quote)
                    if replacement:
                        quote = {**event(1, line=8, ts=1, w0=90), "market": 0}
                        runner.feed(quote)
                    frozen = copy.deepcopy(quote)
                    quote["water"][0] = 1
                    quote["score"][0] = 7
                    result = runner.flush()
                    self.assertEqual(len(result), 1)
                    actual = runner.dispatcher.intent(result[0]["intent_id"])["body"]["quote"]
                    self.assertEqual(actual, frozen)

    def test_returned_signal_does_not_alias_the_pending_offer(self):
        with tempfile.TemporaryDirectory() as td:
            with StreamingPaperRunner(Path(td) / "paper.db", standard_packet(), True) as runner:
                received = runner.feed({**event(0, line=8, ts=1), "market": 0})
                frozen = copy.deepcopy(received["signals"][0])
                received["signals"][0]["line"] = 12
                received["signals"][0]["score"][0] = 5
                result = runner.flush()
                actual = runner.dispatcher.intent(result[0]["intent_id"])["body"]["signal"]
                self.assertEqual(actual, frozen)

    def test_dispatcher_freezes_constructor_rules(self):
        frozen = packet(["LIVE_OVER", "LIVE_OVER"])
        quote = {**event(0, line=8, ts=1), "market": 0}
        offers = [offer(frozen, index, quote) for index in range(2)]
        with tempfile.TemporaryDirectory() as td:
            with PaperDispatcher(Path(td) / "paper.db", frozen) as dispatcher:
                frozen["rules"][0]["priority"] = 999
                accepted = [row for row in dispatcher.submit_batch(offers) if row["status"] == "RESERVED"]
                self.assertEqual([row["strategy_id"] for row in accepted], ["r0"])

    def test_runner_freezes_constructor_packet(self):
        frozen = copy.deepcopy(standard_packet())
        with tempfile.TemporaryDirectory() as td:
            with StreamingPaperRunner(Path(td) / "paper.db", frozen, True) as runner:
                frozen["contract"]["league"] = "changed after construction"
                result = runner.feed({**event(0, line=8, ts=1), "market": 0})
                self.assertEqual(len(result["signals"]), 1)

    def test_handoff_assets_reject_changed_or_missing_contract_files(self):
        frozen = standard_packet()
        frozen["water_scale"] = 100
        names = ("event.schema.json", "execution_config.json", "integration_manifest.json")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = root / "results"
            atomic_json(folder / "rules.json", frozen)
            for name in names:
                for missing in (False, True):
                    with self.subTest(name=name, missing=missing):
                        write_assets(root)
                        self.assertEqual(verify_scalar_cases(folder)["status"], "PASS")
                        if missing:
                            (folder / name).unlink()
                        else:
                            atomic_json(folder / name, {})
                        with self.assertRaises(ValueError):
                            verify_scalar_cases(folder)

    def test_cached_engine_report_must_match_its_binding(self):
        from lab.workflow_journal import _verify_engine_locked
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "tests").mkdir()
            (root / "tests.log").write_text("previous successful check", encoding="utf-8")
            atomic_json(root / "report.json", {"status": "PASS", "binding": "other",
                        "engine_hash": "engine", "runtime": {}, "source_and_tests_unchanged": True,
                        "log_sha256": sha(root / "tests.log")})
            with patch("lab.workflow_journal.ROOT", root), \
                 patch("lab.workflow_journal.source_fingerprint", return_value="engine"), \
                 patch("lab.packaging.checked_process", return_value=SimpleNamespace(returncode=0)) as child:
                result = _verify_engine_locked(root, "expected", "engine", [], {}, lambda **kw: None, None)
            child.assert_called_once()
            self.assertEqual(result["binding"], "expected")
            self.assertFalse(result["reused"])


class FullAuditPackaging(unittest.TestCase):
    setUp = publication.PackagingPublication.setUp
    fake_check = publication.PackagingPublication.fake_check
    run_package = publication.PackagingPublication.run_package
    assert_unpublished = publication.PackagingPublication.assert_unpublished

    def test_failed_audit_archive_preserves_original_decision_and_gap_notes(self):
        names = ("最终决定.md", "缺口说明.md")
        for name in names:
            (self.root / "results" / name).write_text("previous partial result", encoding="utf-8")
        original = zipfile.ZipFile
        def fail_audit(file, mode="r", *args, **kwargs):
            if Path(file).name.startswith("B_") and mode == "w":
                raise OSError("injected B archive failure")
            return original(file, mode, *args, **kwargs)
        with patch.object(packaging.zipfile, "ZipFile", side_effect=fail_audit):
            with self.assertRaisesRegex(OSError, "B archive failure"):
                self.run_package()
        self.assert_unpublished()
        for name in names:
            self.assertEqual((self.root / "results" / name).read_text(encoding="utf-8"),
                             "previous partial result", name)

    def test_published_decision_matches_both_archives(self):
        packages, _ = self.run_package()
        for kind in ("developer", "audit"):
            with zipfile.ZipFile(self.root / "packages" / packages[kind]) as archive:
                for name in ("最终决定.md", "缺口说明.md"):
                    self.assertEqual(archive.read("results/" + name),
                                     (self.root / "results" / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
