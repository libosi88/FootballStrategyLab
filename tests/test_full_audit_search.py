"""Search correctness and damaged-checkpoint regressions from the full audit."""
from contextlib import closing, contextmanager
from itertools import combinations
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from lab.common import DEFAULT, DIRECTIONS, MISSING, atomic_json, read_json, sha, write_jsonl
from lab.mining import build_direction, first_indices
from lab.normalization import normalize_conditions
from lab.review_cache import NAMES, load_review, save_review
from lab.rules import mask
from lab.standard_atoms import AtomCatalog, W, C
from lab.standard_columns import StandardColumns
from lab.standard_masks import MaskStore
from lab.standard_mining import mine_standard, ordered_standard_events
from lab.standard_search import candidate_families, family_conditions_from, run_class_search
from test_core import event


class Columns(dict):
    def atom_mask(self, atom):
        return mask(atom, dict(self))


@contextmanager
def prepared_search(root):
    columns = Columns(eid=np.arange(16), mid=np.repeat(np.arange(8), 2),
                      side=np.zeros(16, np.int64), pnl=np.tile([-200, 190], 8),
                      water=np.tile([90, 100], 8), minute=np.tile([0, 1], 8))
    config = {**DEFAULT, 'min_profit': '0', '_water_scale': 100,
              'min_free_disk_mb': 0, 'standard_node_budget': 3}
    with AtomCatalog(root / 'atoms.sqlite3', True) as atoms:
        for atom in ({'feature': 'water', 'op': 'ge', 'value': 90},
                     {'feature': 'water', 'op': 'ge', 'value': 100},
                     {'feature': 'minute', 'op': 'eq', 'value': 1},
                     {'feature': 'water', 'op': 'ge', 'value': 110}):
            atoms.add(atom, W | C, 100)
        atoms.commit()
        with MaskStore(root, columns, atoms, config, 'full-audit-search') as masks:
            masks.prepare(lambda **kw: None, lambda: False)
            yield columns, atoms, masks, config


class FullAuditSearch(unittest.TestCase):
    def test_missing_readonly_dictionary_does_not_create_a_database(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'absent'
            with self.assertRaises(sqlite3.OperationalError):
                AtomCatalog(root / 'atoms.sqlite3')
            self.assertFalse(root.exists())

    def test_frozen_dictionary_reader_cannot_modify_rows(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'atoms.sqlite3'
            with AtomCatalog(path, True) as writer:
                writer.add({'feature': 'water', 'op': 'ge', 'value': 90}, W | C, 100)
                writer.commit()
            before = sha(path)
            with AtomCatalog(path) as reader:
                self.assertEqual(reader[0]['value'], 90)
                with self.assertRaises(sqlite3.OperationalError):
                    reader.conn.execute('DELETE FROM atoms')
            self.assertEqual(sha(path), before)

    def test_completed_direction_requires_its_dictionary_seal(self):
        events = [{**event(0, line=8), 'market': 0}]
        labels = {'a': {'eligible': True, 'final': [0, 0], 'year': 2024}}
        config = {**DEFAULT, 'min_free_disk_mb': 0}
        with tempfile.TemporaryDirectory() as td, patch(
                'lab.standard_feature_cache.source_fingerprint', return_value='audit-static-source'):
            state = mine_standard(td, events, labels, 100, 'LIVE_OVER', config,
                                  lambda **kw: None, lambda: False)
            self.assertEqual(state['status'], 'COMPLETE')
            root = Path(td) / 'mining' / 'LIVE_OVER'
            (root / 'dictionary_complete.json').unlink()
            before = self.evidence_hashes(root)
            with self.assertRaisesRegex(ValueError, '字典.*封存'):
                mine_standard(td, events, labels, 100, 'LIVE_OVER', config,
                              lambda **kw: None, lambda: False)
            self.assertEqual(self.evidence_hashes(root), before)
            self.assertFalse((root / 'dictionary_complete.json').exists())

    def stop(self, root, atoms, masks, config):
        result = run_class_search(root, atoms, masks, config, lambda **kw: None,
                                  lambda: False, 'full-audit-search')
        self.assertEqual(result['status'], 'BUDGET_STOP')
        self.assertGreater(result['candidates'], 0)
        return result

    def damage_ledger(self, root):
        with closing(sqlite3.connect(root / 'search.sqlite3')) as conn:
            changed = conn.execute("DELETE FROM ledger WHERE kind='EVALUATED' AND net>0").rowcount
            conn.commit()
        self.assertGreater(changed, 0)

    def evidence_hashes(self, root):
        names = ('state.json', 'standard_plan.json', 'search.sqlite3', 'search_evidence.json')
        return {name: sha(root / name) for name in names if (root / name).exists()}

    def test_resume_rejects_changed_partial_ledger_without_resealing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with prepared_search(root) as (_, atoms, masks, config):
                self.stop(root, atoms, masks, config)
                self.damage_ledger(root)
                before = self.evidence_hashes(root)
                with self.assertRaises(ValueError):
                    run_class_search(root, atoms, masks, {**config, 'standard_node_budget': 0},
                                     lambda **kw: None, lambda: False, 'full-audit-search')
                self.assertEqual(self.evidence_hashes(root), before)

    def test_partial_candidate_read_rejects_changed_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with prepared_search(root) as (_, atoms, masks, config):
                self.stop(root, atoms, masks, config)
                self.damage_ledger(root)
                with self.assertRaises(ValueError):
                    list(candidate_families(root, 0))

    def test_resume_rejects_missing_sealed_artifact(self):
        for name in ('search.sqlite3', 'standard_plan.json', 'state.json', 'search_evidence.json'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                with prepared_search(root) as (_, atoms, masks, config):
                    self.stop(root, atoms, masks, config)
                    (root / name).unlink()
                    before = self.evidence_hashes(root)
                    with self.assertRaises(ValueError):
                        run_class_search(root, atoms, masks, config, lambda **kw: None,
                                         lambda: False, 'full-audit-search')
                    self.assertEqual(self.evidence_hashes(root), before)

    def test_partial_candidates_reject_missing_public_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with prepared_search(root) as (_, atoms, masks, config):
                self.stop(root, atoms, masks, config)
                (root / 'state.json').unlink()
                with self.assertRaises(ValueError):
                    list(candidate_families(root, 0))

    def test_review_cache_requires_every_output_checksum(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            save_review(root, 'review-fixture', {'done': True},
                        **{name: [{'id': name}] for name in NAMES})
            manifest = read_json(root / 'manifest.json')
            del manifest['files']['rows.jsonl.gz']
            atomic_json(root / 'manifest.json', manifest)
            write_jsonl(root / 'rows.jsonl.gz', [{'id': 'changed-without-checksum'}])
            with self.assertRaises(ValueError):
                load_review(root, 'review-fixture')

    def test_minute_normalization_preserves_text_halftime_exclusion(self):
        values = np.array([MISSING, -2, -1, 0, 1, 5, 15, 16], np.int64)
        cases = [[('eq', -2), ('le', 15)], [('ge', -2), ('le', -2)],
                 [('range', -2, -1)], [('eq', -2)],
                 [('ge', -2), ('le', 15)], [('ge', 0), ('le', 0)]]
        for comparisons in cases:
            original = [{'feature': 'minute', 'op': entry[0], 'value': entry[1],
                         **({'upper': entry[2]} if len(entry) == 3 else {})}
                        for entry in comparisons]
            for conditions in (original, list(reversed(original))):
                with self.subTest(conditions=conditions):
                    expected = np.ones(len(values), np.bool_)
                    for atom in conditions:
                        expected &= mask(atom, {'minute': values})
                    normalized = normalize_conditions(conditions, 100)
                    actual = np.full(len(values), normalized is not None, np.bool_)
                    for atom in normalized or ():
                        actual &= mask(atom, {'minute': values})
                    np.testing.assert_array_equal(actual, expected)

    def test_valid_partial_resume_preserves_all_profitable_expressions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with prepared_search(root) as (columns, atoms, masks, config):
                self.stop(root, atoms, masks, config)
                result = run_class_search(root, atoms, masks, {**config, 'standard_node_budget': 0},
                                          lambda **kw: None, lambda: False, 'full-audit-search')
                self.assertEqual(result['status'], 'COMPLETE')
                self.assert_candidates_equal_reference(root, result, columns, atoms)

    def test_partial_candidates_reject_downgraded_public_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with prepared_search(root) as (_, atoms, masks, config):
                self.stop(root, atoms, masks, config)
                state = read_json(root / 'state.json')
                state['status'] = 'RUNNING'
                atomic_json(root / 'state.json', state)
                with self.assertRaises(ValueError):
                    list(candidate_families(root, 0))

    def assert_candidates_equal_reference(self, root, result, base, atoms):
        expected = {}
        for ids in [(), *((i,) for i in range(len(atoms))), *combinations(range(len(atoms)), 2)]:
            indices = first_indices(ids, base, atoms)
            net = sum(int(base['pnl'][i]) for i in indices)
            if net > 0:
                expected[ids] = (len(indices), net)
        actual = {}
        for family in candidate_families(root, 0):
            for ids in family_conditions_from(family['block'], family['anchor'], family['prefix']):
                self.assertNotIn(ids, actual)
                actual[ids] = (family['n'], family['net'])
        self.assertEqual(actual, expected)
        self.assertEqual(result['candidates'], len(expected))

    def test_all_sixteen_directions_match_unpruned_first_event_reference(self):
        events = []
        labels = {}
        for mid, final in enumerate(([2, 1], [0, 0], [1, 2])):
            sid = 'audit-' + str(mid)
            labels[sid] = {'eligible': True, 'year': 2024 + mid, 'final': final}
            for phase in (0, 1):
                for step in range(6):
                    for market in (0, 1):
                        quote = event(len(events),
                                      line=(0, 1, -1, 2, 0, -2)[step] if market else 8 + step % 3,
                                      w0=90 + step * 2, w1=100 - step,
                                      ts=phase * 20 + step, valid=step != 3)
                        quote.update(sid=sid, mid=mid, phase=phase, market=market,
                                     status='滚' if phase else '即')
                        events.append(quote)
        events = ordered_standard_events(events)
        config = {**DEFAULT, 'min_profit': '0', '_water_scale': 100,
                  'min_free_disk_mb': 0, 'standard_node_budget': 0}
        with tempfile.TemporaryDirectory() as td:
            for direction in DIRECTIONS:
                with self.subTest(direction=direction):
                    root = Path(td) / direction
                    base = build_direction(events, labels, 100, direction, config)
                    self.assertGreater(len(base['eid']), 0)
                    with AtomCatalog(root / 'atoms.sqlite3', True) as atoms:
                        for feature, value in (('water', 90), ('water', 95),
                                               ('line_init', 0), ('line_init', 1)):
                            atoms.add({'feature': feature, 'op': 'ge', 'value': value}, W | C, 100)
                        atoms.commit()
                        with StandardColumns(base, events, direction) as columns:
                            with MaskStore(root, columns, atoms, config, direction) as masks:
                                masks.prepare(lambda **kw: None, lambda: False)
                                result = run_class_search(root, atoms, masks, config,
                                                          lambda **kw: None, lambda: False, direction)
                        self.assertEqual(result['status'], 'COMPLETE')
                        self.assertEqual(result['raw_total'], 11)
                        self.assert_candidates_equal_reference(root, result, base, atoms)


if __name__ == '__main__':
    unittest.main()
