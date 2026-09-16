"""Offline assignment must bind every input used by a league task."""
import tempfile
import unittest
from pathlib import Path
from lab.fleet import make_plans, import_plan
from lab.common import atomic_json, sha, write_jsonl
from lab.data import verify_prepared_inputs
from lab.store import Store
from test_v031 import quote, write_csv


class FleetInputVersions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / 'input'
        write_csv(self.data / 'quotes.csv', [quote()])
        self.workspace = self.root / 'workspace'
        self.serial = 0

    def create(self):
        self.serial += 1
        output = self.root / str(self.serial)
        make_plans([str(self.data)], output, 1, '皇冠')
        return import_plan(output / 'node_001.json', self.data, self.workspace)['jobs'][0]

    def test_changed_result_index_creates_new_task(self):
        path = self.data / 'index.csv'
        fields = ['sId', '联赛', '状态', '全场比分']
        row = {'sId': 'one', '联赛': '真实甲组', '状态': '完', '全场比分': '2-1'}
        write_csv(path, [row], fields)
        first = self.create()
        old = Store(self.workspace).get(first)
        write_csv(path, [{**row, '全场比分': '3-1'}], fields)
        self.assertNotEqual(first, self.create())
        self.assertEqual(Store(self.workspace).get(first), old)

    def test_changed_quality_exclusion_creates_new_task(self):
        path = self.data / 'exclude_sids.csv'
        fields = ['sId', 'reason', 'scope', 'source']
        row = {'sId': 'one', 'reason': 'remaining_suspicious', 'scope': 'all', 'source': 'audit'}
        write_csv(path, [row], fields)
        first = self.create()
        old = Store(self.workspace).get(first)
        write_csv(path, [{**row, 'reason': 'pending_score_verify'}], fields)
        self.assertNotEqual(first, self.create())
        self.assertEqual(Store(self.workspace).get(first), old)

    def test_identical_complete_inputs_reuse_paused_task(self):
        first = self.create()
        self.assertEqual(first, self.create())
        self.assertEqual(len(Store(self.workspace).jobs()), 1)
        self.assertEqual(Store(self.workspace).get(first)['status'], 'PAUSED')


class PreparedInputEvidence(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        write_jsonl(self.root / 'events.jsonl.gz', [])
        atomic_json(self.root / 'labels.json', {})
        atomic_json(self.root / 'data_audit.json', {'water_scale': 100})
        atomic_json(self.root / 'holdout_plan.json', {'status': 'FULL_HISTORY'})
        files = ('events.jsonl.gz', 'labels.json', 'data_audit.json', 'holdout_plan.json')
        self.manifest = {name: sha(self.root / name) for name in files}

    def verify(self, manifest, config=None):
        atomic_json(self.root / 'prepared_manifest.json', manifest)
        return verify_prepared_inputs(self.root, config or {})

    def test_complete_prepared_manifest_is_accepted(self):
        self.assertEqual(self.verify(self.manifest), self.manifest)

    def test_omitted_label_hash_does_not_disable_verification(self):
        with self.assertRaises(ValueError):
            self.verify({name: value for name, value in self.manifest.items() if name != 'labels.json'})

    def test_empty_manifest_is_rejected(self):
        with self.assertRaises(ValueError):
            self.verify({})

    def test_changed_labels_are_rejected(self):
        atomic_json(self.root / 'labels.json', {'changed': True})
        with self.assertRaises(ValueError):
            self.verify(self.manifest)

    def test_undeclared_path_is_rejected(self):
        with self.assertRaises(ValueError):
            self.verify({**self.manifest, '../outside.json': 'untrusted'})

    def test_split_requires_both_holdout_files(self):
        atomic_json(self.root / 'holdout_plan.json', {'status': 'SPLIT'})
        self.manifest['holdout_plan.json'] = sha(self.root / 'holdout_plan.json')
        with self.assertRaises(ValueError):
            self.verify(self.manifest)

    def test_partition_keeps_its_explicit_three_file_contract(self):
        manifest = {name: value for name, value in self.manifest.items() if name != 'holdout_plan.json'}
        self.assertEqual(self.verify(manifest, {'research_partition': {'kind': 'walk_forward_training'}}), manifest)


if __name__ == '__main__':
    unittest.main()
