"""Semantics, integrity, order and cooperative-pause regressions for preparation."""
from collections import defaultdict
from contextlib import contextmanager, closing
from pathlib import Path
from unittest.mock import patch
import sqlite3
import tempfile
import unittest

import numpy as np

from lab.common import DEFAULT, MISSING, digest
# Explicit 0.5 validation policy; the product default is now historical research.
from lab.common import VALIDATION_DEFAULT as DEFAULT
from lab.mining import Paused, build_direction
from lab.rules import mask as reference_mask
from lab.standard_atoms import AtomCatalog, W, C, T, X, P
from lab.standard_columns import StandardColumns
from lab.standard_kernels import bound_window_columns, path_predicate
from lab.standard_masks import MaskStore, _ScalarPrefixes
from lab.standard_search import build_blocks, run_class_search
from test_core import event


class ReferenceColumns(dict):
    def __init__(self, base):
        super().__init__(base)
        self.fallback_calls = []

    def column(self, name):
        return self[name]

    def atom_mask(self, atom):
        self.fallback_calls.append(atom)
        return reference_mask(atom, dict(self))


class LegacyGroups:
    def __init__(self, store):
        self.store = store

    def groups(self, ids):
        selected = set(ids)
        result = defaultdict(list)
        for atom, key, checksum in self.store.conn.execute('SELECT atom,hash,checksum FROM members ORDER BY atom'):
            if checksum != digest([self.store.binding, atom, key]):
                raise ValueError('bad reference member checksum')
            if atom in selected:
                result[key].append(atom)
        # Registration (priority) order of each class's first member, exactly as the store publishes groups.
        return [{'hash': key, 'members': members} for key, members in sorted(result.items(), key=lambda item: item[1][0])]


@contextmanager
def prepared_store():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        base = {'eid': np.arange(12), 'mid': np.repeat(np.arange(3), 4),
                'pnl': np.array([-200, 140, 180, 0, 160, -200, 150, 0, -100, 0, 180, -200]),
                'water': np.array([MISSING, 70, 90, 85, 80, 100, 75, 90, 70, 80, 90, 85]),
                'line': np.tile([-2, 0, 1, 2], 3), 'path1_keep': np.tile([1, 2, 1, 2], 3),
                'pulse_keep': np.tile([0, 1, 1, 0], 3), 'cross_age': np.tile([0, 1, 2, 3], 3),
                'pre_line': np.tile([0, 1, 1, 2], 3)}
        columns = ReferenceColumns(base)
        with AtomCatalog(root / 'atoms.sqlite3', True) as atoms:
            for atom, groups in (
                ({'feature': 'water', 'op': 'eq', 'value': 80}, W | C),
                ({'feature': 'water', 'op': 'le', 'value': 85}, W | C),
                ({'feature': 'water', 'op': 'ge', 'value': 85}, W),
                ({'feature': 'water', 'op': 'range', 'value': 70, 'upper': 90}, W | C),
                ({'feature': 'line', 'op': 'ge', 'value': 1}, W | C),
                ({'feature': 'path1_keep', 'op': 'eq', 'value': 1, 'pulse': True}, W | C),
                ({'feature': 'path1_keep', 'op': 'eq', 'value': 2}, T),
                ({'feature': 'cross_age', 'op': 'ge', 'value': 1}, X),
                ({'feature': 'pre_line', 'op': 'ge', 'value': 1}, P),
            ):
                atoms.add(atom, groups, 100)
            atoms.commit()
            with MaskStore(root, columns, atoms, {**DEFAULT, 'max_feature_cache_mb': 1}, 'preparation-regression') as store:
                store.prepare(lambda **kw: None, lambda: False)
                yield root, columns, atoms, store


class ScalarPrefixPreparation(unittest.TestCase):
    def test_signed_missing_boundaries_and_eviction_match_direct_reference(self):
        values = np.array([MISSING, -3, -1, 0, 1, 3] * 39, np.int64)
        # Includes inter-match padding and word boundaries, not a contiguous bit buffer.
        positions = np.r_[np.arange(73), np.arange(128, 211), np.arange(256, 334)]
        self.assertEqual(len(positions), len(values))
        buffer = np.zeros(384, np.uint8)
        prefix = _ScalarPrefixes(values, positions, buffer, 48)
        atoms = [{'feature': 'water', 'op': op, 'value': value}
                 for value in range(-5, 6) for op in ('eq', 'le', 'ge')]
        atoms += [{'feature': 'water', 'op': 'range', 'value': lo, 'upper': hi}
                  for lo in range(-5, 6) for hi in range(-5, 6)]
        for atom in atoms + list(reversed(atoms)):
            expected = np.zeros(384, np.uint8)
            expected[positions] = reference_mask(atom, {'water': values})
            packed = np.packbits(expected, bitorder='little').view('<u8')
            np.testing.assert_array_equal(prefix.predicate(atom), packed)
            self.assertLessEqual(prefix.bytes, 48)

    def test_all_missing_and_constant_columns_remain_exact(self):
        for values in (np.full(65, MISSING), np.full(65, -2)):
            prefix = _ScalarPrefixes(values, np.arange(65), np.zeros(128, np.uint8), 16)
            for op in ('eq', 'le', 'ge', 'range'):
                atom = {'feature': 'water', 'op': op, 'value': -2, 'upper': 3}
                expected = np.zeros(128, np.uint8)
                expected[:65] = reference_mask(atom, {'water': values})
                np.testing.assert_array_equal(prefix.predicate(atom), np.packbits(expected, bitorder='little').view('<u8'))

    def test_actual_preparation_preserves_all_masks_first_events_and_safe_bounds(self):
        with prepared_store() as (_, columns, atoms, store):
            for atom_id in range(len(atoms)):
                truth = reference_mask(atoms[atom_id], dict(columns))
                expected = np.zeros(len(store.buffer), np.uint8)
                expected[store.positions] = truth
                key = store.conn.execute('SELECT hash FROM members WHERE atom=?', (atom_id,)).fetchone()[0]
                np.testing.assert_array_equal(store.get(key), np.packbits(expected, bitorder='little').view('<u8'))
                first = []
                upper = 0
                for mid in np.unique(columns['mid']):
                    indices = np.flatnonzero(truth & (columns['mid'] == mid))
                    if len(indices):
                        first.append(int(columns['pnl'][indices[0]]))
                        upper += max(0, max(int(columns['pnl'][i]) for i in indices))
                self.assertEqual(store.statistics(key), (len(first), sum(first), upper))
            self.assertEqual(len(columns.fallback_calls), 1)
            self.assertTrue(columns.fallback_calls[0]['pulse'])


class CachedMemberGroups(unittest.TestCase):
    def test_all_blocks_and_member_order_match_uncached_reference_with_one_scan(self):
        with prepared_store() as (_, _, atoms, store):
            expected = build_blocks(atoms, LegacyGroups(store))
            statements = []
            store.conn.set_trace_callback(statements.append)
            actual = build_blocks(atoms, store)
            self.assertEqual(actual, expected)
            self.assertEqual(build_blocks(atoms, store), expected)
            scans = [sql for sql in statements if sql.startswith('SELECT atom,hash,checksum FROM members')]
            self.assertEqual(len(scans), 1)
            self.assertEqual(store.groups([3, 1, 3]), LegacyGroups(store).groups([3, 1, 3]))

    def test_local_member_tampering_invalidates_snapshot(self):
        with prepared_store() as (_, _, _, store):
            store.groups([0, 1])
            store.conn.execute("UPDATE members SET checksum='changed' WHERE atom=0")
            with self.assertRaisesRegex(ValueError, '映射校验失败'):
                store.groups([0, 1])

    def test_external_member_tampering_invalidates_snapshot(self):
        with prepared_store() as (root, _, _, store):
            store.groups([0, 1])
            with closing(sqlite3.connect(root / 'masks.sqlite3')) as other:
                other.execute("UPDATE members SET checksum='changed' WHERE atom=0")
                other.commit()
            with self.assertRaisesRegex(ValueError, '映射校验失败'):
                store.groups([0, 1])

    def test_missing_middle_member_is_rejected_before_plan_construction(self):
        with prepared_store() as (_, _, atoms, store):
            store.conn.execute('DELETE FROM members WHERE atom=2')
            with self.assertRaisesRegex(ValueError, '成员不完整或越界'):
                build_blocks(atoms, store)
            self.assertIsNone(store._members_snapshot)

    def test_extra_out_of_range_member_is_rejected_even_with_valid_checksum(self):
        with prepared_store() as (_, _, atoms, store):
            extra = len(atoms)
            key = store.conn.execute('SELECT hash FROM members WHERE atom=0').fetchone()[0]
            store.conn.execute('INSERT INTO members VALUES(?,?,?)', (extra, key, digest([store.binding, extra, key])))
            with self.assertRaisesRegex(ValueError, '成员不完整或越界'):
                build_blocks(atoms, store)
            self.assertIsNone(store._members_snapshot)

    def test_pause_during_snapshot_publishes_nothing_and_resume_is_exact(self):
        with prepared_store() as (_, _, atoms, store):
            updates = []
            store.set_group_control(lambda **kw: updates.append(kw), lambda: bool(updates))
            with self.assertRaises(Paused):
                store.groups(atoms.ids(W))
            self.assertIsNone(store._members_snapshot)
            store.set_group_control(lambda **kw: None, lambda: False)
            self.assertEqual(store.groups(atoms.ids(W)), LegacyGroups(store).groups(atoms.ids(W)))

    def test_search_pauses_before_any_block_build_using_legacy_two_arg_signature(self):
        with prepared_store() as (root, _, atoms, store):
            with patch('lab.standard_search.build_blocks', side_effect=AssertionError('should not build')):
                with self.assertRaises(Paused):
                    run_class_search(root, atoms, store, DEFAULT, lambda **kw: None, lambda: True, 'preparation-regression')
            self.assertFalse((root / 'standard_plan.json').exists())


class CausalColumnReuse(unittest.TestCase):
    def data(self, direction):
        events = [event(i, line=line, w0=80+i*3, w1=90-i*2, valid=i != 3)
                  for i, line in enumerate((-2, -2, 1, 1, 2, 2))]
        labels = {'a': {'eligible': True, 'final': [3, 1], 'year': 2024}}
        return events, build_direction(events, labels, 100, direction, {**DEFAULT, 'profile': 'standard'})

    def test_fixed_and_mixed_sides_use_only_needed_kernels_and_keep_identical_masks(self):
        atom = {'feature': 'path1_keep', 'op': 'eq', 'value': 1, 'sequence': 'subsequence'}
        for direction, calls in (('LIVE_HOME', 1), ('LIVE_AWAY', 1), ('LIVE_GIVE', 2)):
            with self.subTest(direction=direction):
                events, base = self.data(direction)
                with StandardColumns(base, events, direction) as columns:
                    r = columns.raw
                    expected = np.zeros(len(base['eid']), np.bool_)
                    for side in (0, 1):
                        full = path_predicate(r['mid'], r['valid'], r['line'], r['w'+str(side)], r['ts'], np.array([1]), False, False, True, False, 1, 1, -1)
                        chosen = base['side'] == side
                        expected[chosen] = full[columns.positions[chosen]]
                    with patch('lab.standard_columns.path_predicate', wraps=path_predicate) as kernel:
                        np.testing.assert_array_equal(columns.atom_mask(atom), expected)
                        self.assertEqual(kernel.call_count, calls)

    def test_window_three_columns_share_one_call_without_changing_values(self):
        events, base = self.data('LIVE_GIVE')
        with StandardColumns(base, events, 'LIVE_GIVE') as columns:
            r = columns.raw
            reference = bound_window_columns(r['mid'], r['valid'], r['minute'], r['line'], r['w0'], r['w1'], 0, 5)
            with patch('lab.standard_columns.bound_window_columns', wraps=bound_window_columns) as kernel:
                for kind, at in (('line_init', 0), ('water_init', 1 + base['side']), ('otherwater_init', 2 - base['side'])):
                    np.testing.assert_array_equal(columns.column('window_0_5_' + kind), reference[columns.positions, at])
                self.assertEqual(kernel.call_count, 1)

    def test_window_tiny_cache_and_partial_evictions_never_lose_requested_column(self):
        events, base = self.data('LIVE_GIVE')
        with StandardColumns(base, events, 'LIVE_GIVE', cache_mb=0) as columns:
            r = columns.raw
            reference = bound_window_columns(r['mid'], r['valid'], r['minute'], r['line'], r['w0'], r['w1'], 0, 5)
            for kind, at in [('line_init', 0), ('water_init', 1 + base['side']), ('otherwater_init', 2 - base['side'])] * 3:
                np.testing.assert_array_equal(columns.column('window_0_5_' + kind), reference[columns.positions, at])
                self.assertEqual(columns.resident, sum(value.nbytes for value in columns.cache.values()))


if __name__ == '__main__':
    unittest.main()
