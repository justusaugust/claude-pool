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
        self.assertEqual(registry.load(self.path), {'version': 1, 'generation': 0, 'updatedAt': 0, 'devices': [], 'accounts': [], 'leitstand': None})

    def test_update_bumps_generation_and_writes_0600(self):
        value = registry.update(lambda v: v['devices'].append(MAC), self.path)
        self.assertEqual(value['generation'], 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(registry.load(self.path), value)

    def test_merge_keeps_higher_generation(self):
        self.assertEqual(registry.merge(gen(3), gen(4))['generation'], 4)
        self.assertEqual(registry.merge(gen(4), gen(3))['generation'], 4)
        local = gen(4, devices=[MAC])
        self.assertEqual(registry.merge(local, gen(4, devices=[VPS])), local)

    def test_replace_ignores_older_or_equal_generations(self):
        current = registry.update(lambda v: v['devices'].append(MAC), self.path)
        self.assertFalse(registry.replace(gen(1), self.path))
        self.assertFalse(registry.replace(gen(0), self.path))
        self.assertEqual(registry.load(self.path), current)
        self.assertTrue(registry.replace(gen(5, devices=[VPS]), self.path))
        self.assertEqual(registry.load(self.path)['generation'], 5)

    def test_replace_rejects_invalid_values(self):
        with self.assertRaises(ValueError): registry.replace({**gen(5), 'extra': 1}, self.path)

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

    def test_exactly_one_usage_owner_and_leitstand_on_a_known_device(self):
        with self.assertRaises(ValueError): registry.update(lambda v: v['devices'].extend([MAC, {**VPS, 'roles': ['usage']}]), self.path)
        leit = {'device': 'mac', 'email': 'a@example.com', 'organization': 'AI Værk Team', 'cwd': '/work'}
        with self.assertRaises(ValueError): registry.update(lambda v: v.update(devices=[MAC], leitstand={**leit, 'device': 'ghost'}), self.path)
        with self.assertRaises(ValueError): registry.update(lambda v: v['devices'].append({**VPS, 'roles': ['leitstand']}), self.path)
        with self.assertRaises(ValueError): registry.update(lambda v: v['devices'].extend([MAC, {**VPS, 'name': 'mac'}]), self.path)
        value = registry.update(lambda v: v.update(devices=[MAC], leitstand=leit), self.path)
        self.assertEqual(value['leitstand'], leit)

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


if __name__ == '__main__': unittest.main()
