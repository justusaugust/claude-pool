"""Convergence harness for the device registry and long-lived tokens: three devices on temp homes, each running the real
device_sync and registry code, joined by fake links that a seeded random schedule cuts and heals. Random account adds,
removes and reserves land on whichever device is chosen, syncs happen only over links that are up. After every step no
token may be lost unless a user removal tombstoned it; after one full sync round over healed links every registry is the
same and every device holds the token each registry row names. Offline, fakes only; no real tool is ever run."""
import contextlib
import io
import json
import os
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import accounts_cli
import device_sync
import long_lived
import registry
import settings
from remote import Completed
from fakes import fixture_token, long_lived_file

BASE = ('[providers.anthropic]\nkind = "anthropic"\nauth = "claude_oauth"\n\n'
        '[[providers.anthropic.accounts]]\nname = "personal"\n')
NAMES = ['alpha', 'beta', 'gamma', 'delta']
FIELDS = ['STORES', 'LONG_LIVED', 'RUNTIME', 'CONFIG', 'REGISTRY', 'SELF', 'HERE']
SEEDS = [1, 2, 3, 4, 5, 6]
STEPS = 40


class Pipe(io.BytesIO):
    def isatty(self): return False


class Device:
    def __init__(self, root, name):
        self.name = name
        home = root / name
        self.paths = {'STORES': home / 'credentials', 'LONG_LIVED': home / 'long-lived', 'RUNTIME': home / 'runtime',
                      'CONFIG': home / 'cfg/config.json', 'REGISTRY': home / 'cfg/devices.json', 'SELF': home / 'cfg/device.json',
                      'HERE': home / 'here'}
        for key in ['STORES', 'LONG_LIVED']: self.paths[key].mkdir(mode=0o700, parents=True)
        self.paths['CONFIG'].parent.mkdir(mode=0o700)
        self.paths['RUNTIME'].mkdir()
        (self.paths['RUNTIME'] / 'shunt.toml').write_text(BASE)
        self.paths['CONFIG'].write_text(json.dumps({'accounts': [{'name': 'personal', 'label': 'Personal'}]}))
        shunt = self.paths['HERE'] / 'bin/shunt'; shunt.parent.mkdir(parents=True)
        shunt.write_text('#!/bin/sh\nfor a in "$@"; do [ "$a" = check ] && exit 0; done\nexit 3\n'); shunt.chmod(0o700)
        registry.set_self(name, self.paths['SELF'])

    def files(self):
        """Every credential file this device holds, anywhere: served, replica, moved aside."""
        found = []
        for folder in (self.paths['STORES'], self.paths['LONG_LIVED']):
            found += [p for p in folder.rglob('*.json*') if p.is_file() and not p.name.startswith('.')]
        return found


class World:
    def __init__(self, root, seed):
        self.rng = random.Random(seed)
        self.devices = [Device(root, name) for name in ['mac', 'vps', 'box']]
        self.up = {frozenset((a.name, b.name)): True for a in self.devices for b in self.devices if a is not b}
        self.stack = []
        self.created = {}          # fingerprint -> account name
        self.user_removed = set()  # fingerprints a user removal deleted on purpose (and tombstoned)
        self.version = 0
        rows = [{'name': d.name, 'ssh': d.name, 'platform': 'linux', 'roles': [], 'addedAt': 1} for d in self.devices]
        first = self.devices[0]
        with self.on(first): registry.update(lambda value: value['devices'].extend(rows))
        data = first.paths['REGISTRY'].read_bytes()
        for other in self.devices[1:]:
            with self.on(other): registry.receive(json.loads(data))

    @contextlib.contextmanager
    def on(self, device):
        saved = {key: getattr(settings, key) for key in FIELDS}
        for key in FIELDS: setattr(settings, key, device.paths[key])
        self.stack.append(device)
        try: yield
        finally:
            self.stack.pop()
            for key, value in saved.items(): setattr(settings, key, value)

    def link(self, target):
        world = self

        class Link:
            name = target.name

            def device(self, *args, stdin=None, timeout=120):
                here = world.stack[-1]
                if not world.up[frozenset((here.name, target.name))]: return Completed(255, b'', b'')
                with world.on(target):
                    try: return Completed(0, world.verb(args, stdin), b'')
                    except Exception as error: return Completed(1, b'', type(error).__name__.encode())

        return Link()

    def verb(self, args, stdin):
        verb = args[0]
        if verb == 'status': return json.dumps(device_sync.status()).encode()
        if verb == 'registry': return json.dumps(registry.load()).encode()
        if verb == 'receive-registry': return json.dumps(device_sync.receive_registry(stdin, overview=idle)).encode()
        if verb == 'receive-account': return device_sync.receive_account(args[1], stdin).encode()
        if verb == 'send-account':
            out = Pipe(); device_sync.send_account(args[1], out=out); return out.getvalue()
        raise RuntimeError('unexpected verb ' + verb)

    # Operations ----------------------------------------------------------------------------------------------------------

    def add(self, device, name):
        with self.on(device):
            if registry.account(registry.load(), name) is not None: return
            self.version += 1
            token = fixture_token('%s%d' % (name, self.version))
            source = long_lived_file(device.paths['HERE'] / 'minted', name, user='user-' + name, org='org-' + name, token=token)
            blob = source.read_bytes(); source.unlink()
            fp = long_lived.fingerprint(json.loads(blob))
            self.created[fp] = name
            row = {'name': name, 'label': name.title(), 'alias': name, 'userId': 'user-' + name, 'organizationId': 'org-' + name,
                   'fingerprint': fp}
            registry.update(lambda value: (value.update(accounts=[r for r in value['accounts'] if r['name'] != name]),
                                           value['accounts'].append(row)))
            # The enrolling device holds the token as replica and serves it, exactly as receive_account installs it.
            device_sync.receive_account(name, blob)

    def remove(self, device, name):
        with self.on(device):
            entry = registry.account(registry.load(), name)
            if entry is None: return
            for path in (device.paths['STORES'] / (name + '.json'), device.paths['LONG_LIVED'] / (name + '.json')):
                if accounts_cli.stored_kind(path) == 'long-lived': self.user_removed.add(device_sync.fingerprint(json.loads(path.read_bytes())))
            if entry.get('fingerprint'): self.user_removed.add(entry['fingerprint'])
            with contextlib.redirect_stdout(io.StringIO()):
                accounts_cli.remove_account(name, False, config=settings.CONFIG, stores=settings.STORES, binary=settings.HERE / 'bin/shunt',
                                            runtime=settings.RUNTIME, backend_source=None, backend_env=None, overview=idle,
                                            directory=settings.LONG_LIVED, registry_path=settings.REGISTRY, native_identity=None)

    def reserve(self, device, name, pct):
        with self.on(device):
            if registry.account(registry.load(), name) is None: return
            registry.update(lambda value: registry.account(value, name).update(reserveAt=pct))

    def sync(self, a, b):
        with self.on(a): return device_sync.sync_one(self.link(b))

    def step(self):
        roll = self.rng.random()
        device = self.rng.choice(self.devices)
        name = self.rng.choice(NAMES)
        if roll < 0.25: self.add(device, name); return 'add %s %s' % (device.name, name)
        if roll < 0.35: self.remove(device, name); return 'remove %s %s' % (device.name, name)
        if roll < 0.45: self.reserve(device, name, self.rng.choice([0, 70, 85])); return 'reserve %s %s' % (device.name, name)
        if roll < 0.6:
            key = self.rng.choice(sorted(self.up, key=sorted))
            self.up[key] = not self.up[key]
            return 'link %s %s' % ('-'.join(sorted(key)), 'up' if self.up[key] else 'down')
        other = self.rng.choice([d for d in self.devices if d is not device])
        return 'sync %s %s: %s' % (device.name, other.name, self.sync(device, other))

    # Invariants ----------------------------------------------------------------------------------------------------------

    def present(self):
        found = set()
        for device in self.devices:
            for path in device.files():
                try: found.add(device_sync.fingerprint(json.loads(path.read_bytes())))
                except (ValueError, RuntimeError, KeyError): pass
        return found

    def lost(self):
        return {fp: name for fp, name in self.created.items() if fp not in self.present() and fp not in self.user_removed}

    def registries(self):
        values = []
        for device in self.devices:
            with self.on(device): values.append(registry.load())
        return values


def idle(): return {'sessions': []}


class ConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name) / 'fake-bin'; self.bin.mkdir()
        for name in ['launchctl', 'systemctl', 'ssh', 'claude', 'claude-native', 'herdr']:
            (self.bin / name).write_text('#!/bin/sh\nexit 97\n'); (self.bin / name).chmod(0o755)
        patches = [patch.dict(os.environ, {'PATH': str(self.bin) + ':/usr/bin:/bin'}),
                   patch.object(device_sync, 'STATUS_PARTS', []), patch.object(device_sync, 'INTEGRATION_HOOKS', []),
                   patch.object(device_sync, 'default_native_identity', lambda: None),
                   patch.object(device_sync, 'service_state', lambda: 'running'),
                   patch.object(device_sync, 'local_overview', lambda overview: idle)]
        patches += [patch.object(settings, key, getattr(settings, key)) for key in FIELDS]
        for item in patches: item.start(); self.addCleanup(item.stop)

    def run_seed(self, seed):
        world = World(Path(self.tmp.name) / ('seed-%d' % seed), seed)
        log = []
        for _ in range(STEPS):
            log.append(world.step())
            self.assertEqual(world.lost(), {}, 'seed %d lost a token after: %s' % (seed, '; '.join(log[-6:])))
        for key in world.up: world.up[key] = True
        for a in world.devices:
            for b in world.devices:
                if a is not b: log.append('final sync %s %s: %s' % (a.name, b.name, world.sync(a, b)))
        values = world.registries()
        trail = 'seed %d: %s' % (seed, '; '.join(log))
        self.assertEqual([registry.digest(v) for v in values], [registry.digest(values[0])] * len(values), trail)
        self.assertEqual(world.lost(), {}, trail)
        for device in world.devices:
            for row in values[0]['accounts']:
                replica = device.paths['LONG_LIVED'] / (row['name'] + '.json')
                self.assertEqual(device_sync.fingerprint(json.loads(replica.read_bytes())), row['fingerprint'],
                                 device.name + ' ' + row['name'] + ' ' + trail)
        return values[0]

    def test_random_changes_under_partitions_lose_no_token_and_converge(self):
        seen = set()
        for seed in SEEDS:
            with self.subTest(seed=seed):
                final = self.run_seed(seed)
                seen |= {row['name'] for row in final['accounts']} | {row['name'] for row in final.get('removed', [])}
        self.assertTrue(seen, 'the schedule never added an account')

    def test_the_review_scenario_converges_with_the_partitioned_add_kept(self):
        world = World(Path(self.tmp.name) / 'review', 0)
        mac, vps, box = world.devices
        world.up[frozenset(('mac', 'vps'))] = False; world.up[frozenset(('mac', 'box'))] = False
        world.add(mac, 'alpha')                        # only copy, while mac is cut off
        world.reserve(vps, 'beta', 0); world.add(vps, 'beta'); world.reserve(vps, 'beta', 85)  # vps moves two generations ahead
        for key in world.up: world.up[key] = True
        self.assertEqual(world.sync(mac, vps), 'ok')
        self.assertEqual(world.lost(), {})
        names = [[row['name'] for row in value['accounts']] for value in world.registries()[:2]]
        self.assertEqual(names, [['alpha', 'beta'], ['alpha', 'beta']])


if __name__ == '__main__':
    unittest.main()
