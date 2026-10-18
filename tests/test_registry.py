"""The device registry: validated, generation-numbered, secret-free, safe under concurrent writers."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import registry
from fakes import fixture_token

MAC = {'name': 'mac', 'ssh': 'justus@mac', 'platform': 'darwin', 'roles': ['usage'], 'tailscaleIp': '100.64.0.1', 'cua': True, 'herdrSession': None, 'addedAt': 1}
VPS = {'name': 'personal-vps', 'ssh': 'justus@vps', 'platform': 'linux', 'roles': [], 'tailscaleIp': None, 'cua': False, 'herdrSession': 'main', 'addedAt': 2}
ENTRY = {'name': 'team', 'label': 'Team', 'alias': 'team', 'plan': 'max', 'email': 'a@example.com', 'organization': 'AI Værk Team',
         'userId': 'user-a', 'organizationId': 'org-a', 'fingerprint': '0123456789ab', 'expiresAt': 4_000_000_000_000}


def gen(n, **extra):
    return {**registry.EMPTY(), 'generation': n, **extra}


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / 'cfg'; self.dir.mkdir(mode=0o700)
        self.path = self.dir / 'devices.json'

    def test_empty_registry_defaults(self):
        self.assertEqual(registry.load(self.path), {'version': 1, 'generation': 0, 'updatedAt': 0, 'devices': [], 'accounts': [], 'conductor': None})

    def test_update_bumps_generation_and_writes_0600(self):
        value = registry.update(lambda v: v['devices'].append(MAC), self.path)
        self.assertEqual(value['generation'], 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(registry.load(self.path), value)

    def test_merge_keeps_higher_generation(self):
        self.assertEqual(registry.merge(gen(3), gen(4))['generation'], 4)
        self.assertEqual(registry.merge(gen(4), gen(3))['generation'], 4)

    def test_a_row_added_while_a_peer_moved_ahead_survives_the_merge(self):
        base = registry.update(lambda v: v['devices'].append(MAC), self.path)
        mine = registry.update(lambda v: v['accounts'].append(ENTRY), self.path)
        theirs = {**copy.deepcopy(base), 'generation': 3}  # two unrelated changes ahead, never saw team, no tombstone
        for merged in (registry.merge(mine, theirs), registry.merge(theirs, mine)):
            self.assertEqual([r['name'] for r in merged['accounts']], ['team'])
            self.assertEqual(merged['generation'], 3)

    def test_removal_writes_a_tombstone_with_the_fingerprint_that_wins_over_the_older_row(self):
        old = registry.update(lambda v: (v['devices'].append(MAC), v['accounts'].append(ENTRY)), self.path)
        removed = registry.update(lambda v: v.update(accounts=[]), self.path)
        self.assertEqual(removed['removed'], [{'kind': 'account', 'name': 'team', 'generation': 2, 'fingerprint': '0123456789ab'}])
        self.assertEqual(registry.merge(old, removed)['accounts'], [])
        self.assertEqual(registry.merge(removed, old)['accounts'], [])
        self.assertEqual(registry.merge(old, removed), registry.merge(removed, old))

    def test_a_row_added_again_after_its_tombstone_wins_and_drops_it(self):
        registry.update(lambda v: (v['devices'].append(MAC), v['accounts'].append(ENTRY)), self.path)
        removed = registry.update(lambda v: v.update(accounts=[]), self.path)
        again = registry.update(lambda v: v['accounts'].append({**ENTRY, 'fingerprint': 'ba9876543210'}), self.path)
        self.assertNotIn('removed', again)
        self.assertEqual(registry.merge(removed, again)['accounts'][0]['fingerprint'], 'ba9876543210')

    def test_concurrent_changes_to_different_rows_both_survive(self):
        base = registry.update(lambda v: (v['devices'].extend([MAC, VPS]), v['accounts'].append(ENTRY)), self.path)
        a = copy.deepcopy(base); b = copy.deepcopy(base)
        other = self.dir / 'other.json'
        registry.write(b, other)
        a = registry.update(lambda v: registry.account(v, 'team').update(reserveAt=85), self.path)
        b = registry.update(lambda v: registry.device(v, 'personal-vps').update(cua=True), other)
        merged = registry.merge(a, b)
        self.assertEqual(registry.account(merged, 'team')['reserveAt'], 85)
        self.assertTrue(registry.device(merged, 'personal-vps')['cua'])
        self.assertEqual(merged, registry.merge(b, a))
        self.assertEqual(registry.merge(merged, a), merged); self.assertEqual(registry.merge(merged, merged), merged)

    def test_concurrent_usage_moves_keep_exactly_one_owner(self):
        base = registry.update(lambda v: v['devices'].extend([MAC, VPS, {**VPS, 'name': 'third', 'ssh': 'third'}]), self.path)
        other = self.dir / 'other.json'; registry.write(copy.deepcopy(base), other)
        def move(name):
            def change(v):
                for row in v['devices']: row['roles'] = ['usage'] if row['name'] == name else []
            return change
        a = registry.update(move('personal-vps'), self.path)
        b = registry.update(move('third'), other)
        merged = registry.merge(a, b)
        self.assertEqual(merged, registry.merge(b, a))
        self.assertEqual(registry.owner(merged)['name'], registry.owner(registry.merge(b, a))['name'])
        registry.write(merged, other)  # the next local change on that device writes the one owner
        after = registry.update(lambda v: v['accounts'].append(ENTRY), other)
        self.assertEqual([r['name'] for r in after['devices'] if 'usage' in r.get('roles', [])], [registry.owner(merged)['name']])

    def test_receive_merges_under_the_lock_and_writes_only_a_change(self):
        current = registry.update(lambda v: v['devices'].append(MAC), self.path)
        before = self.path.read_bytes()
        self.assertEqual(registry.receive(gen(0), self.path), (current, current))
        self.assertEqual(self.path.read_bytes(), before)
        previous, merged = registry.receive(gen(5, devices=[VPS]), self.path)
        self.assertEqual(previous, current)
        self.assertEqual([r['name'] for r in merged['devices']], ['mac', 'personal-vps'])
        self.assertEqual(registry.load(self.path), merged)

    def test_receive_rejects_invalid_values(self):
        with self.assertRaises(ValueError): registry.receive({**gen(5), 'extra': 1}, self.path)

    def test_force_puts_this_list_above_every_peer_and_tombstones_rows_only_peers_have(self):
        registry.update(lambda v: (v['devices'].append(MAC), v['accounts'].append(ENTRY)), self.path)
        peer = gen(9, devices=[{**MAC, 'addedAt': 5, 'generation': 9}, {**VPS, 'generation': 9}],
                   accounts=[{**ENTRY, 'name': 'ops', 'userId': 'u-o', 'fingerprint': 'aaaaaaaaaaaa', 'generation': 9}])
        forced = registry.force([peer], self.path)
        self.assertEqual(forced['generation'], 10)
        merged = registry.merge(peer, forced)
        self.assertEqual([r['name'] for r in merged['devices']], ['mac']); self.assertEqual(registry.device(merged, 'mac')['addedAt'], 1)
        self.assertEqual([r['name'] for r in merged['accounts']], ['team'])
        self.assertIn({'kind': 'account', 'name': 'ops', 'generation': 10, 'fingerprint': 'aaaaaaaaaaaa'}, merged['removed'])

    def test_equal_generation_with_different_content_is_reported_as_diverged(self):

        a, b = registry.diverged(gen(5, devices=[MAC]), gen(5, devices=[VPS]))
        self.assertRegex(a, r'^[0-9a-f]{12}$'); self.assertNotEqual(a, b)
        self.assertIsNone(registry.diverged(gen(5), gen(5)))
        self.assertIsNone(registry.diverged(gen(5, devices=[MAC]), gen(6, devices=[VPS])))

    def test_rejects_token_like_values_and_unknown_fields(self):
        with self.assertRaises(ValueError): registry.update(lambda v: v['accounts'].append({**ENTRY, 'label': fixture_token()}), self.path)
        with self.assertRaises(ValueError): registry.update(lambda v: v['accounts'].append({**ENTRY, 'token': 'x'}), self.path)
        with self.assertRaises(ValueError): registry.update(lambda v: v.update(extra=1), self.path)
        self.assertFalse(self.path.exists())

    def test_load_rejects_token_like_file_and_oversize_file(self):
        self.path.write_text(json.dumps({**gen(1), 'accounts': [{**ENTRY, 'email': fixture_token()}]})); self.path.chmod(0o600)
        with self.assertRaises(ValueError): registry.load(self.path)
        self.path.write_text(' ' * (256 * 1024 + 1)); self.path.chmod(0o600)
        with self.assertRaises(ValueError): registry.load(self.path)

    def test_exactly_one_usage_owner_and_conductor_on_a_known_device(self):
        with self.assertRaises(ValueError): registry.update(lambda v: v['devices'].extend([MAC, {**VPS, 'roles': ['usage']}]), self.path)
        entry = {'device': 'mac', 'email': 'a@example.com', 'organization': 'AI Værk Team', 'cwd': '/work'}
        with self.assertRaises(ValueError): registry.update(lambda v: v.update(devices=[MAC], conductor={**entry, 'device': 'ghost'}), self.path)
        with self.assertRaises(ValueError): registry.update(lambda v: v['devices'].append({**VPS, 'roles': ['conductor']}), self.path)
        with self.assertRaises(ValueError): registry.update(lambda v: v['devices'].extend([MAC, {**VPS, 'name': 'mac'}]), self.path)
        value = registry.update(lambda v: v.update(devices=[MAC], conductor=entry), self.path)
        self.assertEqual(value['conductor'], entry)

    def test_an_old_leitstand_entry_is_read_as_the_conductor_and_written_back_renamed(self):
        entry = {'device': 'mac', 'email': 'a@example.com', 'organization': 'AI Værk Team', 'cwd': '/work'}
        old = {**registry.EMPTY(), 'generation': 3, 'devices': [MAC], 'leitstand': entry}
        del old['conductor']
        self.path.write_text(json.dumps(old)); self.path.chmod(0o600)
        self.assertEqual(registry.load(self.path)['conductor'], entry)
        self.assertNotIn('leitstand', registry.load(self.path))
        registry.update(lambda v: None, self.path)
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored['conductor'], entry); self.assertNotIn('leitstand', stored)
        self.assertEqual(registry.validate({**old, 'generation': 4})['conductor'], entry)  # an older peer's registry still arrives

    def test_local_device_without_ssh_is_valid(self):
        self.assertIsNone(registry.update(lambda v: v['devices'].append({**MAC, 'ssh': None}), self.path)['devices'][0]['ssh'])

    def test_device_name_pattern(self):
        for bad in ['Mac', 'a b', '', 'x' * 41, '../x']:
            with self.assertRaises(ValueError): registry.update(lambda v: v['devices'].append({**MAC, 'name': bad}), self.path)

    def test_reserve_at_must_be_0_to_99(self):
        for bad in [100, -1, 'x', True, 1.5]:
            with self.assertRaises(ValueError): registry.update(lambda v: v['accounts'].append({**ENTRY, 'reserveAt': bad}), self.path)
        for good in [0, 1, 85, 99]:
            self.assertEqual(registry.update(lambda v: v.update(accounts=[{**ENTRY, 'reserveAt': good}]), self.path)['accounts'][0]['reserveAt'], good)

    def test_concurrent_updates_serialize(self):
        errors = []
        def add(i):
            try: registry.update(lambda v: v['devices'].append({**VPS, 'name': 'd%d' % i}), self.path)
            except Exception as error: errors.append(error)
        threads = [threading.Thread(target=add, args=(i,)) for i in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])
        value = registry.load(self.path)
        self.assertEqual(value['generation'], 20)
        self.assertEqual(sorted(d['name'] for d in value['devices']), sorted('d%d' % i for i in range(20)))

    def test_lookups(self):
        value = {**gen(1, devices=[MAC, VPS], accounts=[ENTRY])}
        self.assertEqual(registry.owner(value)['name'], 'mac')
        self.assertIsNone(registry.owner(gen(1, devices=[VPS])))
        self.assertEqual(registry.device(value, 'personal-vps'), VPS)
        self.assertIsNone(registry.device(value, 'nope'))
        self.assertEqual(registry.account(value, 'team'), ENTRY)
        self.assertIsNone(registry.account(value, 'nope'))

    def test_self_name_roundtrip(self):
        path = self.dir / 'device.json'
        self.assertIsNone(registry.self_name(path))
        registry.set_self('mac', path)
        self.assertEqual(registry.self_name(path), 'mac')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError): registry.set_self('Bad Name', path)

    def test_update_does_not_mutate_on_failure(self):
        registry.update(lambda v: v['devices'].append(MAC), self.path)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError): registry.update(lambda v: v['devices'].append({**MAC}), self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_load_reports_a_registry_specific_error_for_corrupt_or_loose_files(self):
        self.path.write_text('{not json'); self.path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'Registry file is unreadable'): registry.load(self.path)
        self.path.write_text(json.dumps(registry.EMPTY())); self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'Registry file is unreadable'): registry.load(self.path)

    def test_self_name_is_none_for_an_unreadable_or_corrupt_file(self):
        path = self.dir / 'device.json'
        path.write_text('{oops'); path.chmod(0o600)
        self.assertIsNone(registry.self_name(path))
        path.write_text('{"name": "mac"}'); path.chmod(0o644)
        self.assertIsNone(registry.self_name(path))

    def test_load_wraps_a_stat_failure_as_the_registry_error(self):
        from unittest.mock import patch
        with patch.object(Path, 'stat', side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(ValueError, 'Registry file is unreadable'): registry.load(self.path)


if __name__ == '__main__': unittest.main()


class MergeAlgebraTests(unittest.TestCase):
    """registry.merge is a join: commutative, idempotent and associative over the whole registry, Conductor entry included,
    so every order of syncs between any number of devices ends in the same registry."""
    NAMES = ['mac', 'vps', 'box', 'lap']

    def random_registry(self, rng):
        devices = []
        for name in rng.sample(self.NAMES, rng.randint(0, 4)):
            devices.append({'name': name, 'ssh': name, 'platform': rng.choice(['darwin', 'linux']), 'roles': [],
                            'addedAt': rng.randint(1, 3), 'generation': rng.randint(0, 4), 'cua': rng.random() < .3})
        if devices and rng.random() < .6: rng.choice(devices)['roles'] = ['usage']
        accounts = [{'name': name, 'label': name.title(), 'fingerprint': rng.choice(['aaaaaaaaaaaa', 'bbbbbbbbbbbb']),
                     'reserveAt': rng.choice([0, 85]), 'generation': rng.randint(0, 4)}
                    for name in rng.sample(['team', 'ops', 'mail'], rng.randint(0, 3))]
        removed = [{'kind': kind, 'name': name, 'generation': rng.randint(0, 5), **({'fingerprint': 'aaaaaaaaaaaa'} if kind == 'account' else {})}
                   for kind, name in rng.sample([('device', n) for n in self.NAMES] + [('account', n) for n in ['team', 'ops', 'mail']],
                                                rng.randint(0, 4))]
        value = {'version': 1, 'generation': rng.randint(0, 6), 'updatedAt': rng.randint(0, 3), 'devices': devices,
                 'accounts': accounts, 'conductor': None}
        if removed: value['removed'] = removed
        if devices and rng.random() < .7:
            value['conductor'] = {'device': rng.choice(devices)['name'], 'email': rng.choice(['a@x.de', 'b@x.de']),
                                  'organization': 'Team', 'cwd': '~/projects/conductor'}
            value['conductorGeneration'] = rng.randint(0, 3)
        return registry.validate(value)

    def test_merge_is_associative_commutative_and_idempotent_over_random_registries(self):
        import random
        rng = random.Random(20261018)
        for i in range(3000):
            a, b, c = (self.random_registry(rng) for _ in range(3))
            with self.subTest(i=i):
                left = registry.merge(registry.merge(a, b), c)
                self.assertEqual(left, registry.merge(a, registry.merge(b, c)))
                self.assertEqual(registry.merge(a, b), registry.merge(b, a))
                self.assertEqual(registry.merge(left, left), left)

    def test_the_conductor_entry_with_the_higher_generation_wins_and_ties_go_by_device_name(self):
        def entry(device, generation):
            return {**registry.EMPTY(), 'devices': [{'name': n, 'platform': 'linux'} for n in ('mac', 'vps')],
                    'conductor': {'device': device, 'email': 'a@x.de', 'organization': 'T', 'cwd': '/c'}, 'conductorGeneration': generation}
        self.assertEqual(registry.merge(entry('mac', 3), entry('vps', 2))['conductor']['device'], 'mac')
        self.assertEqual(registry.merge(entry('mac', 2), entry('vps', 2))['conductor']['device'], 'vps')
        self.assertEqual(registry.merge(entry('vps', 2), entry('mac', 2))['conductor']['device'], 'vps')

    def test_a_conductor_entry_survives_a_merge_that_meets_its_device_tombstone_before_its_row(self):
        # The re-review counterexample shape: one side removed the device, another re-added it; the entry must not be lost
        # on the way, whatever the order.
        base = {'name': 'vps', 'platform': 'linux', 'roles': []}
        entry = {'device': 'vps', 'email': 'a@x.de', 'organization': 'T', 'cwd': '/c'}
        a = {**registry.EMPTY(), 'devices': [{**base, 'generation': 1}], 'conductor': entry, 'conductorGeneration': 1}
        b = {**registry.EMPTY(), 'removed': [{'kind': 'device', 'name': 'vps', 'generation': 2}]}
        c = {**registry.EMPTY(), 'devices': [{**base, 'generation': 3}]}
        first, second = registry.merge(registry.merge(a, b), c), registry.merge(a, registry.merge(b, c))
        self.assertEqual(first, second); self.assertEqual(first['conductor'], entry)

