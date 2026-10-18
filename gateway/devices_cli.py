"""`claude-pool device ...`: the user verbs that add, check and remove machines, and the internal verbs another device calls
over SSH. Internal verbs take no secret in argv: tokens arrive on stdin and leave on a stdout pipe, never a terminal."""
import argparse
import contextlib
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import socket
import sys
import tempfile
import time
import tomllib

import accounts_cli
# Every module that contributes a STATUS_PARTS or INTEGRATION_HOOKS entry, imported here so registration never depends on call order.
import conductor
import conductor_board
import device_cua
import device_herdr  # noqa: F401  (registers herdrUnits)
import device_install
import device_integrations  # noqa: F401  (registers herdrProfiles and its hook)
import device_preview
from account_metadata import load as load_config
from device_report import ADD_ORDER, CHECK_NAMES, Step, ready, render
import device_sync
import native_login  # noqa: F401  (registers nativeLogin)
import readings
import registry
from private_files import atomic_write, private_directory
from remote import Local, Remote, redact, remote_command
import routing
import service
import settings

USER_VERBS = ['add', 'check', 'remove', 'list', 'sync', 'switch', 'conductor move', 'conductor status']
INTERNAL_VERBS = ['status', 'registry', 'receive-registry', 'receive-account', 'send-account', 'identity', 'readings', 'shell',
                  'cua-token', 'cua-tunnel', 'conductor serve', 'conductor install', 'conductor uninstall', 'forget-accounts', 'self', 'preview-rules',
                  'herdr-units', 'receive-checks']
CHECK_HELP = {
    'reachable': ('ssh to the device works without a password prompt', 'ssh <ssh-target> true must work without a password prompt (BatchMode)'),
    'native-claude': ('Native Claude Code is installed', 'curl -fsSL https://claude.ai/install.sh | bash on the device'),
    'install': ('claude-pool is installed and knows the device commands', 'claude-pool device add <ssh-target>; if it says Python 3.11 or newer is missing, install Python 3.11 or newer on the device first'),
    'version': ('the device runs the same claude-pool sources as this one (each platform builds its own backend)', 'claude-pool device add <ssh-target>'),
    'linger': ('Linux services survive logout (macOS: skipped)', 'claude-pool device add <ssh-target> tries it without root; if it stays failed, a root user runs sudo loginctl enable-linger <user> on the device'),
    'shell': ('the claude shell function sends claude through the pool', 'claude-pool device add <ssh-target>'),
    'accounts': ('registry, token fingerprints, reserve settings and account health match', 'claude-pool device sync; claude-pool device sync --force-registry on the device whose list is right; claude-pool account add <email> --long-lived --replace --name <name> on the device that first enrolled it, then device sync; claude-pool account reserve <name> --at <pct>; claude-pool device switch; ok with a name hint: claude-pool account rename <local-name> <registry-name> on that device'),
    'service': ('the pool service is running the current program (stale: launchd still runs the one from before an update)',
                'claude-pool device add <ssh-target>; or ssh <ssh-target> .local/bin/claude-pool --start; stale, when no task runs: '
                + device_install.restart_fix('<ssh-target>')),
    'doctor': ('claude-pool doctor on the device reports nothing', 'run ssh <device> .local/bin/claude-pool doctor and apply the fix it prints'),
    'native-login': ('Native Claude is logged in to an account the pool serves', 'claude-pool device add <ssh-target> and click Authorize; claude-native auth status if it cannot be read'),
    'herdr': ('this machine has a herdr profile for the device', 'herdr machine add <ssh-target> --label <name>'),
    'herdr-sessions': ('every herdr session registered for the device (add --herdr-session) comes back after a reboot (Linux devices only); '
                       'other running sessions are only named',
                       remote_command('<ssh-target>', device_herdr.VERB + ' --session <session>') + '; to hand a running session to its unit now, while its agents are idle: '
                       + remote_command('<ssh-target>', device_herdr.VERB + ' --adopt')),
    'app': ('the Mac that runs the Claude app lists and trusts the SSH environment', 'the Claude app clicks printed under Needs you'),
    'cua': ('the device shares its desktop through Cua and the machines can reach each other', 'see docs/DEVICES.md, Cua; macOS: switch on cua-spacesd in Accessibility, then claude-pool device add <ssh-target> --cua'),
    'conductor': ('exactly one Conductor runs, on the registry device, connected to Remote Control', 'not connected: turn Remote Control on in the Claude app, wait two minutes, run claude-pool device check <device>, then ssh <device> .local/bin/claude-pool device conductor install --restart; not running: ssh <device> .local/bin/claude-pool device conductor install; claude-pool device conductor move <device>; stop the hand-started Conductor (herdr session conductor)'),
    'readings': ('usage readings from the usage owner are fresh', 'ssh -o BatchMode=yes <owner> true must work from the device'),
    'preview': ('the preview tool is installed, links are HTTPS and every preview comes back after a reboot (Linux devices only)', 'claude-pool device add <ssh-target>; a root user runs sudo tailscale set --operator=<user> for HTTPS; previews that would not survive a reboot: ' + remote_command('<ssh-target>', device_preview.RESTORE)),
}
assert tuple(CHECK_HELP) == CHECK_NAMES
DEVICE_HELP = '\n'.join([
    'claude-pool device <verb>: connect several machines to one pool. An agent can add, check, repair and remove a machine alone.',
    'The full guide with the same table: docs/DEVICES.md', '',
    'BEFORE device add <ssh-target>',
    '  - Both machines are in the same tailnet; ssh from this machine to the target works with no password prompt (ssh-keygen, ssh-copy-id).',
    '  - The target has Python 3.11 or newer. add installs claude-pool there from this machine\'s checkout.',
    '  - This machine has at least one long-lived account: claude-pool account add <email> --long-lived (opens a browser; or --from-file).',
    '  - Another device to use as usage owner needs ssh from the new machine to the owner without a prompt (readings).', '',
    'VERBS',
    '  device add <ssh-target> [--name N] [--cua] [--usage-owner] [--herdr-session S] [--herdr-units listed|all]',
    '      Adds or repairs a machine; rerun to finish what is missing. N matches [a-z0-9-]{1,40}, default from the host name.',
    '      --cua also shares desktops through Cua. --usage-owner makes it the usage poller (and creates the Conductor if none exists);',
    '      it needs its own refreshable login for every account (claude-pool account add <email> --name <name> on it), else it is refused.',
    '      --herdr-session S: the herdr profile on this machine opens session S on the target (herdr machine add --remote-session S),',
    '      and S comes back after a reboot there (a claude-pool-herdr@S unit, Linux). Only registered sessions get a unit;',
    '      --herdr-units all also gives one to every session running there now.',
    '  device check [name] [--probe] [--json]   Reads every check below, changes nothing. --probe sends one tiny Claude request per long-lived account.',
    '  device remove <name> [--wait] [--uninstall] [--forget]',
    '      Stops the pool there, removes its token copies and herdr profile; never destroys the only copy of a token (run device sync first).',
    '      Exit 3: tasks are running there, use --wait. --forget drops a lost machine from the registry of every device; on it, only its previews are stopped if it answers.',
    '      The Conductor device and the usage owner cannot be removed first.',
    '  device list                              Devices, roles and the account count',
    '  device sync [--force-registry]           Merges the registry row by row and copies long-lived tokens between devices.',
    '                                           --force-registry makes this device\'s list win; other devices retire, never delete, tokens it drops.',
    '  device switch [--wait]                   Restarts the local pool when no request is in flight, to apply a pending membership change',
    '  device conductor move <device>           Moves the one Conductor to a device whose Native login is the Conductor account, linger is on and the pool runs.',
    '                                           It starts a new conversation: open "Conductor Claude" in the Claude app.',
    '  device conductor status                  Shows where the Conductor runs and whether it is connected',
    '  (on the Conductor device: claude-pool device conductor install [--session-id ID] [--fresh] [--restart] | uninstall)', '',
    'EXIT CODES: 0 nothing is missing, failed or unreachable (manual steps do not block); 2 a step needs work, read its fix line; 3 remove: tasks running.',
    'A failed linger step (root needed) also gives exit 2 until the root command ran.',
    'A device that prints "outdated launcher (no device support)" needs claude-pool device add <ssh-target> from this machine;',
    '"missing" means no launcher is installed there: the same command.', '',
    'CHECKS in the order add runs them: name, what it proves, fix',
    *('  %-14s %s. Fix: %s' % (name, proves, fix) for name, (proves, fix) in CHECK_HELP.items()), '',
    'WHAT ONLY A HUMAN CAN DO',
    '  - One Native Authorize click per new machine: add opens claude.ai in this machine\'s browser. Nothing is pasted. If the click alone cannot',
    '    work, native-login is manual and prints the commands to run on the machine.',
    '  - macOS Accessibility for cua-spacesd (System Settings, Privacy & Security, Accessibility), then run add --cua again.',
    '  - Cua is never installed by claude-pool: install it from github.com/trycua/cua and check cua --version on both machines first.',
    '  - Root commands that add only prints: sudo loginctl enable-linger <user> (Linux) and sudo tailscale set --operator=<user> (HTTPS previews).',
    '  - Claude app on the Mac (Code tab, environment menu): add the SSH environment, open it once and trust it.', '',
    'ACCOUNTS AND TOKENS',
    '  Refreshable accounts stay on the machine that logged in. Long-lived tokens are the only credentials copied, only by these commands,',
    '  through a pipe; machines are compared by token fingerprint. The usage owner polls usage; others read it every 5 minutes.',
    '  account reserve <name> --at <pct>|--off keeps an account as the last choice (the Conductor account is reserved from 85%).',
    '  A long-lived token lasts one year; doctor warns 30 days before and fails 7 days before. Revoking is manual in claude.ai settings.',
    '  Status reauthenticate means the token is revoked or expired: on the device that first enrolled the account run',
    '  claude-pool account add <email> --long-lived --replace --name <name> (needs a browser), then claude-pool device sync.',
    '  If that device is gone (--replace says no token is saved), run the same command without --replace on any machine with a browser.', '',
    'RELATED TOOLS',
    '  claude-pool conductor snapshot|events|tell|journal   what Conductor Claude uses to watch and prompt herdr agents;',
    '                                                      tell --list shows the queued tasks, tell --cancel <id> removes one',
    '  preview <port> | preview file <path>                 share a dev server or one file or folder over Tailscale (Linux devices)',
    '  device herdr-units [--adopt] [--session S] [--all]  on a Linux device: a unit per registered herdr session (or S; --all: every',
    '                                                       running one too), so it comes back after a reboot.',
    '                                                       A running session stays as it is until the next boot; --adopt hands it to its',
    '                                                       unit now, only while none of its agents is working or waiting.',
    '  Removal runs forget-accounts on the device, only after its pool is stopped. A lost machine: device remove <name> --forget.',
    '', 'Internal verbs (called over SSH): ' + ', '.join(INTERNAL_VERBS)])
from long_lived import REPLACE_GONE, REPLACE_WHERE, replace_fix
NO_SSH = 'no ssh target from this device'
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


# Status parts this module adds: what another device needs to check this one without a second verb ----------------------------

def overview_part():
    """The local pool's in-flight state and account statuses (no identities, no tokens); None when it cannot be read.
    A pool that is not running reads as idle with no accounts."""
    try: value = device_sync.local_overview(None)()
    except Exception: return {'overview': None}
    if not isinstance(value, dict): return {'overview': None}
    health = value.get('health') if isinstance(value.get('health'), dict) else {}
    sessions = [row for row in value.get('sessions') or [] if isinstance(row, dict)]
    accounts = [row for row in value.get('accounts') or [] if isinstance(row, dict) and isinstance(row.get('name'), str)]
    try: rejected = readings.reauthenticate(settings.LONG_LIVED_PROBES)  # Shunt only cools a revoked long-lived token
    except Exception: rejected = set()
    accounts = [{**row, 'status': 'reauthenticate'} if row['name'] in rejected else row for row in accounts]
    return {'overview': {'health': {'activeRequests': health.get('activeRequests', 0) if isinstance(health.get('activeRequests', 0), int) else 0},
                         'sessions': [{'account': row.get('account'), 'busy': bool(row.get('busy'))} for row in sessions],
                         'accounts': [{'name': row['name'], 'status': row.get('status')} for row in accounts]}}


def readings_part():
    """When this device last received usage readings from the usage owner (ms), or None, and which accounts they cover
    (an account the owner reads no usage for has no row, or one that was never seen)."""
    try: shared = readings.load_shared(settings.SHARED_READINGS)
    except Exception: shared = None
    covered = sorted(name for name, entry in shared['accounts'].items() if entry.get('seenAt')) if shared else None
    return {'sharedReadingsAt': shared['at'] if shared else None, 'sharedReadingsAccounts': covered}


device_sync.STATUS_PARTS.extend([overview_part, readings_part])


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


def receive_checks(argv, out, **_):
    """Another device's `device check` results for the Conductor Board, JSON on stdin (no secrets); newest per device wins."""
    parse('receive-checks', argv)
    try: value = json.loads(stdin_bytes(conductor_board.LIMIT))
    except ValueError: raise ValueError('receive-checks reads the shared check results as JSON on stdin') from None
    print(json.dumps({'merged': conductor_board.merge_checks(value)}))
    return 0


def unreachable_in(results):
    """Devices whose check just found them unreachable: sharing would only wait for the ssh timeout."""
    return {name for name, steps in results.items() if any(step.status == 'unreachable' for step in steps if step is not None)}


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
    ids = {row['name']: registry.ids(row) for row in device_sync.account_rows() if row['active'] and registry.ids(row)}
    print(json.dumps(readings.export(device_sync.pool_overview(), registry.self_name(), int(time.time() * 1000), ids=ids)))
    return 0


def shell(argv, out, **_):
    action = parse('shell', argv, (['action'], {'choices': ['check', 'add', 'remove']})).action
    rc = device_install.shell_rc()
    if action == 'check': return 0 if device_install.shell_check(rc) else 1
    changed = (device_install.shell_add if action == 'add' else device_install.shell_remove)(rc)
    print(('Changed ' if changed else 'Unchanged ') + str(rc))
    return 0


def herdr_units(argv, out, **_):
    """This device's herdr session units (device_herdr): enable them, --adopt running sessions, or --remove ours."""
    args = parse('herdr-units', argv, (['--adopt'], {'action': 'store_true'}), (['--remove'], {'action': 'store_true'}),
                 (['--session'], {'action': 'append'}), (['--all'], {'action': 'store_true'}))
    if args.remove and (args.adopt or args.session or args.all): raise Usage('herdr-units --remove takes no other option')
    for name in args.session or []:
        if not registry.HERDR_SESSION.fullmatch(name): raise Usage('--session takes letters, digits, dot, dash and underscore')
    host = device_herdr.Host()
    if args.remove: steps = [device_herdr.remove(host)]
    else: steps = device_herdr.apply(host, device_herdr.listed(), adopt=args.adopt, only=args.session, everything=args.all) or [
        Step(device_herdr.STEP, 'ok', 'no saved herdr session is registered' + (' or runs' if args.all else ''))]
    print(render('herdr sessions on ' + (registry.self_name() or 'this device'), steps))
    return 0 if all(step.status in ('ok', 'skipped') for step in steps) else 1


def cua_token(argv, out, **_):
    parse('cua-token', argv)
    device_cua.send_token(binary(out))
    return 0


def cua_tunnel(argv, out, **_):
    """This device's SSH tunnel to another device's Cua host (add prints its port and state as JSON; remove deletes ours only)."""
    args = parse('cua-tunnel', argv, (['action'], {'choices': ['add', 'remove']}), (['peer'], {}), (['ip'], {'nargs': '?'}),
                 (['ssh'], {'nargs': '?'}))
    if args.action == 'add':
        if not (args.ip and args.ssh): raise Usage('cua-tunnel add needs <device> <tailscale-ip> <ssh-target>')
        print_json(device_cua.tunnel_add(args.peer, args.ip, args.ssh))
        return 0
    if args.ip or args.ssh: raise Usage('cua-tunnel remove takes only <device>')
    device_cua.tunnel_remove(args.peer)
    print('removed')
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


def pool_stopped():
    """Deliberately stopped (`--stop` left the maintenance marker) and nothing answers the gateway, backend or control port."""
    if not (settings.RUNTIME / 'maintenance').exists(): return False
    for port in (settings.GATEWAY_PORT, settings.BACKEND_PORT, settings.CONTROL_PORT):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=1): return False
        except OSError: continue
    return True


def drop_members(names):
    """`accounts_cli.apply_change` for a stopped pool: the config and runtime TOML are parsed and validated, but the live-backend
    `shunt check` is skipped, so the member list may become empty (a device that is being removed)."""
    config = settings.CONFIG
    paths, _env = accounts_cli.local_paths_env(settings.RUNTIME, settings.STORES)
    config.parent.mkdir(parents=True, exist_ok=True)
    with (config.parent / ('.' + config.name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = load_config(config)
        value['accounts'] = [row for row in value.get('accounts', []) if row['name'] not in names]
        candidates = []
        for path in paths:
            if not path.exists(): continue
            text = path.read_text()
            for name in sorted(names): text = accounts_cli.remove_membership(text, name)
            tomllib.loads(text)
            candidates.append((path, text))
        with tempfile.NamedTemporaryFile(mode='w', dir=config.parent) as candidate:
            candidate.write(json.dumps(value)); candidate.flush(); load_config(Path(candidate.name))
        atomic_write(config, json.dumps(value, indent=2) + '\n')
        for path, text in candidates:
            if path.read_text() != text: atomic_write(path, text)


def forget_accounts(argv, out, **_):
    """Run on a device that is being removed, after its pool was stopped: drop every long-lived copy and its membership.
    A refreshable grant is unique to its machine and stays byte-identical, with its membership."""
    parse('forget-accounts', argv)
    if not pool_stopped():
        print('The pool on this device is not stopped; run claude-pool --stop first. Nothing was changed.', file=sys.stderr)
        return 1
    names = set(device_sync.config_names())
    for folder in (settings.STORES, settings.LONG_LIVED):
        names |= {p.stem for p in folder.glob('*.json') if not p.stem.startswith('.')}
    names = sorted(name for name in names if device_sync.NAME.fullmatch(name))
    report = {'removed': [], 'kept': [], 'retired': [], 'failed': {}}
    private_directory(settings.STORES)
    with contextlib.ExitStack() as held:
        created = []
        for name in names:  # never race an `account add` of these names
            path = settings.STORES / ('.' + name + '.add.lock')
            if not path.exists(): created.append(path)
            lock = held.enter_context(path.open('a'))
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('An account add of ' + name + ' is running; rerun later. Nothing was changed.', file=sys.stderr)
                for item in created: item.unlink(missing_ok=True)
                return 1
        try:
            members = set(device_sync.config_names())
            served = [n for n in names if not (settings.STORES / (n + '.json')).is_symlink()
                      and accounts_cli.stored_kind(settings.STORES / (n + '.json')) == 'long-lived']
            # A long-lived store file with no membership and no replica may be the only copy: moved aside, never deleted.
            strays = {n for n in served if n not in members and accounts_cli.stored_kind(settings.LONG_LIVED / (n + '.json')) != 'long-lived'}
            # Membership goes first, so no member list points at a file while it is unlinked.
            drop_members({n for n in served if n in members})
            for name in names:
                try:
                    if name in strays:
                        device_sync.retire_store(settings.STORES / (name + '.json'), settings.STORES / '.retired')
                        report['retired'].append(name); continue
                    done = _unlink_long_lived(settings.STORES / (name + '.json')) if name in served else False
                    done = _unlink_long_lived(settings.LONG_LIVED / (name + '.json')) or done
                    (report['removed'] if done else report['kept']).append(name)
                except Exception as error:
                    report['failed'][name] = type(error).__name__ + ': ' + str(error)[:200]
        except Exception as error:
            report['failed']['membership'] = type(error).__name__ + ': ' + str(error)[:200]
        finally:
            for item in created: item.unlink(missing_ok=True)
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
                     (['--move'], {'action': 'store_true'}), (['--restart'], {'action': 'store_true'}))
        step = conductor.install(args.session_id, args.fresh, args.move, args.restart)
    else:
        parse('conductor uninstall', argv)
        step = conductor.uninstall()
    print(render(registry.self_name() or 'this device', [step]))
    return 0 if step.status in ('ok', 'skipped') else 1


# User verbs ----------------------------------------------------------------------------------------------------------------

def list_devices(argv, out, **_):
    parse('list', argv)
    value, me = registry.load(), registry.self_name()
    if not value['devices']:
        print('No devices registered yet. Add one with: claude-pool device add <ssh-target>')
        return 0
    lead = (registry.conductor(value) or {}).get('device')
    width = max(len(row['name']) for row in value['devices'])
    for row in value['devices']:
        notes = [{'darwin': 'macOS', 'linux': 'Linux'}.get(row.get('platform'), row.get('platform') or 'unknown')]
        if row['name'] == me: notes.append('this device')
        if registry.is_owner(value, row): notes.append('usage owner')
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
    if args.force_registry:
        # Every row here is stamped above every list that can be read; rows only another device has get tombstones that
        # name their token, so a receiving device retires exactly that token and never unlinks one.
        peers, me = [], registry.self_name()
        for row in registry.load()['devices']:
            if row['name'] == me or not row.get('ssh'): continue
            try: peers.append(registry.validate(json.loads(device_sync.peer(remotes(row['ssh']), 'registry'))))
            except (ValueError, RuntimeError, OSError, device_sync.Failed, device_sync.Unreachable, device_sync.Outdated, device_sync.Missing):
                print(row['name'] + ': its list could not be read; rows only it has stay')
        registry.force(peers)
    results = device_sync.sync_all(remotes)
    # The Conductor Board on a device with no ssh back here still sees this device's check results.
    conductor_board.share_checks(remotes, skip={name for name, result in results.items() if result == 'unreachable'})
    if not results: print('No other devices to sync with.')
    for name, result in sorted(results.items()): print(name + ': ' + result)
    return 0 if all(result == 'ok' or result.startswith('skipped') for result in results.values()) else 1


def switch(argv, out, local, **_):
    args = parse('switch', argv, (['--wait'], {'action': 'store_true'}))
    def pool(flag): return local.pool(flag, timeout=POOL_TIMEOUT).returncode
    return device_sync.switch(args.wait, device_sync.local_overview(None), pool)


def conductor_user(action, argv, remotes, local):
    value = registry.load()
    if action == 'move':
        target = parse('conductor move', argv, (['device'], {})).device
        return conductor.move(target, value, remotes=remotes, local=local)
    parse('conductor status', argv)
    me, statuses, outdated = registry.self_name(), {}, {}
    for row in value['devices']:
        if row['name'] == me: statuses[row['name']] = device_sync.status()
        elif row.get('ssh'):
            # Through Remote.device: a launcher without device support is never handed the words.
            try: statuses[row['name']] = device_sync.peer_status(remotes(row['ssh']))
            except device_sync.Outdated: outdated[row['name']] = row['ssh']
            except (ValueError, RuntimeError, OSError, device_sync.Failed, device_sync.Unreachable, device_sync.Missing): pass
    lead = (registry.conductor(value) or {}).get('device')
    if lead in outdated:
        step = Step(conductor.STEP, 'failed', 'outdated launcher on ' + lead + ' (no device support)', 'claude-pool device add ' + outdated[lead])
    else: step = conductor.check(statuses, value)
    print(render('Conductor' + (' (' + lead + ')' if lead else ''), [step]))
    for name, ssh in sorted(outdated.items()):
        if name != lead: print('  outdated launcher on ' + name + ', its Conductor state is unknown; fix: claude-pool device add ' + ssh)
    return 0 if ready([step]) else 2


def conductor_verb(argv, out, remotes, local, **_):
    action, rest = (argv[0], argv[1:]) if argv else (None, [])
    if action in ('serve', 'install', 'uninstall'): return conductor_internal(action, rest)
    if action in ('move', 'status'): return conductor_user(action, rest, remotes, local)
    raise Usage('claude-pool device conductor move <device> | status')


# Orchestration: add, check and remove --------------------------------------------------------------------------------------

READINGS_MAX_AGE_MS = 900_000
READINGS_WAIT, READINGS_POLL = 30, 5  # seconds `add` waits for a new device's first usage readings
PROBE = ['-p', 'Reply with OK.', '--model', 'haiku']
SEVERITY = {'failed': 0, 'unreachable': 0, 'missing': 1, 'manual': 2, 'skipped': 3, 'ok': 4}
UNINSTALL_SCRIPT = r'''PATH=/opt/homebrew/bin:/usr/local/bin:$PATH; export PATH
wrapper="$HOME/.local/bin/claude-pool"
[ -f "$wrapper" ] || exit 0
line=$(grep '^exec ' "$wrapper" | tail -n 1)
rest=${line% \"\$@\"}
case "$rest" in
  *\') run=${rest%\'}; run=${run##*\'} ;;
  *) run=${rest##* } ;;
esac
dir=$(dirname "$run")
if [ -f "$dir/current/install.py" ]; then installer="$dir/current/install.py"; else installer="$dir/install.py"; fi
[ -n "$run" ] && [ -f "$installer" ] || exit 3
py=
for c in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then py=$(command -v "$c"); break; fi
done
[ -n "$py" ] || exit 3
exec "$py" "$installer" --uninstall --prefix "$dir"'''


def _sleep(seconds): time.sleep(seconds)
def local_platform(): return service.platform()


def local_name():
    raw = re.sub(r'[^a-z0-9-]+', '-', socket.gethostname().split('.')[0].lower()).strip('-')[:40].strip('-')
    return raw or 'this-device'


def name_for(target):
    host = target.rpartition('@')[2].split(':')[0].split('.')[0].lower()
    return re.sub(r'[^a-z0-9-]+', '-', host).strip('-')[:40].strip('-')


def _redacted(error):
    return type(error).__name__ + (': ' + redact(str(error), device_sync.secrets())[:200] if str(error) else '')


def worst(name, steps):
    """One step from several parts: the most serious status, the details of every part that is not ok, its fix."""
    steps = [step for step in steps if step is not None]
    lead = min(steps, key=lambda step: SEVERITY.get(step.status, 0))
    if lead.status == 'ok': return Step(name, 'ok', '; '.join(dict.fromkeys(step.detail for step in steps)))
    detail = '; '.join(step.detail for step in steps if step.status != 'ok')
    return Step(name, lead.status, detail, lead.fix)


def issues_step(name, issues, fine):
    """`issues` are (status, detail, fix); none means `fine`."""
    if not issues: return Step(name, 'ok', fine)
    issues = sorted(issues, key=lambda issue: SEVERITY[issue[0]])
    return Step(name, issues[0][0], '; '.join(issue[1] for issue in issues), issues[0][2])


class Session:
    """Shared state of one add, check or remove run: the registry, this device and the statuses already read."""
    def __init__(self, remotes, local, probe=False):
        self.remotes, self.local, self.probe = remotes, local, probe
        self.me = registry.self_name()
        self._statuses = {}

    def value(self): return registry.load()

    def target(self, row, args=None):
        """The device's Target, or None for another device this one has no ssh target for (the device that ran add)."""
        mine = row['name'] == self.me
        if not mine and not row.get('ssh'): return None
        return Target(self, row, self.local if mine else self.remotes(row['ssh']), mine, args)

    def status_of(self, name):
        """A device's `device status`, or None when it cannot be read. Cached for this run."""
        if name not in self._statuses:
            row = registry.device(self.value(), name)
            try:
                if name == self.me: remote = self.local
                elif row and row.get('ssh'): remote = self.remotes(row['ssh'])
                else: remote = None
                self._statuses[name] = device_sync.peer_status(remote) if remote is not None else None
            except Exception: self._statuses[name] = None
        return self._statuses[name]

    def forget(self, name): self._statuses.pop(name, None)

    def local_status(self): return self.status_of(self.me) or {} if self.me else {}


class Target:
    def __init__(self, session, row, remote, mine, args):
        self.s, self.row, self.remote, self.mine, self.args = session, row, remote, mine, args
        self.where = row.get('ssh') or row['name']
        self.retry = 'claude-pool device add ' + self.where
        self.native, self.supported = None, False

    def status(self, fresh=False):
        if fresh: self.s.forget(self.row['name'])
        return self.s.status_of(self.row['name']) or {}

    def forget(self): self.s.forget(self.row['name'])

    def on(self, command): return command if self.mine else remote_command(self.where, command)

    def needs_install(self, name):
        return Step(name, 'missing', 'needs a claude-pool with device support on ' + self.row['name'] + ' first', self.retry)

    def needs_native(self, name):
        return Step(name, 'missing', 'needs Native Claude on ' + self.row['name'] + ' first', device_install.NATIVE_FIX)


def check_reachable(t):
    if t.mine: return Step('reachable', 'ok', 'this device')
    if t.remote.reachable(): return Step('reachable', 'ok', 'ssh works')
    return Step('reachable', 'unreachable', 'could not reach ' + t.where + ' over ssh',
                'ssh ' + t.where + ' true must work without a password prompt (BatchMode)')


def check_native_claude(t):
    step, t.native = device_install.check_native(t.remote)
    return step


def check_install(t):
    step = device_install.check_install(t.remote)
    t.supported = step.status == 'ok'
    if step.detail == 'older claude-pool without device support':
        return Step('install', 'failed', 'outdated launcher (no device support)', t.retry)
    return step


def ensure_install(t):
    if not t.native: return t.needs_native('install')
    archive = device_install.source_archive(Path(__file__).resolve().parents[1])
    step = device_install.install(t.remote, archive, t.native)
    t.forget()
    return check_install(t) if step.status == 'ok' else step


def check_version(t):
    if not t.supported: return t.needs_install('version')
    wanted = device_sync.build_version()
    if wanted is None: return Step('version', 'skipped', 'this device has no build version to compare with')
    return device_install.check_version(t.remote, wanted)


def ensure_version(t):
    step = ensure_install(t)
    return check_version(t) if step.status == 'ok' else Step('version', step.status, step.detail, step.fix)


def check_linger(t): return device_install.check_linger(t.remote)
def ensure_linger(t): return device_install.enable_linger(t.remote)


def check_shell(t):
    return device_install.check_shell(t.remote) if t.supported else t.needs_install('shell')


def ensure_shell(t):
    return device_install.add_shell(t.remote) if t.supported else t.needs_install('shell')


def _local_fingerprint(name):
    path = settings.LONG_LIVED / (name + '.json')
    if accounts_cli.stored_kind(path) != 'long-lived': return None
    return device_sync.fingerprint(json.loads(path.read_bytes()))


def _reserve_issues(value):
    """This device's config rows whose reserve differs from the registry; runs the reserve hook once when any does."""
    def differing():
        rows = {row['name']: row.get('reserveAt', 0) for row in load_config(settings.CONFIG).get('accounts', [])}
        members = [accounts_cli.member_identity(settings.STORES, name) for name in rows]
        found = []
        for row in value['accounts']:
            name = registry.local_name(row, members)
            if 'reserveAt' in row and name in rows and rows[name] != row['reserveAt']: found.append((name, rows[name], row['reserveAt']))
        return found
    if not differing(): return []
    try: accounts_cli.reserve_hook(value)
    except Exception: pass
    return [('failed', f'{name} reserve {have} % here, {want} % in the registry',
             'claude-pool account reserve ' + name + (' --at ' + str(want) if want else ' --off')) for name, have, want in differing()]


def check_accounts(t):
    if not t.supported: return t.needs_install('accounts')
    st = t.status()
    if not st: return Step('accounts', 'failed', 'could not read the status of ' + t.row['name'], t.retry)
    value, issues, name = registry.load(), [], t.row['name']
    if st.get('name') != name: issues.append(('missing', 'does not know its device name yet', t.retry))
    generation, mine = st.get('registryGeneration'), registry.digest(value)
    if generation == value['generation'] and st.get('registryHash') != mine:
        issues.append(('failed', 'registry diverged ' + mine + ' ' + str(st.get('registryHash')),
                       'claude-pool device sync (merges both lists row by row); if a list is wrong afterwards: '
                       'claude-pool device sync --force-registry on the device whose list is right'))
    elif not isinstance(generation, int) or generation < value['generation']:
        issues.append(('missing', 'registry generation ' + str(generation) + ' there, ' + str(value['generation']) + ' here',
                       'claude-pool device sync'))
    elif generation > value['generation']:
        issues.append(('missing', 'registry generation ' + str(generation) + ' there is ahead of ' + str(value['generation']) + ' here',
                       'claude-pool device sync'))
    theirs = st.get('accounts') or {}
    here = device_sync.account_rows()
    renames = []  # (local name there, registry name): not a failure, but one name everywhere is easier to read
    for row in value['accounts']:
        there = registry.local_name(row, theirs)
        if there is not None and there != row['name'] and (theirs.get(there) or {}).get('active', True): renames.append((there, row['name']))
    for row in value['accounts']:
        # Each device holds the subscription under its own name: matched by UUIDs, by name only without them.
        want = row.get('fingerprint') or _local_fingerprint(device_sync.local_name(row, here) or row['name'])
        if want is None: continue
        there = registry.local_name(row, theirs)
        have = (theirs.get(there) or {}).get('fingerprint')
        if have is None: issues.append(('missing', row['name'] + ' is not on ' + name, 'claude-pool device sync'))
        elif have != want:
            issues.append(('failed', 'fingerprint drift on ' + registry.describe(row, there) + ': ' + want + ' expected, ' + have + ' there',
                           'claude-pool device sync'))
    for account, row in sorted(theirs.items()):
        if row.get('duplicateOf'):
            issues.append(('manual', account + ' on ' + name + ' is a second copy of ' + str(row['duplicateOf']) +
                           ' (the same subscription); nothing serves it',
                           'on ' + name + ': once ' + str(row['duplicateOf']) + ' works, move ' + account +
                           '.json out of the claude-long-lived and credential folders (for example into the credential folder\'s .retired)'))
    overview = st.get('overview') if isinstance(st.get('overview'), dict) else {}
    for row in overview.get('accounts') or []:
        if isinstance(row, dict) and row.get('status') == 'reauthenticate':
            entry = registry.entry_for(value, theirs.get(row.get('name')) or {'name': row.get('name')}) or {}
            who = registry.describe(entry, row.get('name')) if entry else str(row.get('name'))
            issues.append(('failed', who + ' needs reauthenticate (token revoked or expired)',
                           replace_fix(entry.get('email'), entry.get('name') or row.get('name'))))
    if t.s.probe:
        for account, row in sorted(theirs.items()):
            if row.get('kind') != 'long-lived' or not row.get('active', True): continue
            entry = registry.entry_for(value, row) or {}
            # The alias is the target's own: its config row carries the name the status reports.
            alias = (entry.get('alias') if entry.get('name') == account else None) or account
            done = t.remote.pool(alias, *PROBE, timeout=300)
            if done.returncode or b'OK' not in (done.stdout or b''):
                issues.append(('failed', 'probe failed for ' + (registry.describe(entry, account) if entry else account),
                               replace_fix(entry.get('email'), entry.get('name') or account)))
    if t.mine: issues += _reserve_issues(value)
    pending = st.get('switchPending') or []
    if pending:
        issues.append(('manual', 'switch pending for ' + ', '.join(pending), t.on('claude-pool device switch')))
    fine = str(len(theirs)) + ' account' + ('' if len(theirs) == 1 else 's') + ', registry generation ' + str(generation)
    if renames:
        fine += '; ' + ', '.join(local + ' (registry: ' + name + ')' for local, name in renames)
        if not issues:
            return Step('accounts', 'ok', fine, '; '.join(t.on('claude-pool account rename ' + local + ' ' + name + ' --wait') for local, name in renames))
    return issues_step('accounts', issues, fine)


def ensure_accounts(t):
    if not t.supported: return t.needs_install('accounts')
    if t.status().get('name') != t.row['name']: device_sync.peer(t.remote, 'self', t.row['name'])
    t.forget()
    result = device_sync.push(t.remote)
    t.forget()
    if result != 'ok': return Step('accounts', 'failed', 'could not sync: ' + result, t.retry)
    return check_accounts(t)


def check_service(t):
    if not t.supported: return t.needs_install('service')
    return device_install.check_service(t.remote, None if t.mine else t.where)


def ensure_service(t):
    if not t.supported: return t.needs_install('service')
    step = check_service(t)
    if step.detail.startswith('stale'): return step  # --start never reloads a running daemon; a restart would cut running tasks
    step = device_install.start_service(t.remote, t.where)
    t.forget()
    return step


def check_doctor(t):
    if not t.supported: return t.needs_install('doctor')
    step = device_install.check_doctor(t.remote)
    return step._replace(fix=t.on(step.fix)) if step.fix else step


def check_native_login(t):
    if not t.native: return t.needs_native('native-login')
    value = registry.load(); entry = registry.conductor(value)
    return native_login.check(t.remote, t.native, value['accounts'], conductor_here=entry is not None and entry['device'] == t.row['name'])


def ensure_native_login(t):
    step = check_native_login(t)
    if step.status != 'missing' or not t.native or t.mine: return step
    done = native_login.login(t.where, t.native)
    t.forget()
    return done if done.status != 'ok' else check_native_login(t)


def check_herdr(t): return device_integrations.check_herdr(t.row, t.s.local)


def ensure_herdr(t):
    return device_integrations.ensure_herdr(t.row, t.s.local, lambda ssh: t.s.remotes(ssh).reachable())


def check_herdr_sessions(t):
    if t.row.get('platform') == 'darwin': return Step('herdr-sessions', 'skipped', device_herdr.LINUX_ONLY)
    if not t.supported: return t.needs_install('herdr-sessions')
    return device_herdr.check(t.status().get('herdrUnits'), t.row.get('herdrSessions') or [], t.on(device_herdr.VERB),
                              everything=getattr(t.args, 'herdr_units', None) == 'all')


def ensure_herdr_sessions(t):
    """Enables a unit for every registered session that misses one, and with `--herdr-units all` for every running session
    too; never adopts (that stops a running server). The sessions are named on the command line: the device's own
    registry may not list them yet (add syncs it at the end)."""
    everything = getattr(t.args, 'herdr_units', None) == 'all'
    step = check_herdr_sessions(t)
    if step.status != 'missing': return step
    names = t.row.get('herdrSessions') or []
    t.remote.device('herdr-units', *[word for name in names for word in ('--session', name)], *(['--all'] if everything else []),
                    timeout=POOL_TIMEOUT)
    t.forget()
    return check_herdr_sessions(t)


def check_app(t): return device_integrations.check_app(t.row, device_integrations.APP_DIR, local_platform())


def _cua_ips(t):
    there = device_cua.tailscale_ip(t.remote)
    if not there: return None, None, Step('cua', 'skipped', 'no Tailscale address on ' + t.row['name'])
    here = device_cua.tailscale_ip(t.s.local)
    if not here: return None, None, Step('cua', 'skipped', 'no Tailscale address on this device')
    return there, here, None


def _tunnel_ports(value, device):
    """The loopback ports of the SSH tunnels `device` reaches other devices' Cua hosts through, by device name."""
    return dict((registry.device(value, device) or {}).get('cuaTunnels') or {}) if device else {}


def _space_issue(part, ip, port, peer, holder, missing, again):
    """(issue or None, through a tunnel): whether `peer` is a Space on `holder`, directly or at the recorded tunnel port."""
    spaces = part.get('spaces') or []
    if f'direct:{ip}:{device_cua.PORT}' in spaces: return None, False
    if port is None or f'direct:{device_cua.LOOPBACK}:{port}' not in spaces: return ('missing', missing, again), False
    state = (part.get('tunnels') or {}).get(peer)
    if not isinstance(state, dict) or state.get('port') != port:
        return ('missing', 'the SSH tunnel to ' + peer + ' is not loaded on ' + holder, again), True
    if state.get('running') is not True: return ('missing', 'the SSH tunnel to ' + peer + ' does not run on ' + holder, again), True
    return None, True


def check_cua(t):
    if not t.row.get('cua'): return Step('cua', 'skipped', 'not requested (add it with claude-pool device add ' + t.where + ' --cua)')
    if t.mine: return Step('cua', 'skipped', 'this device; each linked device checks it')
    there, here, skipped = _cua_ips(t)
    if skipped: return skipped
    if not t.supported: return t.needs_install('cua')
    part, mine = t.status().get('cua'), t.s.local_status().get('cua') or {}
    again = t.retry + ' --cua'
    name, me = t.row['name'], t.s.me
    if not part: return Step('cua', 'missing', 'Cua is not installed on ' + name, device_cua.DOCS_FIX)
    issues = []
    if not part.get('host'): issues.append(('missing', 'the Cua host does not run on ' + name, again))
    if not part.get('mcp'): issues.append(('missing', 'the Cua MCP server is missing on ' + name, again))
    value = registry.load()
    found, here_via = _space_issue(mine, there, _tunnel_ports(value, me).get(name), name, 'this device',
                                   name + ' is not a Space on this device', again)
    if found: issues.append(found)
    found, there_via = _space_issue(part, here, _tunnel_ports(value, name).get(me), me or 'this device', name,
                                    'this device is not a Space on ' + name, again)
    if found: issues.append(found)
    via = [who for who, used in ((name, here_via), (me, there_via)) if used and who]
    return issues_step('cua', issues, 'linked both ways' + (' (' + ' and '.join(via) + ' through an SSH tunnel)' if via else ''))


def _register_here(t, there, me):
    """Register the device's Cua host on this one; when a firewall drops port 3211, through an SSH tunnel claude-pool owns
    here. The tunnel's port is recorded on this device's registry row, so check and remove find it."""
    name = t.row['name']
    port = _tunnel_ports(registry.load(), me).get(name)
    tunnel = device_cua.Tunnel(t.s.local, name, there, t.row['ssh'], port) if t.row.get('ssh') else None
    step = device_cua.register(t.s.local, device_cua.host_token(t.remote), there, name, tunnel=tunnel)
    if tunnel is not None and tunnel.port is not None and tunnel.port != port and registry.device(registry.load(), me) is not None:
        def record(value):
            row = registry.device(value, me)
            if row is not None: row['cuaTunnels'] = {**(row.get('cuaTunnels') or {}), name: tunnel.port}
        registry.update(record)
    return step


def _drop_tunnel(me, name):
    """A registry change: this device no longer records a tunnel to `name`."""
    def change(value):
        row = registry.device(value, me) if me else None
        tunnels = dict((row or {}).get('cuaTunnels') or {})
        if name not in tunnels: return
        del tunnels[name]
        if tunnels: row['cuaTunnels'] = tunnels
        else: row.pop('cuaTunnels')
    return change


def forget_tunnels(row, local, remote, me):
    """Steps that delete the SSH tunnel this device owns to the device in `row` (and its Space), and, when `remote` is given,
    the tunnels that device owns to others. Returns (steps, whether this device's own tunnel is gone)."""
    steps, gone = [], True
    port = _tunnel_ports(registry.load(), me).get(row['name'])
    if port is not None:
        here = device_cua.forget_tunnel(local, row['name'], port)
        steps += here; gone = all(step.status == 'ok' for step in here)
    if remote is not None:
        for peer in sorted(row.get('cuaTunnels') or {}):
            done = remote.device('cua-tunnel', 'remove', peer)
            steps.append(Step('cua', 'ok', 'SSH tunnel to ' + peer + ' removed on ' + row['name']) if done.returncode == 0 else
                         Step('cua', 'failed', 'could not remove the SSH tunnel to ' + peer + ' on ' + row['name']))
    return steps, gone


def ensure_cua(t):
    if not t.row.get('cua') or t.mine: return check_cua(t)
    there, here, skipped = _cua_ips(t)
    if skipped: return skipped
    if not t.supported: return t.needs_install('cua')
    me = t.s.me or 'this-device'
    def note(value):
        for device, address in ((t.row['name'], there), (me, here)):
            row = registry.device(value, device)
            if row is not None and row.get('tailscaleIp') != address: row['tailscaleIp'] = address
    hosts = [device_cua.ensure_host(t.remote, t.row['name'], there), device_cua.ensure_host(t.s.local, me, here)]
    steps = list(hosts)
    if all(step.status == 'ok' for step in hosts):
        current = registry.load()  # the addresses are recorded only once both hosts share their desktop there
        wanted = copy.deepcopy(current); note(wanted)
        if wanted != current: registry.update(note)
        steps.append(_register_here(t, there, me))
        steps.append(device_cua.register(t.remote, device_cua.host_token(t.s.local), here, me))
    if t.native: steps.append(device_cua.ensure_mcp(t.remote, t.native))
    t.forget(); t.s.forget(t.s.me)
    return worst('cua', steps)


def _adopt(step):
    """A hand-started Conductor is running; handing it to the service is the user's step, so it is listed under Needs you."""
    if step.status == 'failed' and (step.fix or '').startswith('stop the hand-started Conductor'): return step._replace(status='manual')
    return step


def check_conductor(t):
    value = registry.load()
    entry = registry.conductor(value)
    if not entry: return Step('conductor', 'skipped', 'no Conductor configured (claude-pool device add <ssh-target> --usage-owner sets one up)')
    lead, name = entry['device'], t.row['name']
    if name != lead:
        part = t.status().get('conductor') or {}
        if not (part.get('running') or part.get('duplicates') or part.get('error')):
            return Step('conductor', 'skipped', 'the Conductor runs on ' + lead)
    if not t.supported and name == lead: return t.needs_install('conductor')
    statuses = {}
    for device in dict.fromkeys([lead, name, *(row['name'] for row in value['devices'])]):
        found = t.s.status_of(device)
        if found is not None: statuses[device] = found
    return _adopt(conductor.check(statuses, value))


def ensure_conductor(t):
    """Idempotent: creates the registry entry (with --usage-owner, when there is none) and installs the Conductor on the
    target whenever the registry names it and nothing runs there yet. Never installs next to a running Conductor."""
    entry = registry.conductor(registry.load())
    if not entry and not (t.args and t.args.usage_owner): return check_conductor(t)
    if entry and entry['device'] != t.row['name']: return check_conductor(t)
    if not t.supported: return t.needs_install('conductor')
    status = t.status(fresh=True)
    if not entry:
        login = status.get('nativeLogin')
        if not isinstance(login, dict) or not login.get('email'):
            return Step('conductor', 'missing', 'the Conductor needs a Native login on ' + t.row['name'] + ' first', t.retry + ' --usage-owner')
        entry = {'device': t.row['name'], 'email': login['email'], 'organization': login.get('organization') or '', 'cwd': settings.CONDUCTOR_HOME}
        def place(value):
            if not registry.conductor(value): value['conductor'] = entry
        registry.update(place)
    part = status.get('conductor') or {}
    if part.get('running') or part.get('duplicates') or part.get('error'): return check_conductor(t)
    device_sync.peer(t.remote, 'receive-registry', stdin=json.dumps(registry.load()).encode())
    t.remote.device('conductor', 'install', timeout=300)
    t.forget()
    return check_conductor(t)  # the device's own report after the install, never a generic one


def owner_login_fix(owner, row, local=None):
    """Connect a refreshable login for `row` on the usage owner (under `local`, its name there, when it has one): only a
    refreshable grant can read usage."""
    command = 'claude-pool account add ' + (row.get('email') or '<email>') + ' --name ' + (local or row['name'])
    return remote_command(owner['ssh'], command) if owner.get('ssh') else command + ' on ' + owner['name']


def take_usage_role(t):
    """`add --usage-owner`: the role moves only to a device that serves every registry account from its own refreshable
    login (status servingKind); a device that only holds long-lived tokens could read no usage. None when it moved."""
    value = registry.load()
    if registry.is_owner(value, t.row): return None
    rows = t.status(fresh=True).get('accounts') or {}
    missing = []
    for row in value['accounts']:
        there = registry.local_name(row, rows)  # its name on that device: matched by UUIDs, by name only without them
        held = rows.get(there) or {}
        if held.get('servingKind') != 'refreshable' or not held.get('active', True): missing.append((row, there))
    if missing:
        fix = owner_login_fix(t.row, *missing[0]) + ', then claude-pool device add ' + t.where + ' --usage-owner'
        return Step('usage-owner', 'failed', t.row['name'] + ' serves no refreshable login for ' + ', '.join(registry.describe(*item) for item in missing) +
                    ', so it could not read their usage; the usage owner stays ' + ((registry.owner(value) or {}).get('name') or 'unset'), fix)
    def move(current):
        for row in current['devices']: row['roles'] = ['usage'] if row['name'] == t.row['name'] else [r for r in row.get('roles', []) if r != 'usage']
    registry.update(move)
    t.row = registry.device(registry.load(), t.row['name'])
    return None


def check_readings(t):
    value = registry.load()
    owner = registry.owner(value)
    if owner is None: return Step('readings', 'skipped', 'no usage owner')
    if owner['name'] == t.row['name']: return Step('readings', 'skipped', 'this device is the usage owner')
    if not t.supported: return t.needs_install('readings')
    if t.status().get('serviceStale') is True:
        return Step('readings', 'skipped', 'the pool there runs a program from before the last update; see the service step')
    at = t.status().get('sharedReadingsAt')
    now, reach = int(time.time() * 1000), owner.get('ssh') or owner['name']
    if not isinstance(at, int):
        return Step('readings', 'manual', 'no usage readings from ' + owner['name'] + ' yet',
                    'first readings arrive within 5 min; needs ssh from ' + t.where + ' to ' + reach + ' without a password prompt')
    covered = t.status().get('sharedReadingsAccounts')
    if now - at <= READINGS_MAX_AGE_MS and isinstance(covered, list):
        # The readings carry the target's own names (readings.localize); each registry row is looked up under its name there.
        rows = t.status().get('accounts') or {}
        missing = [(row, registry.local_name(row, rows)) for row in value['accounts']]
        missing = [(row, there) for row, there in missing if (there or row['name']) not in covered]
        if missing:
            owners = (t.s.status_of(owner['name']) or {}).get('accounts') or {}
            return Step('readings', 'failed', 'the usage readings from ' + owner['name'] + ' have no row for ' +
                        ', '.join(registry.describe(*item) for item in missing) + ' (the owner serves no refreshable login for it)',
                        owner_login_fix(owner, missing[0][0], registry.local_name(missing[0][0], owners)))
    if now - at <= READINGS_MAX_AGE_MS:
        return Step('readings', 'ok', 'from ' + owner['name'] + ', ' + str(max(0, now - at) // 60000) + ' min old')
    return Step('readings', 'missing', 'no usage readings from ' + owner['name'] + ' in the last 15 minutes',
                t.on('ssh -o BatchMode=yes ' + reach + ' true') + ' must work (readings are pulled from the usage owner every 5 minutes)')


def ensure_readings(t):
    step = check_readings(t)
    waited = 0
    while step.status in ('missing', 'manual') and t.supported and waited < READINGS_WAIT:
        _sleep(READINGS_POLL); waited += READINGS_POLL
        t.forget(); step = check_readings(t)
    return step


def _user(t):
    done = t.remote.call(['id', '-un'])
    return (done.stdout or b'').decode('utf-8', 'replace').strip() or '<user>'


def check_preview(t):
    if t.row.get('platform') == 'darwin': return Step('preview', 'skipped', device_preview.LINUX_ONLY)
    if not t.supported: return t.needs_install('preview')
    have = device_preview._inspect(t.remote) or ''
    if have.split()[-1:] and have.split()[-1] in device_preview.KNOWN_PREVIOUS:
        # a file or a symlink to one; the install path updates a plain file and refuses a symlink with its own fix
        return Step('preview', 'missing', 'an earlier release of the preview tool is installed', t.retry)
    tool = device_preview.check(t.remote, _user(t))
    if tool.status in ('missing', 'failed'): return tool
    if t.remote.device('preview-rules', 'check').returncode:
        return Step('preview', 'missing', 'the Previews rule is not in ~/.claude/CLAUDE.md', t.retry)
    return tool


def ensure_preview(t):
    if t.row.get('platform') == 'darwin' or not t.supported: return check_preview(t)
    step = device_preview.install(t.remote)
    if step.status != 'ok': return step
    if t.remote.device('preview-rules', 'check').returncode and t.remote.device('preview-rules', 'add').returncode:
        return Step('preview', 'failed', 'could not add the Previews rule', t.retry)
    return check_preview(t)


CHECKS = {'reachable': check_reachable, 'native-claude': check_native_claude, 'install': check_install, 'version': check_version,
          'linger': check_linger, 'shell': check_shell, 'accounts': check_accounts, 'service': check_service, 'doctor': check_doctor,
          'native-login': check_native_login, 'herdr': check_herdr, 'herdr-sessions': check_herdr_sessions, 'app': check_app, 'cua': check_cua,
          'conductor': check_conductor, 'readings': check_readings, 'preview': check_preview}
ENSURES = {'install': ensure_install, 'version': ensure_version, 'linger': ensure_linger, 'shell': ensure_shell,
           'accounts': ensure_accounts, 'service': ensure_service, 'native-login': ensure_native_login, 'herdr': ensure_herdr,
           'herdr-sessions': ensure_herdr_sessions,           'cua': ensure_cua, 'conductor': ensure_conductor, 'readings': ensure_readings, 'preview': ensure_preview}
assert set(CHECKS) == set(CHECK_NAMES) and set(ENSURES) <= set(CHECKS)


def run_step(work, name, t):
    try: return work(t)
    except Exception as error: return Step(name, 'failed', _redacted(error), t.retry)


def check_all(t):
    """Every check in order; an unreachable device reports that one step only."""
    first = run_step(check_reachable, 'reachable', t)
    if first.status == 'unreachable': return [first]
    return [first] + [run_step(CHECKS[name], name, t) for name in CHECK_NAMES[1:]]


def _desired(value, me, name, target, platform, args):
    """The registry after `add`: this device and the target listed, with the target's options."""
    if registry.device(value, me) is None:
        value['devices'].append({'name': me, 'platform': local_platform(), 'roles': [], 'addedAt': int(time.time())})
    row = registry.device(value, name)
    if row is None:
        row = {'name': name, 'ssh': target, 'platform': platform, 'roles': [], 'addedAt': int(time.time())}
        value['devices'].append(row)
    row['ssh'], row['platform'] = target, platform
    if args.herdr_session:
        row['herdrSession'] = args.herdr_session
        row['herdrSessions'] = list(dict.fromkeys([*(row.get('herdrSessions') or []), args.herdr_session]))
    if args.cua: row['cua'] = True
    if args.usage_owner and 'usage' not in row.get('roles', []):
        for other in value['devices']: other['roles'] = [role for role in other.get('roles', []) if role != 'usage']
        row['roles'] = ['usage']


def prefer_ensured(done, step, retry):
    """The final report shows the recheck, which names the current fix, unless it says nothing better than the ensure did:
    the ensure step stays when it is manual (the user's part) or when the recheck has no fix or only the generic rerun."""
    if done is None or step.status == 'ok' or done.status == 'ok': return False
    return done.status == 'manual' or not step.fix or step.fix == retry


def print_sync(results):
    for name, result in sorted(results.items()): print('sync ' + name + ': ' + result)


def add(argv, out, remotes, local, **_):
    args = parse('add', argv, (['target'], {}), (['--name'], {}), (['--cua'], {'action': 'store_true'}),
                 (['--usage-owner'], {'action': 'store_true'}), (['--herdr-session'], {}),
                 (['--herdr-units'], {'choices': ['listed', 'all'], 'default': 'listed'}))
    target, value, me = args.target, registry.load(), registry.self_name()
    if args.herdr_session is not None and not registry.HERDR_SESSION.fullmatch(args.herdr_session):
        raise Usage('--herdr-session takes letters, digits, dot, dash and underscore')
    known = next((row for row in value['devices'] if row.get('ssh') == target), None)
    name = args.name or (known['name'] if known else name_for(target))
    if not registry.NAME.fullmatch(name or ''): raise Usage('Device name must match [a-z0-9-]{1,40}; pass --name')
    row = registry.device(value, name)
    if row is not None and row.get('ssh') not in (None, target):
        print(name + ' is already the device at ' + row['ssh'] + '; pick another --name. Nothing was changed.')
        return 2
    if name == (me or local_name()):
        print(name + ' is this device; run claude-pool device add on another device to add this one. Nothing was changed.')
        return 2
    remote = remotes(target)
    reach = Target(Session(remotes, local), {'name': name, 'ssh': target}, remote, False, args)
    first = check_reachable(reach)
    if first.status != 'ok':
        print(render(name + ' (' + target + ')', [first]))
        return 2
    platform = {'Linux': 'linux', 'Darwin': 'darwin'}.get((remote.call(['uname', '-s']).stdout or b'').decode('utf-8', 'replace').strip())
    if platform is None:
        print(render(name + ' (' + target + ')', [first, Step('native-claude', 'failed', 'only macOS and Linux devices are supported', None)]))
        return 2
    if me is None: registry.set_self(local_name()); me = registry.self_name()
    start = value['generation']
    # The usage role moves only after the accounts step, once the target's own logins can be read (take_usage_role).
    placed = argparse.Namespace(**{**vars(args), 'usage_owner': False})
    wanted = copy.deepcopy(value); _desired(wanted, me, name, target, platform, placed)
    if wanted != value: registry.update(lambda current: _desired(current, me, name, target, platform, placed))
    session = Session(remotes, local)
    t = Target(session, registry.device(registry.load(), name), remote, False, args)
    ensured, refused = {'reachable': None}, None
    for step_name in ADD_ORDER[1:]:
        t.row = registry.device(registry.load(), name)
        step = run_step(CHECKS[step_name], step_name, t)
        ensured[step_name] = None
        if step.status != 'ok' and step_name in ENSURES:
            ensured[step_name] = run_step(ENSURES[step_name], step_name, t)
        if step_name == 'accounts' and args.usage_owner:
            refused = run_step(take_usage_role, 'usage-owner', t)
            if refused is not None: t.args = argparse.Namespace(**{**vars(args), 'usage_owner': False})  # no Conductor either
    synced = {}
    if registry.load()['generation'] != start:  # every other device learns about this one (and runs its herdr hook)
        try: synced = device_sync.sync_all(remotes)
        except Exception as error: synced = {'all devices': 'failed: ' + _redacted(error)}
    session._statuses.clear()
    t.row = registry.device(registry.load(), name)
    final = []
    for step in check_all(t):
        done = ensured.get(step.name)
        final.append(done if prefer_ensured(done, step, t.retry) else step)
    if refused is not None: final.append(refused)
    conductor_board.record_checks({name: final})  # the Conductor Board reads the last result instead of checking again
    conductor_board.share_checks(remotes, skip=unreachable_in({name: final}))
    print(render(name + ' (' + target + ')', final))
    print_sync(synced)
    return 0 if ready(final) else 2


def check(argv, out, remotes, local, **_):
    args = parse('check', argv, (['name'], {'nargs': '?'}), (['--probe'], {'action': 'store_true'}), (['--json'], {'action': 'store_true'}))
    value = registry.load()
    rows = [row for row in value['devices'] if args.name in (None, row['name'])]
    if args.name and not rows:
        print('No device named ' + args.name + ' (see claude-pool device list).', file=sys.stderr)
        return 2
    if not rows:
        print('No devices registered yet. Add one with: claude-pool device add <ssh-target>')
        return 0
    session, results = Session(remotes, local, probe=args.probe), {}
    for row in rows:
        try: t = session.target(row)
        except ValueError:
            results[row['name']] = [Step('reachable', 'failed', 'invalid ssh target ' + str(row.get('ssh')), 'claude-pool device add <ssh-target> --name ' + row['name'])]
            continue
        if t is None:
            results[row['name']] = [Step('reachable', 'skipped', NO_SSH + '; check it on itself: claude-pool device check ' + row['name'])]
            continue
        results[row['name']] = check_all(t)
    good = all(ready(steps) for steps in results.values())
    conductor_board.record_checks(results)  # the Conductor Board reads the last result instead of checking again
    conductor_board.share_checks(remotes, skip=unreachable_in(results))  # and so does every peer's board
    if args.json:
        print_json({'ready': good, 'devices': {name: [step._asdict() for step in steps] for name, steps in results.items()}})
    else:
        print('\n\n'.join(render(name + (' (' + row['ssh'] + ')' if row.get('ssh') else ' (this device)' if name == session.me else ''), results[name])
                          for name, row in ((row['name'], row) for row in rows)))
    return 0 if good else 2


LABEL = re.compile(r'[A-Za-z0-9.-]{1,200}')


def _service_label(status):
    label = status.get('serviceLabel')
    return label if isinstance(label, str) and LABEL.fullmatch(label) else service.DEFAULT_LABEL


def _unit_command(label, platform):
    return service.unload_hint(label, platform)


def _loaded_command(label, platform):
    if platform == 'darwin': return 'launchctl print gui/$(id -u)/' + label + ' >/dev/null 2>&1'
    return 'systemctl --user is-active --quiet ' + label + '.service || systemctl --user is-enabled --quiet ' + label + '.service'


def _refuse_role(value, row):
    """Why this device cannot leave the registry now (Conductor device or usage owner), or None."""
    name = row['name']
    if name == (registry.conductor(value) or {}).get('device'):
        other = next((r['name'] for r in value['devices'] if r['name'] != name), '<other>')
        return 'The Conductor runs on ' + name + '. Move it first: claude-pool device conductor move ' + other + '. Nothing was changed.'
    if registry.is_owner(value, row):
        return (name + ' is the usage owner. Make another device the owner first: claude-pool device add <other-target> --usage-owner. '
                'Nothing was changed.')
    return None


def forget(row, local, remotes):
    """`remove --forget`: drops a lost device from this device's records. On the device itself only its previews are
    stopped, and only while it still answers: their enabled units would otherwise serve on across reboots."""
    name, steps = row['name'], []
    previews_stopped = False
    if row.get('ssh') and row.get('platform') != 'darwin':
        remote = remotes(row['ssh'])
        if remote.reachable():
            step = run_step(lambda t: device_preview.remove(remote), 'preview', _Plain(row))
            if step.status == 'failed':
                print(render(name + ' (' + row['ssh'] + ')', [step]))
                print(name + ' stays registered; once its previews are stopped, run: claude-pool device remove ' + name + ' --forget')
                return 1
            steps.append(step); previews_stopped = True
        else:
            steps.append(Step('preview', 'manual', 'its previews keep serving there, also after a reboot',
                              remote_command(row['ssh'], device_preview.STOP_ALL)))
    ip = row.get('tailscaleIp')
    if row.get('cua') and ip:
        space = f'direct:{ip}:{device_cua.PORT}'
        ids = device_cua._space_ids(local)
        if ids is None or space not in ids: steps.append(Step('cua', 'ok', 'no Space for ' + name + ' here'))
        else:
            done = device_cua._cua(local, 'spaces', 'rm', space)
            steps.append(Step('cua', 'ok', 'Space forgotten') if done.returncode == 0 else
                         Step('cua', 'failed', 'could not forget the Space', 'cua spaces rm ' + space))
    else: steps.append(Step('cua', 'skipped', 'not linked' if not row.get('cua') else 'no Tailscale address recorded'))
    me, gone = registry.self_name(), False
    if row.get('cua'):
        try: tunnel_steps, gone = forget_tunnels(row, local, None, me)
        except Exception as error: tunnel_steps = [Step('cua', 'failed', _redacted(error), 'claude-pool device remove ' + name + ' --forget')]
        steps += tunnel_steps
    steps.append(run_step(lambda t: device_integrations.remove_herdr(row, local), 'herdr', _Plain(row)))
    def drop(current):
        current.update(devices=[r for r in current['devices'] if r['name'] != name])
        if gone: _drop_tunnel(me, name)(current)
    registry.update(drop)
    try: synced = device_sync.sync_all(remotes)
    except Exception as error: synced = {'all devices': 'failed: ' + _redacted(error)}
    print(render(name + ' (forgotten here)', steps))
    print_sync(synced)
    print('Removed ' + name + ' from the registry. ' + ('Only its previews were stopped' if previews_stopped else 'Nothing on ' + name + ' was changed')
          + ': tokens copied to ' + name + ' remain there. '
          'If the machine is lost, revoke those long-lived tokens in claude.ai settings and re-enroll each with '
          + replace_fix(None, None) + REPLACE_GONE + '.')
    if local_platform() == 'darwin' and row.get('ssh'):
        print('In the Claude app: Code tab, environment menu, remove the ' + row['ssh'] + ' environment.')
    return 0 if all(step.status in ('ok', 'skipped', 'manual') for step in steps) else 1


def unique_long_lived(theirs):
    """The long-lived accounts another device holds (status rows by name) whose token this device holds under no name:
    compared by fingerprint, so a copy kept here under another local name counts."""
    try: rows = device_sync.account_rows()
    except Exception: rows = []
    held = {row.get(key) for row in rows for key, kind in (('fingerprint', 'kind'), ('servingFingerprint', 'servingKind'))
            if row.get(kind) == 'long-lived' and row.get(key)}
    return [account for account, entry in sorted(theirs.items())
            if entry.get('kind') == 'long-lived' and entry.get('fingerprint') not in held]


def remove(argv, out, remotes, local, **_):
    args = parse('remove', argv, (['name'], {}), (['--wait'], {'action': 'store_true'}), (['--uninstall'], {'action': 'store_true'}),
                 (['--forget'], {'action': 'store_true'}))
    value, me = registry.load(), registry.self_name()
    row = registry.device(value, args.name)
    if row is None:
        print('No device named ' + args.name + ' (see claude-pool device list).'); return 2
    if args.name == me:
        print(args.name + ' is this device; remove it from another device. Nothing was changed.'); return 2
    refused = _refuse_role(value, row)
    if refused: print(refused); return 2
    if args.forget: return forget(row, local, remotes)
    if not row.get('ssh'):
        print(args.name + ' has no ssh target; drop it with claude-pool device remove ' + args.name + ' --forget.'); return 2
    remote = remotes(row['ssh'])
    if not remote.reachable():
        print(args.name + ' is unreachable (ssh ' + row['ssh'] + ' true must work without a password prompt). Nothing was changed. '
              'If it is gone for good: claude-pool device remove ' + args.name + ' --forget')
        return 2
    def read():
        return device_sync.peer_status(remote)
    try: theirs = read()
    except device_sync.Outdated:
        print(args.name + ' runs an older claude-pool without device support; run claude-pool device add ' + row['ssh'] +
              ' first. Nothing was changed.'); return 2
    except (device_sync.Missing, device_sync.Unreachable, device_sync.Failed, ValueError, RuntimeError, OSError) as error:
        print('Could not read the status of ' + args.name + ' (' + _redacted(error) + '). Nothing was changed.'); return 2
    unique = unique_long_lived(theirs['accounts'])
    if unique:
        print(args.name + ' holds long-lived ' + ', '.join(unique) + ' that this device has no matching copy of; copy it here first: '
              'claude-pool device sync. Nothing was changed.')
        return 2
    def overview():
        part = (theirs if overview.first else read()).get('overview')
        overview.first = False
        if not isinstance(part, dict): raise ValueError('no pool overview')
        return part
    overview.first = True
    if not routing.wait_idle(overview, None, args.wait, sleep=_sleep):
        print('Claude tasks are running on ' + args.name + '; run claude-pool device remove ' + args.name +
              ' --wait or rerun later. Nothing was changed.')
        return 3
    title, again = args.name + ' (' + row['ssh'] + ')', 'claude-pool device remove ' + args.name
    def on(command): return remote_command(row['ssh'], command)
    def stop_here(steps):
        print(render(title, steps))
        print(args.name + ' stays registered; once the fix is done, run: ' + again)
        return 1
    preview = run_step(lambda t: device_preview.remove(remote), 'preview', _Plain(row))
    steps = [preview._replace(fix=again) if preview.status == 'failed' else preview]
    if preview.status == 'failed': return stop_here(steps)
    if row.get('platform') == 'linux':  # its units, never another unit; running herdr sessions keep running until the next boot
        if remote.device('herdr-units', '--remove', timeout=POOL_TIMEOUT).returncode:
            return stop_here(steps + [Step('herdr-sessions', 'failed', 'could not remove the herdr session units',
                                           on(device_herdr.VERB + ' --remove'))])
        steps.append(Step('herdr-sessions', 'ok', 'no claude-pool herdr session unit left; running sessions keep running until the next reboot'))
    if not device_install.remove_shell(remote):
        return stop_here(steps + [Step('shell', 'failed', 'could not remove the claude function', on('claude-pool device shell remove'))])
    if device_install.shell_left(remote):
        steps.append(Step('shell', 'manual', 'a claude function that runs claude-pool is still in the shell rc file',
                          'on ' + args.name + ', delete this line from ~/.zshrc or ~/.bashrc: ' + device_install.FUNCTION))
    else: steps.append(Step('shell', 'ok', 'claude function removed'))
    stopped = remote.pool('--stop', timeout=POOL_TIMEOUT)
    if stopped.returncode:
        return stop_here(steps + [Step('service', 'failed', 'the pool on ' + args.name + ' did not stop', on('claude-pool --stop'))])
    label = _service_label(theirs)
    unit = remote.call(['sh', '-c', _unit_command(label, row.get('platform'))])
    if unit.returncode == 0: steps.append(Step('service', 'ok', 'pool stopped and its service disabled'))
    elif unit.returncode != 255 and remote.call(['sh', '-c', _loaded_command(label, row.get('platform'))]).returncode not in (0, 255):
        steps.append(Step('service', 'ok', 'pool stopped; its service was not loaded'))
    else:
        return stop_here(steps + [Step('service', 'failed', 'pool stopped, but its service is still loaded',
                                       on(shlex.quote(_unit_command(label, row.get('platform')))))])
    forgot = remote.device('forget-accounts', timeout=POOL_TIMEOUT)
    if forgot.returncode:
        return stop_here(steps + [Step('accounts', 'failed', 'could not remove the synced accounts', on('claude-pool device forget-accounts'))])
    steps.append(Step('accounts', 'ok', 'synced long-lived accounts removed; its own logins stay'))
    me, gone = registry.self_name(), False
    if row.get('cua'):
        try: steps += device_cua.remove(remote, [local])
        except Exception as error: steps.append(Step('cua', 'failed', _redacted(error), again))
        try:
            tunnel_steps, gone = forget_tunnels(row, local, remote, me)
            steps += tunnel_steps
        except Exception as error: steps.append(Step('cua', 'failed', _redacted(error), again))
    else: steps.append(Step('cua', 'skipped', 'not linked'))
    steps.append(run_step(lambda t: device_integrations.remove_herdr(row, local), 'herdr', _Plain(row)))
    def drop(current):
        current.update(devices=[r for r in current['devices'] if r['name'] != args.name])
        if gone: _drop_tunnel(me, args.name)(current)
    registry.update(drop)
    try: synced = device_sync.sync_all(remotes)
    except Exception as error: synced = {'all devices': 'failed: ' + _redacted(error)}
    if args.uninstall:
        done = remote.call(['sh', '-c', UNINSTALL_SCRIPT], timeout=600)
        if done.returncode == 0: steps.append(Step('install', 'ok', 'claude-pool uninstalled; config, credentials and Native Claude stay'))
        else: steps.append(Step('install', 'failed', 'uninstall failed', 'on ' + row['ssh'] + ', run install.py --uninstall from the claude-pool application folder'))
    print(render(title, steps))
    print_sync(synced)
    print('Removed ' + args.name + ' from the registry.')
    if local_platform() == 'darwin':
        print('In the Claude app: Code tab, environment menu, remove the ' + row['ssh'] + ' environment.')
    return 0 if all(step.status in ('ok', 'skipped', 'manual') for step in steps) else 1


class _Plain:
    """What run_step needs to name a fix when a removal part raises."""
    def __init__(self, row): self.retry = 'claude-pool device remove ' + row['name']


VERBS = {'add': add, 'check': check, 'remove': remove, 'list': list_devices, 'sync': sync,
         'switch': switch, 'conductor': conductor_verb, 'status': status, 'registry': show_registry,
         'receive-registry': receive_registry, 'receive-account': receive_account, 'send-account': send_account,
         'identity': identity, 'readings': export_readings, 'shell': shell, 'cua-token': cua_token, 'cua-tunnel': cua_tunnel,
         'forget-accounts': forget_accounts, 'self': name_self, 'preview-rules': preview_rules, 'herdr-units': herdr_units,
         'receive-checks': receive_checks}


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
