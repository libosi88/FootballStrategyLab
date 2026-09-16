"""Independent input-boundary regressions from the comprehensive project audit."""
import csv
import tempfile
import unittest
from pathlib import Path

from lab.common import MISSING, check_config, sha, timestamp
from lab.data import inspect, load_inputs
from test_v031 import FIELDS, quote, write_csv


class FullAuditData(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def rows(self, first=None, second=None):
        a, b = quote(), quote()
        a.update(first or {})
        b.update({"变化时间": "2024-03-01 20:21"})
        b.update(second or {})
        return [a, b]

    def load(self, rows):
        path = self.root / "quotes.csv"
        write_csv(path, rows)
        original = sha(path)
        result = load_inputs(inspect([str(path)], self.root / "cache"), "真实甲组", "皇冠")
        self.assertEqual(sha(path), original)
        return result

    def test_missing_final_before_complete_label_is_merged(self):
        events, labels, _, _ = self.load(self.rows({"全场比分": ""}))
        self.assertEqual(len(events), 2)
        self.assertTrue(labels["one"]["eligible"])
        self.assertEqual(labels["one"]["final"], [2, 1])

    def test_missing_final_after_complete_label_does_not_conflict(self):
        _, labels, _, _ = self.load(self.rows(second={"全场比分": ""}))
        self.assertTrue(labels["one"]["eligible"])
        self.assertEqual(labels["one"]["final"], [2, 1])

    def test_missing_optional_metadata_is_merged_in_either_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                partial = {"开球时间": "", "比赛状态": ""}
                rows = self.rows(second=partial) if reverse else self.rows(first=partial)
                _, labels, _, _ = self.load(rows)
                self.assertTrue(labels["one"]["eligible"])
                self.assertEqual(labels["one"]["result_status"], "完")
                self.assertEqual(labels["one"]["kickoff"], timestamp("2024-03-01 19:30"))

    def test_conflicting_complete_final_stays_excluded(self):
        _, labels, _, audit = self.load(self.rows(second={"全场比分": "3-1"}))
        self.assertFalse(labels["one"]["eligible"])
        self.assertTrue(labels["one"].get("conflict"))
        self.assertGreater(audit["label_conflicts_by_field"].get("final", 0), 0)

    def test_all_missing_final_does_not_invent_result(self):
        _, labels, _, _ = self.load(self.rows({"全场比分": ""}, {"全场比分": ""}))
        self.assertFalse(labels["one"]["eligible"])
        self.assertEqual(labels["one"]["final"], [MISSING, MISSING])

    def test_malformed_nonempty_final_remains_excluded(self):
        _, labels, _, _ = self.load(self.rows({"全场比分": "not-a-score"}))
        self.assertFalse(labels["one"]["eligible"])

    def duplicate_file(self, name, fields, values):
        path = self.root / name
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerow(values)
        return path

    def duplicate_quotes(self):
        row = quote()
        return self.duplicate_file("duplicate_quotes.csv", [*FIELDS, "上水/大球"],
                                   [row.get(field, "") for field in FIELDS] + ["9.99"])

    def test_duplicate_quote_header_is_rejected_at_inspection(self):
        path = self.duplicate_quotes()
        with self.assertRaisesRegex(ValueError, "重复|duplicate"):
            inspect([str(path)], self.root / "cache")

    def test_duplicate_quote_header_is_rejected_by_direct_loader(self):
        path = self.duplicate_quotes()
        manifest = {"files": [{"path": str(path), "sha256": sha(path), "kind": "quotes"}]}
        with self.assertRaisesRegex(ValueError, "重复|duplicate"):
            load_inputs(manifest, "真实甲组", "皇冠")

    def test_duplicate_index_header_is_rejected(self):
        source = self.root / "quotes.csv"
        write_csv(source, [quote()])
        index = self.duplicate_file("index.csv", ["sId", "联赛", "状态", "全场比分", "全场比分"],
                                    ["one", "真实甲组", "完", "2-1", "9-0"])
        with self.assertRaisesRegex(ValueError, "重复|duplicate"):
            inspect([str(source), str(index)], self.root / "cache")

    def test_duplicate_quality_header_is_rejected(self):
        source = self.root / "quotes.csv"
        write_csv(source, [quote()])
        quality = self.duplicate_file("exclude_sids.csv", ["sId", "reason", "scope", "source", "reason"],
                                      ["one", "pending_score_verify", "all", "audit", "remaining_suspicious"])
        with self.assertRaisesRegex(ValueError, "重复|duplicate"):
            inspect([str(source), str(quality)], self.root / "cache")


class FullAuditConfiguration(unittest.TestCase):
    def test_disabled_execution_flags_must_be_actual_booleans(self):
        for field in ("live_enabled", "second_slot_enabled"):
            self.assertIs(check_config({field: False})[field], False)
            for value in (0, None, [], ""):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    check_config({field: value})

    def test_company_must_be_text(self):
        for value in (0, 1, [], ["皇冠"], {"name": "皇冠"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_config({"company": value})

    def test_invalid_direction_types_raise_configuration_error(self):
        for value in ([{}], [["LIVE_OVER"]], [None]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_config({"directions": value})


if __name__ == "__main__":
    unittest.main()
