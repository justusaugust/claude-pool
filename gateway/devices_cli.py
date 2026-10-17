"""`claude-pool device ...`: the user verbs that add, check and remove machines, and the internal verbs another device calls
over SSH. Internal verbs take no secret in argv: tokens arrive on stdin and leave on a stdout pipe, never a terminal."""
import argparse
import contextlib
import fcntl
import json
import os
import sys
import time

import accounts_cli
# Every module that contributes a STATUS_PARTS or INTEGRATION_HOOKS entry, imported here so registration never depends on call order.
import conductor
import device_cua
import device_install
import device_integrations  # noqa: F401  (registers herdrProfiles and its hook)
import device_preview
from device_report import ready, render
import device_sync
import native_login  # noqa: F401  (registers nativeLogin)
import readings
import registry
from private_files import private_directory
from remote import Local, Remote
import settings

USER_VERBS = ['add', 'check', 'remove', 'list', 'sync', 'switch', 'conductor move', 'conductor status']
INTERNAL_VERBS = ['status', 'registry', 'receive-registry', 'receive-account', 'send-account', 'identity', 'readings', 'shell',
                  'cua-token', 'conductor serve', 'conductor install', 'conductor uninstall', 'forget-accounts', 'self', 'preview-rules']
DEVICE_HELP = '\n'.join(['claude-pool device <verb>', *('  device ' + verb for verb in USER_VERBS), '',
                         'Internal verbs (called over SSH): ' + ', '.join(INTERNAL_VERBS)])
POOL_TIMEOUT = 600  # `--stop` waits up to 40 s for the bridge to drain; `--start` waits for the backend


class Usage(Exception): pass


class Parser(argparse.ArgumentParser):
    def error(self, message): raise Usage(message)


def parse(verb, argv, *arguments):
    parser = Parser(prog='claude-pool device ' + verb, add_help=False)
    for names, options in arguments: parser.add_argument(*names, **options)
    return parser.parse_args(argv)


def stdin_bytes(limit):
    data = sys.stdin.buffer.read(limit + 1)
    if len(data) > limit: raise ValueError('Input is larger than ' + str(limit // 1024) + ' KiB')
    return data


def binary(out):
    stream = getattr(out, 'buffer', None)
    if stream is None: raise RuntimeError('Refusing to write a credential to a text stream')
    out.flush()
    return stream


def print_json(value): print(json.dumps(value, indent=2, sort_keys=True))


# Internal verbs ------------------------------------------------------------------------------------------------------------

def status(argv, out, **_):
    parse('status', argv, (['--json'], {'action': 'store_true'}))
    print_json(device_sync.status())  # always JSON: peers parse it with and without --json
    return 0


def show_registry(argv, out, **_):
    parse('registry', argv)
    print_json(registry.load())
    return 0


def receive_registry(argv, out, **_):
    parse('receive-registry', argv)
    print_json(device_sync.receive_registry(stdin_bytes(registry.LIMIT)))
    return 0


def receive_account(argv, out, **_):
    name = device_sync.check_name(parse('receive-account', argv, (['name'], {})).name)
    print(device_sync.receive_account(name, stdin_bytes(device_sync.LIMIT)))
    return 0


def send_account(argv, out, **_):
    name = device_sync.check_name(parse('send-account', argv, (['name'], {})).name)
    device_sync.send_account(name, out=binary(out))
    return 0


def identity(argv, out, **_):
    args = parse('identity', argv, (['user_id'], {}), (['org_id'], {}))
    value = {'shuntAccountUuid': args.user_id, 'shuntOrganizationUuid': args.org_id}
    # Only this device's own refreshable grant answers; never ask another device from here.
    try: info = accounts_cli.long_lived_identity(value, settings.STORES, lambda *_: None)
    except RuntimeError as error:
        print('No identity: ' + ('no refreshable login for this account here' if str(error) == accounts_cli.OWNER_NEEDED else str(error)),
              file=sys.stderr)
        return 1
    print_json({key: info[key] for key in accounts_cli.IDENTITY_KEYS})
    return 0


def export_readings(argv, out, **_):
    parse('readings', argv)
    if not device_sync.is_owner(registry.load()):
        print('This device is not the usage owner; only the usage owner shares readings.', file=sys.stderr)
        return 1
    print(json.dumps(readings.export(device_sync.pool_overview(), registry.self_name(), int(time.time() * 1000))))
    return 0


def shell(argv, out, **_):
    action = parse('shell', argv, (['action'], {'choices': ['check', 'add', 'remove']})).action
    rc = device_install.shell_rc()
    if action == 'check': return 0 if device_install.shell_check(rc) else 1
    changed = (device_install.shell_add if action == 'add' else device_install.shell_remove)(rc)
    print(('Changed ' if changed else 'Unchanged ') + str(rc))
    return 0


def cua_token(argv, out, **_):
    parse('cua-token', argv)
    device_cua.send_token(binary(out))
    return 0


def preview_rules(argv, out, **_):
    return device_preview.rules_verb(parse('preview-rules', argv, (['action'], {'choices': ['add', 'remove', 'check']})).action)


def _unlink_long_lived(path):
    """Unlink `path` only while it is the same long-lived file that was checked; anything else stays."""
    try: before = os.lstat(path)
    except FileNotFoundError: return False
    if path.is_symlink() or accounts_cli.stored_kind(path) != 'long-lived': return False
    if not accounts_cli.same_file(os.lstat(path), before): raise RuntimeError(path.name + ' changed while it was being removed')
    path.unlink()
    return True


def forget_accounts(argv, out, **_):
    """Run on a device that is being removed: drop every long-lived copy and its membership. A refreshable grant is unique
    to its machine and stays byte-identical, with its membership."""
    parse('forget-accounts', argv)
    names = set(device_sync.config_names())
    for folder in (settings.STORES, settings.LONG_LIVED):
        names |= {p.stem for p in folder.glob('*.json') if not p.stem.startswith('.')}
    report = {'removed': [], 'kept': [], 'failed': {}}
    for name in sorted(names):
        if not device_sync.NAME.fullmatch(name): continue
        store, copy = settings.STORES / (name + '.json'), settings.LONG_LIVED / (name + '.json')
        try:
            private_directory(settings.STORES)
            with (settings.STORES / ('.' + name + '.add.lock')).open('a') as lock:  # never race an `account add` of this name
                try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise RuntimeError('an account add of ' + name + ' is running; rerun later') from None
                done = False
                if not store.is_symlink() and accounts_cli.stored_kind(store) == 'long-lived':
                    if name in device_sync.config_names():
                        paths, env = accounts_cli.local_paths_env(settings.RUNTIME, settings.STORES)
                        accounts_cli.unregister(settings.CONFIG, settings.STORES, settings.HERE / 'bin/shunt', paths, name, env)
                    done = _unlink_long_lived(store)
                done = _unlink_long_lived(copy) or done
            (report['removed'] if done else report['kept']).append(name)
        except Exception as error:
            report['failed'][name] = type(error).__name__ + ': ' + str(error)[:200]
    print_json(report)
    return 1 if report['failed'] else 0


def name_self(argv, out, **_):
    registry.set_self(parse('self', argv, (['name'], {})).name)
    return 0


def conductor_internal(action, argv):
    if action == 'serve':
        parse('conductor serve', argv)
        return conductor.serve_service()
    if action == 'install':
        args = parse('conductor install', argv, (['--session-id'], {}), (['--fresh'], {'action': 'store_true'}),
                     (['--move'], {'action': 'store_true'}))
        step = conductor.install(args.session_id, args.fresh, args.move)
    else:
        parse('conductor uninstall', argv)
        step = conductor.uninstall()
    print(render(registry.self_name() or 'this device', [step]))
    return 0 if step.status in ('ok', 'skipped') else 1


# User verbs ----------------------------------------------------------------------------------------------------------------

def not_yet(verb):
    def run(argv, out, **_):
        print('claude-pool device ' + verb + ' is not implemented yet in this build.', file=sys.stderr)
        return 2
    return run


def list_devices(argv, out, **_):
    parse('list', argv)
    value, me = registry.load(), registry.self_name()
    if not value['devices']:
        print('No devices registered yet. Add one with: claude-pool device add <ssh-target>')
        return 0
    lead = (value.get('conductor') or {}).get('device')
    width = max(len(row['name']) for row in value['devices'])
    for row in value['devices']:
        notes = [{'darwin': 'macOS', 'linux': 'Linux'}.get(row.get('platform'), row.get('platform') or 'unknown')]
        if row['name'] == me: notes.append('this device')
        if 'usage' in row.get('roles', []): notes.append('usage owner')
        if row['name'] == lead: notes.append('Conductor')
        if row.get('cua'): notes.append('Cua')
        print(f"{row['name']:<{width}}  {row.get('ssh') or 'no ssh target':<24}  " + ', '.join(notes))
    print(str(len(value['accounts'])) + ' account' + ('' if len(value['accounts']) == 1 else 's') + ' in the registry, generation '
          + str(value['generation']))
    return 0


def sync(argv, out, remotes, **_):
    args = parse('sync', argv, (['--force-registry'], {'action': 'store_true'}))
    if registry.self_name() is None:
        print('This device has no name in the registry yet; add it with claude-pool device add, then sync.')
        return 1
    if args.force_registry: registry.update(lambda value: None)  # a newer generation makes this list win everywhere
    results = device_sync.sync_all(remotes)
    if not results: print('No other devices to sync with.')
    for name, result in sorted(results.items()): print(name + ': ' + result)
    return 0 if all(result == 'ok' for result in results.values()) else 1


def switch(argv, out, local, **_):
    args = parse('switch', argv, (['--wait'], {'action': 'store_true'}))
    def pool(flag): return local.pool(flag, timeout=POOL_TIMEOUT).returncode
    return device_sync.switch(args.wait, device_sync.local_overview(None), pool)


def supports_device(remote):
    """Never send a `device` verb to an older launcher: it would hand the words to Claude as a prompt."""
    return remote.call(['sh', '-c', device_install.LAUNCHER_SCRIPT]).returncode == 0


def peer_status(remote):
    if not supports_device(remote): return None
    done = remote.pool('device', 'status', '--json')
    if done.returncode: return None
    try: value = json.loads(done.stdout)
    except ValueError: return None
    return value if isinstance(value, dict) else None


def conductor_user(action, argv, remotes, local):
    value = registry.load()
    if action == 'move':
        target = parse('conductor move', argv, (['device'], {})).device
        return conductor.move(target, value, remotes=remotes, local=local)
    parse('conductor status', argv)
    me, statuses = registry.self_name(), {}
    for row in value['devices']:
        if row['name'] == me: statuses[row['name']] = device_sync.status()
        elif row.get('ssh'):
            try: found = peer_status(remotes(row['ssh']))
            except ValueError: found = None
            if found is not None: statuses[row['name']] = found
    step = conductor.check(statuses, value)
    print(render('Conductor' + (' (' + value['conductor']['device'] + ')' if value.get('conductor') else ''), [step]))
    return 0 if ready([step]) else 2


def conductor_verb(argv, out, remotes, local, **_):
    action, rest = (argv[0], argv[1:]) if argv else (None, [])
    if action in ('serve', 'install', 'uninstall'): return conductor_internal(action, rest)
    if action in ('move', 'status'): return conductor_user(action, rest, remotes, local)
    raise Usage('claude-pool device conductor move <device> | status')


VERBS = {'add': not_yet('add'), 'check': not_yet('check'), 'remove': not_yet('remove'), 'list': list_devices, 'sync': sync,
         'switch': switch, 'conductor': conductor_verb, 'status': status, 'registry': show_registry,
         'receive-registry': receive_registry, 'receive-account': receive_account, 'send-account': send_account,
         'identity': identity, 'readings': export_readings, 'shell': shell, 'cua-token': cua_token,
         'forget-accounts': forget_accounts, 'self': name_self, 'preview-rules': preview_rules}


def cli(argv, *, remotes=Remote, local=Local(), out=sys.stdout):
    argv = list(argv)
    with contextlib.redirect_stdout(out):
        if argv[:1] in (['help'], ['--help'], ['-h']) or not argv:
            print(DEVICE_HELP)
            return 0 if argv else 2
        run = VERBS.get(argv[0])
        if run is None:
            print('Unknown device verb: ' + argv[0] + '. See claude-pool help device.', file=sys.stderr)
            return 2
        try: return run(argv[1:], out=out, remotes=remotes, local=local)
        except Usage as error:
            print(str(error) + '. See claude-pool help device.', file=sys.stderr)
            return 2
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        except (RuntimeError, OSError, device_sync.Failed, device_sync.Unreachable) as error:
            print('claude-pool device ' + argv[0] + ': ' + str(error), file=sys.stderr)
            return 1


if __name__ == '__main__':
    raise SystemExit(cli(sys.argv[1:]))
