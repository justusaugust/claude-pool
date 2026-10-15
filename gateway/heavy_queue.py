"""Per-suite defaults; legacy queue entry points never serialize work."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys

CAPS = {'CARGO_BUILD_JOBS': '2', 'RUST_TEST_THREADS': '2', 'GOMAXPROCS': '2',
        'VITEST_MAX_WORKERS': '2', 'VITEST_MAX_THREADS': '2', 'VITEST_MAX_FORKS': '2',
        'VITEST_MIN_THREADS': '1', 'VITEST_MIN_FORKS': '1'}
PREFIX = 'export CLAUDE_POOL_SUITE_LIMITS=2 '


def two(value):
    try: return str(min(2, max(1, int(value))))
    except (TypeError, ValueError): return '2'


def option(args, names, add=True, default='2'):
    for i, arg in enumerate(args):
        if arg in names and i + 1 < len(args): args[i + 1] = two(args[i + 1]); return
        for name in names:
            if arg.startswith(name + '='): args[i] = name + '=' + two(arg.split('=', 1)[1]); return
            if name in {'-n', '-j'} and arg.startswith(name) and len(arg) > 2: args[i] = name + two(arg[2:]); return
    if add: args.append(names[0] + '=' + two(default))


def prepare(command):
    if command.startswith(PREFIX): return command
    caps = {k: two(os.environ.get(k, v)) for k, v in CAPS.items()}
    # Compound scripts retain their shell syntax; runner flags need a direct invocation.
    if not any(c in command for c in ';&|<>()\n$`*?[]{}'):
        try: args = shlex.split(command)
        except ValueError: args = []
        if args:
            name = Path(args[0]).name
            direct = args[1:]
            prefix_count = 1
            if name in {'npx', 'bunx'} and direct:
                while direct and direct[0] in {'-y', '--yes', '--no-install'}: direct.pop(0); prefix_count += 1
                if direct: name = direct.pop(0); prefix_count += 1
            elif name == 'pnpm' and direct[:1] == ['exec'] and len(direct) > 1: direct.pop(0); name = direct.pop(0); prefix_count = 3
            if any(a in {'--help', '--version', '-h', '--watch', '--watchAll'} for a in direct): name = ''
            if name in {'vitest', 'jest'}:
                workers = min(int(caps[k]) for k in ['VITEST_MAX_WORKERS', 'VITEST_MAX_THREADS', 'VITEST_MAX_FORKS']) if name == 'vitest' else 2
                option(direct, ['--maxWorkers'], default=workers); option(direct, ['--minWorkers'], False)
                if name == 'vitest':
                    value = next((arg.split('=', 1)[1] for arg in direct if arg.startswith('--maxWorkers=')), None)
                    if value is None: value = direct[direct.index('--maxWorkers') + 1]
                    value = str(min(int(two(value)), int(workers)))
                    for key in ['VITEST_MAX_WORKERS', 'VITEST_MAX_THREADS', 'VITEST_MAX_FORKS']: caps[key] = value
            elif name == 'playwright' and direct[:1] == ['test']: option(direct, ['--workers', '-j'])
            elif name in {'pytest', 'py.test'}: option(direct, ['-n', '--numprocesses'], False)
            elif name == 'cargo' and any(a in {'test', 'build', 'clippy', 'bench'} for a in direct[:2]):
                split = direct.index('--') if '--' in direct else len(direct)
                build, tests = direct[:split], direct[split:]
                option(build, ['--jobs', '-j'], default=os.environ.get('CARGO_BUILD_JOBS', '2')); option(tests, ['--test-threads'], False)
                direct = build + tests
            elif name == 'go' and direct[:1] in (['test'], ['build'], ['vet']):
                option(direct, ['-p'], default=os.environ.get('GOMAXPROCS', '2'))
                if direct[:1] == ['test']: option(direct, ['-parallel'], default=os.environ.get('GOMAXPROCS', '2'))
            else: direct = None
            if direct is not None:
                # Preserve the original executable/launcher while replacing only its arguments.
                command = shlex.join(args[:prefix_count] + direct)
    exports = ' '.join(k + '=' + v for k, v in caps.items())
    return PREFIX + exports + '\n' + command


def run(command, shell, lock=None):
    child = None
    def stop(signum, frame):
        if child is not None and child.poll() is None:
            try: os.killpg(child.pid, signum)
            except ProcessLookupError: pass
        else: raise SystemExit(128 + signum)
    previous = {s: signal.signal(s, stop) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        child = subprocess.Popen([shell, '-c', prepare(command)], start_new_session=True)
        result = child.wait()
        return 128 - result if result < 0 else result
    finally:
        for s, handler in previous.items(): signal.signal(s, handler)


if __name__ == '__main__':
    # shortcut: old loaded hooks use these entry points; remove after those sessions exit.
    if sys.argv[1:] == ['--classify']: sys.stdin.read(1024 * 1024); print('light')
    elif sys.argv[1:] == ['--prepare']: print(json.dumps({'command': prepare(sys.stdin.read(1024 * 1024))}))
    elif len(sys.argv) == 3 and sys.argv[1] == '--gate':
        if int(sys.argv[2]) != os.getppid(): raise SystemExit('Gate caller changed')
    elif len(sys.argv) == 3: raise SystemExit(run(sys.argv[2], sys.argv[1]))
    else: raise SystemExit('Usage: heavy_queue.py SHELL COMMAND')
